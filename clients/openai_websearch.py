from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import time
from typing import Any

import requests

from services.country_normalizer import CountryNormalizationError, country_name, normalize_country


LOGGER = logging.getLogger(__name__)


class OpenAIResearchError(RuntimeError):
    pass


class OpenAIResearchNoMatchError(OpenAIResearchError):
    pass


@dataclass(frozen=True, slots=True)
class CompanyResearch:
    company_name: str
    country: str
    country_code: str
    headquarters: str
    domain: str = ""
    linkedin_url: str = ""
    confidence: str = "medium"
    evidence: str = ""
    sources: tuple[str, ...] = ()


class OpenAIWebSearchClient:
    """Resolve current employer and headquarters using OpenAI web search."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-5-mini",
        timeout_seconds: float = 45.0,
        max_retries: int = 3,
        search_context_size: str = "low",
        session: requests.Session | None = None,
    ) -> None:
        if not api_key.strip():
            raise OpenAIResearchError("OPENAI_API_KEY is not configured")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip() or "gpt-5-mini"
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.search_context_size = search_context_size
        self.session = session or requests.Session()

    def person_company(
        self,
        *,
        person_name: str,
        linkedin_url: str,
        headline: str = "",
        supplied_company_name: str = "",
    ) -> CompanyResearch:
        prompt = (
            "Find the current employer/company represented by this person. Use web search. "
            "Prefer the public LinkedIn profile snippet/page, the person's employer page, "
            "official company leadership/team pages, press releases, or reliable business profiles. "
            "Do not use stale jobs if a newer current role is visible. Do not guess.\n\n"
            f"Person name: {person_name}\n"
            f"LinkedIn profile URL: {linkedin_url}\n"
            f"CSV supplied company, if any: {supplied_company_name or '(none)'}\n"
            f"LinkedIn headline, if any: {headline or '(none)'}\n\n"
            "Return JSON only with these keys: company_name, domain, company_linkedin_url, "
            "headquarters, country, country_code, confidence, evidence, sources. "
            "Use confidence high, medium, low, or none. sources must be an array of URLs. "
            "If no current employer can be verified, set company_name to empty string and confidence to none."
        )
        data = self._request_json(prompt)
        return self._research_from_json(data, require_company=True)

    def company_details(
        self, *, company_name: str, linkedin_url: str = "", domain: str = ""
    ) -> CompanyResearch:
        prompt = (
            "Find the company's official headquarters and headquarters country. Use web search. "
            "Prefer official company pages, annual reports, trusted company profiles, or the "
            "company LinkedIn/About page. Do not guess.\n\n"
            f"Company name: {company_name}\n"
            f"Company LinkedIn URL, if known: {linkedin_url or '(none)'}\n"
            f"Company domain, if known: {domain or '(none)'}\n\n"
            "Return JSON only with these keys: company_name, domain, company_linkedin_url, "
            "headquarters, country, country_code, confidence, evidence, sources. "
            "Use confidence high, medium, low, or none. sources must be an array of URLs."
        )
        data = self._request_json(prompt)
        return self._research_from_json(data, fallback_company=company_name)

    def _request_json(self, prompt: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "tools": [{"type": "web_search", "search_context_size": self.search_context_size}],
            "input": prompt,
            "max_output_tokens": 1200,
        }
        response = self._post(payload)
        text = self._output_text(response)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise OpenAIResearchError("OpenAI response did not contain JSON")
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise OpenAIResearchError("OpenAI response JSON was not an object")
        return parsed

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: BaseException | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                if response.status_code in {408, 409, 429} or response.status_code >= 500:
                    raise OpenAIResearchError(
                        f"OpenAI returned temporary HTTP {response.status_code}: {response.text[:500]}"
                    )
                if response.status_code == 401:
                    raise OpenAIResearchError("OpenAI rejected the API key (HTTP 401)")
                if response.status_code == 403:
                    raise OpenAIResearchError("OpenAI API key lacks access to this model/tool (HTTP 403)")
                if response.status_code >= 400:
                    raise OpenAIResearchError(
                        f"OpenAI returned HTTP {response.status_code}: {response.text[:500]}"
                    )
                body = response.json()
                if not isinstance(body, dict):
                    raise OpenAIResearchError("OpenAI returned an unexpected response format")
                return body
            except OpenAIResearchError as exc:
                last_error = exc
                if "temporary HTTP" not in str(exc) or attempt >= self.max_retries:
                    raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
            delay = min(2 ** (attempt - 1), 8)
            LOGGER.warning("Temporary OpenAI failure (attempt %d/%d); retrying in %ds", attempt, self.max_retries, delay)
            time.sleep(delay)
        raise OpenAIResearchError(f"OpenAI request failed after {self.max_retries} attempt(s): {last_error}")

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        parts: list[str] = []
        for item in response.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text = content.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        text = "\n".join(parts).strip()
        if not text:
            raise OpenAIResearchError("OpenAI response did not include output text")
        return text

    @staticmethod
    def _research_from_json(
        data: dict[str, Any], *, require_company: bool = False, fallback_company: str = ""
    ) -> CompanyResearch:
        company_name = _clean_text(data.get("company_name")) or fallback_company.strip()
        confidence = (_clean_text(data.get("confidence")) or "none").casefold()
        if confidence not in {"high", "medium", "low", "none"}:
            confidence = "low"
        if require_company and (not company_name or confidence in {"low", "none"}):
            raise OpenAIResearchNoMatchError(_clean_text(data.get("evidence")) or "OpenAI could not verify a current employer")

        raw_country = _clean_text(data.get("country_code")) or _clean_text(data.get("country"))
        try:
            code = normalize_country(raw_country)
        except CountryNormalizationError as exc:
            raise OpenAIResearchNoMatchError(
                f"OpenAI did not return a usable headquarters country for {company_name or 'this company'}"
            ) from exc

        country = country_name(code)
        headquarters = _clean_text(data.get("headquarters")) or country
        sources = tuple(
            source.strip()
            for source in data.get("sources", [])
            if isinstance(source, str) and source.strip()
        )
        return CompanyResearch(
            company_name=company_name,
            domain=_clean_text(data.get("domain")),
            linkedin_url=_clean_text(data.get("company_linkedin_url")),
            headquarters=headquarters,
            country=country,
            country_code=code,
            confidence=confidence,
            evidence=_clean_text(data.get("evidence")),
            sources=sources,
        )


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
