from __future__ import annotations

from dataclasses import dataclass
import re

from clients.apollo import (
    ApolloClient,
    ApolloCurrentCompanyUnavailableError,
    ApolloError,
    CurrentEmploymentEvidence,
)
from services.company_matcher import company_match_score, normalize_company_name


@dataclass(frozen=True, slots=True)
class CompanyResolution:
    company_name: str
    status: str
    error: str = ""
    domain: str = ""
    company_linkedin_url: str = ""


class PersonCompanyResolver:
    """Resolve the current employer from the person's LinkedIn profile via Apollo."""

    def __init__(self, apollo: ApolloClient) -> None:
        self.apollo = apollo

    def resolve(
        self, *, person_name: str, linkedin_url: str, supplied_company_name: str, headline: str
    ) -> CompanyResolution:
        headline_company = self._explicit_headline_company(headline)
        supplied_company = supplied_company_name.strip()
        try:
            organization = self.apollo.person_company(linkedin_url, person_name)
            matching_current = self._matching_current_employments(
                organization.name,
                organization.organization_id,
                organization.current_employments,
            )
            evidence_names = [
                organization.name,
                *(job.organization_name for job in matching_current if job.organization_name),
            ]
            input_headline_matches = self._headline_mentions_company(headline, evidence_names)
            apollo_headline_matches = self._headline_mentions_company(
                organization.person_headline, evidence_names
            )
            headline_company_matches = self._company_matches_any(
                headline_company, evidence_names
            )
            supplied_matches = self._company_matches_any(supplied_company, evidence_names)
            strong_current_evidence = any(
                job.organization_name and job.start_date for job in matching_current
            )
            former_role = bool(
                input_headline_matches
                and re.search(r"\b(?:former|formerly|ex)[\s-]", headline, re.I)
            )
            if not organization.profile_matched:
                if not (
                    self._person_name_matches(person_name, organization.person_name)
                    and input_headline_matches
                    and strong_current_evidence
                ):
                    if headline_company:
                        return CompanyResolution(
                            headline_company,
                            "linkedin_headline_verified",
                            error=(
                                f"Ignored Apollo candidate {organization.name!r} because it "
                                "belonged to a different LinkedIn profile"
                            ),
                        )
                    return CompanyResolution(
                        "",
                        "apollo_profile_conflict",
                        error=(
                            f"Rejected Apollo candidate {organization.name!r}: it belonged to "
                            "a different LinkedIn profile and no headline employer was available"
                        ),
                    )
                return CompanyResolution(
                    organization.name,
                    "apollo_cross_verified",
                    domain=organization.domain,
                    company_linkedin_url=organization.linkedin_url,
                )
            if former_role:
                return CompanyResolution(
                    organization.name,
                    "apollo_former_role_review",
                    error=(
                        f"The uploaded headline describes {organization.name!r} as a former "
                        "employer while Apollo still marks it current; manual review is required"
                    ),
                    domain=organization.domain,
                    company_linkedin_url=organization.linkedin_url,
                )
            if headline_company and not headline_company_matches:
                return CompanyResolution(
                    headline_company,
                    "linkedin_headline_verified",
                    error=(
                        f"Rejected weak/stale Apollo company {organization.name!r}; the uploaded "
                        f"headline explicitly identifies current employer {headline_company!r}"
                    ),
                )
            if supplied_company and not supplied_matches and not input_headline_matches:
                return CompanyResolution(
                    supplied_company,
                    "apollo_company_conflict",
                    error=(
                        f"Apollo reported {organization.name!r}, which conflicts with supplied "
                        f"company {supplied_company!r}; manual confirmation is required"
                    ),
                )
            corroborated = (
                input_headline_matches or apollo_headline_matches or supplied_matches
            )
            if strong_current_evidence and corroborated:
                return CompanyResolution(
                    organization.name,
                    "apollo_cross_verified",
                    domain=organization.domain,
                    company_linkedin_url=organization.linkedin_url,
                )
            return CompanyResolution(
                organization.name,
                "apollo_reported_current",
                error=(
                    "Apollo reported this company, but its current-employment evidence was "
                    "incomplete or lacked headline/company corroboration; confirm it before use"
                ),
                domain=organization.domain,
                company_linkedin_url=organization.linkedin_url,
            )
        except ApolloCurrentCompanyUnavailableError as exc:
            if headline_company:
                return CompanyResolution(
                    headline_company,
                    "linkedin_headline_verified",
                    error=f"Apollo current company was unavailable; used explicit headline: {exc}",
                )
            if supplied_company:
                return CompanyResolution(
                    supplied_company,
                    "apollo_current_company_unavailable",
                    error=f"{exc}; manually confirm the supplied company before use",
                )
            return CompanyResolution("", "apollo_current_company_unavailable", str(exc))
        except ApolloError as exc:
            if headline_company:
                return CompanyResolution(
                    headline_company,
                    "linkedin_headline_verified",
                    error=f"Apollo lookup was incomplete; used explicit LinkedIn headline: {exc}",
                )
            return CompanyResolution("", "unresolved", str(exc))

    @staticmethod
    def _explicit_headline_company(headline: str) -> str:
        match = re.search(r"(?:\bat\b|@)\s*([^|,]+)", " ".join(headline.split()), re.I)
        return match.group(1).strip(" -") if match else ""

    @staticmethod
    def _company_matches_any(company: str, candidates: list[str]) -> bool:
        return bool(
            company
            and any(
                candidate and company_match_score(company, candidate) >= 70
                for candidate in candidates
            )
        )

    @staticmethod
    def _headline_mentions_company(headline: str, companies: list[str]) -> bool:
        headline_tokens = set(normalize_company_name(headline).split())
        if not headline_tokens:
            return False
        for company in companies:
            company_tokens = set(normalize_company_name(company).split())
            if company_tokens and company_tokens.issubset(headline_tokens):
                return True
        return False

    @staticmethod
    def _matching_current_employments(
        company_name: str,
        organization_id: str,
        employments: tuple[CurrentEmploymentEvidence, ...],
    ) -> tuple[CurrentEmploymentEvidence, ...]:
        matches = []
        for employment in employments:
            id_matches = bool(
                organization_id
                and employment.organization_id
                and organization_id == employment.organization_id
            )
            name_matches = bool(
                employment.organization_name
                and company_match_score(company_name, employment.organization_name) >= 70
            )
            if id_matches or name_matches:
                matches.append(employment)
        return tuple(matches)

    @staticmethod
    def _person_name_matches(expected: str, returned: str) -> bool:
        expected_parts = re.findall(r"[a-z0-9]+", expected.casefold())
        returned_parts = re.findall(r"[a-z0-9]+", returned.casefold())
        if expected_parts == returned_parts and expected_parts:
            return True
        if len(expected_parts) < 2 or len(returned_parts) < 2:
            return False
        return (
            expected_parts[0] == returned_parts[0]
            and expected_parts[-1][0] == returned_parts[-1][0]
            and (
                expected_parts[-1] == returned_parts[-1]
                or len(expected_parts[-1]) == 1
                or len(returned_parts[-1]) == 1
            )
        )
