from __future__ import annotations

from dataclasses import dataclass
import re

from clients.apollo import ApolloClient, ApolloError
from services.company_matcher import company_match_score


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
        try:
            organization = self.apollo.person_company(linkedin_url, person_name)
            headline_matches = bool(
                headline_company
                and company_match_score(headline_company, organization.name) >= 70
            )
            if not organization.profile_matched:
                if not (
                    self._person_name_matches(person_name, organization.person_name)
                    and headline_matches
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
            if (
                headline_company
                and not headline_matches
            ):
                return CompanyResolution(
                    headline_company,
                    "linkedin_headline_verified",
                    error=(
                        f"Used explicit LinkedIn headline employer {headline_company!r}; "
                        f"Apollo returned conflicting company {organization.name!r}"
                    ),
                )
            return CompanyResolution(
                organization.name,
                "apollo_verified",
                domain=organization.domain,
                company_linkedin_url=organization.linkedin_url,
            )
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
