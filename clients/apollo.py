from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from models.company import ApolloCompany
from services.company_matcher import company_match_score
from services.country_normalizer import CountryNormalizationError, normalize_country


LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ApolloError(RuntimeError):
    pass


class ApolloNoMatchError(ApolloError):
    pass


@dataclass(frozen=True, slots=True)
class PersonOrganization:
    """Current employer identifiers returned by Apollo People Match."""

    name: str
    domain: str = ""
    linkedin_url: str = ""


def normalize_url(value: str | None) -> str:
    if not value:
        return ""
    raw = value.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, flags=re.I):
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    hostname = (parts.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    path = re.sub(r"/+", "/", parts.path).rstrip("/").lower()
    return urlunsplit(("https", hostname, path, "", "")) if hostname else ""


def normalize_domain(value: str | None) -> str:
    if not value:
        return ""
    normalized_url = normalize_url(value)
    hostname = urlsplit(normalized_url).hostname or value.strip().lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


class ApolloClient:
    """Small, retrying wrapper around Apollo organization enrichment/search."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        match_threshold: int,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.match_threshold = match_threshold
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "content-type": "application/json",
                "x-api-key": api_key,
                "user-agent": "servicenow-customer-checker/1.0",
            }
        )

    def enrich(self, company_name: str, linkedin_url: str = "", domain: str = "") -> ApolloCompany:
        # Apollo's enrichment endpoint is most reliable when a domain or company
        # LinkedIn URL is available. The workflow starts from a *person* profile,
        # so it intentionally has neither; search is the correct name-only path.
        if not linkedin_url and not domain:
            return self._best_search_match(company_name, linkedin_url, domain)

        params: dict[str, str] = {"name": company_name}
        if linkedin_url:
            params["linkedin_url"] = linkedin_url
        if domain:
            params["domain"] = normalize_domain(domain)

        payload = self._request("GET", "/organizations/enrich", params=params)
        organization = payload.get("organization") if isinstance(payload, dict) else None
        if isinstance(organization, dict) and self._is_safe_match(
            company_name, linkedin_url, domain, organization
        ):
            return self._to_company(organization, company_name)

        LOGGER.info("Apollo direct enrichment did not return a sufficiently reliable match; trying search")
        return self._best_search_match(company_name, linkedin_url, domain)

    def _best_search_match(self, company_name: str, linkedin_url: str, domain: str) -> ApolloCompany:
        candidates = self._search(company_name, domain)
        ranked = sorted(
            (
                (self._organization_score(company_name, linkedin_url, domain, candidate), candidate)
                for candidate in candidates
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] < self.match_threshold:
            raise ApolloNoMatchError("Apollo could not identify a reliable organization match")
        best_score, best = ranked[0]
        if len(ranked) > 1 and ranked[1][0] == best_score and best_score < 100:
            raise ApolloNoMatchError("Apollo returned multiple equally plausible organizations")
        try:
            return self._to_company(best, company_name, score_override=best_score)
        except ApolloNoMatchError as exc:
            # Organization Search often identifies the correct company but omits
            # headquarters fields. Enrich it again using its returned stable
            # identifier rather than a bare name (which Apollo may reject with 422).
            if "headquarters country" not in str(exc):
                raise
            enriched = self._enrich_search_candidate(best, company_name, best_score)
            if enriched is None:
                raise
            return enriched

    def _enrich_search_candidate(
        self, candidate: dict[str, Any], requested_name: str, score: int
    ) -> ApolloCompany | None:
        domain = normalize_domain(
            str(candidate.get("primary_domain") or candidate.get("website_url") or "")
        )
        linkedin_url = str(candidate.get("linkedin_url") or "").strip()
        if not domain and not linkedin_url:
            return None
        params: dict[str, str] = {"name": str(candidate.get("name") or requested_name)}
        if domain:
            params["domain"] = domain
        if linkedin_url:
            params["linkedin_url"] = linkedin_url
        payload = self._request("GET", "/organizations/enrich", params=params)
        organization = payload.get("organization") if isinstance(payload, dict) else None
        if not isinstance(organization, dict):
            return None
        return self._to_company(organization, requested_name, score_override=score)

    def person_company(self, linkedin_url: str, person_name: str = "") -> PersonOrganization:
        """Resolve a person's current organization using Apollo's People Match API.

        The endpoint is intentionally isolated here: no LinkedIn page is scraped by this
        application, and a failed lookup is reported as an unresolved person rather than
        guessed from an unrelated organization.
        """

        normalized = normalize_url(linkedin_url)
        if not normalized or "linkedin.com/" not in normalized:
            raise ApolloNoMatchError("A valid LinkedIn profile URL is required")
        # Apollo documents these as query parameters for the People Match endpoint.
        payload = self._request("POST", "/people/match", params={"linkedin_url": normalized})
        person = payload.get("person") if isinstance(payload.get("person"), dict) else payload
        if not isinstance(person, dict):
            raise ApolloNoMatchError("Apollo did not return a person record")

        organization = person.get("organization")
        if isinstance(organization, dict) and isinstance(organization.get("name"), str):
            name = organization["name"].strip()
            if name:
                return PersonOrganization(
                    name=name,
                    domain=normalize_domain(
                        str(organization.get("primary_domain") or organization.get("website_url") or "")
                    ),
                    linkedin_url=normalize_url(str(organization.get("linkedin_url") or "")),
                )
        label = f" for {person_name}" if person_name else ""
        raise ApolloNoMatchError(f"Apollo did not return a current organization{label}")

    def _search(self, company_name: str, domain: str) -> list[dict[str, Any]]:
        params: list[tuple[str, str | int]] = [
            ("q_organization_name", company_name),
            ("page", 1),
            ("per_page", 10),
        ]
        if domain:
            params.append(("q_organization_domains_list[]", normalize_domain(domain)))
        payload = self._request("POST", "/mixed_companies/search", params=params)
        organizations = payload.get("organizations", []) if isinstance(payload, dict) else []
        return [item for item in organizations if isinstance(item, dict)]

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    timeout=self.timeout_seconds,
                    **kwargs,
                )
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise requests.HTTPError(
                        f"Apollo returned temporary HTTP {response.status_code}", response=response
                    )
                if response.status_code == 401:
                    raise ApolloError("Apollo rejected the API key (HTTP 401)")
                if response.status_code == 403:
                    raise ApolloError("Apollo API key lacks access to this endpoint (HTTP 403)")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ApolloError("Apollo returned an unexpected response format")
                return payload
            except ApolloError:
                raise
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                last_error = exc
                status = (
                    exc.response.status_code
                    if isinstance(exc, requests.HTTPError) and exc.response is not None
                    else None
                )
                retryable = isinstance(exc, (requests.Timeout, requests.ConnectionError)) or status in RETRYABLE_STATUS_CODES
                if not retryable:
                    response_text = ""
                    if isinstance(exc, requests.HTTPError) and exc.response is not None:
                        response_text = " ".join(exc.response.text.split())[:500]
                    detail = f"HTTP {status}" if status is not None else str(exc)
                    if response_text:
                        detail = f"{detail}: {response_text}"
                    raise ApolloError(f"Apollo request failed: {detail}") from exc
                if attempt == self.max_retries:
                    break
                delay = min(2 ** (attempt - 1), 8)
                LOGGER.warning("Temporary Apollo failure (attempt %d/%d); retrying in %ds", attempt, self.max_retries, delay)
                time.sleep(delay)
            except (ValueError, requests.RequestException) as exc:
                raise ApolloError(f"Apollo request failed: {exc}") from exc
        raise ApolloError(f"Apollo request failed after {self.max_retries} attempt(s): {last_error}")

    def _is_safe_match(
        self,
        company_name: str,
        linkedin_url: str,
        domain: str,
        organization: dict[str, Any],
    ) -> bool:
        return self._organization_score(company_name, linkedin_url, domain, organization) >= self.match_threshold

    def _organization_score(
        self,
        company_name: str,
        linkedin_url: str,
        domain: str,
        organization: dict[str, Any],
    ) -> int:
        returned_name = str(organization.get("name") or "")
        name_score = company_match_score(company_name, returned_name)
        expected_linkedin = normalize_url(linkedin_url)
        returned_linkedin = normalize_url(str(organization.get("linkedin_url") or ""))
        expected_domain = normalize_domain(domain)
        returned_domain = normalize_domain(
            str(organization.get("primary_domain") or organization.get("website_url") or "")
        )
        linkedin_exact = bool(expected_linkedin and returned_linkedin and expected_linkedin == returned_linkedin)
        domain_exact = bool(expected_domain and returned_domain and expected_domain == returned_domain)
        if linkedin_exact or domain_exact:
            return max(95, name_score)
        if expected_linkedin and returned_linkedin and expected_linkedin != returned_linkedin:
            return min(name_score, 69)
        if expected_domain and returned_domain and expected_domain != returned_domain:
            return min(name_score, 69)
        return name_score

    def _to_company(
        self, organization: dict[str, Any], requested_name: str, score_override: int | None = None
    ) -> ApolloCompany:
        country = str(organization.get("country") or "").strip()
        try:
            country_code = normalize_country(country)
        except CountryNormalizationError as exc:
            raise ApolloNoMatchError("Apollo match has no usable headquarters country") from exc
        city = str(organization.get("city") or "").strip()
        state = str(organization.get("state") or "").strip()
        parts = [part for part in (city, state, country) if part]
        returned_name = str(organization.get("name") or requested_name).strip()
        score = score_override if score_override is not None else company_match_score(requested_name, returned_name)
        return ApolloCompany(
            company_name=returned_name,
            country=country,
            country_code=country_code,
            city=city,
            state=state,
            headquarters=", ".join(parts),
            linkedin_url=str(organization.get("linkedin_url") or ""),
            domain=str(organization.get("primary_domain") or ""),
            match_score=score,
        )
