from __future__ import annotations

from dataclasses import dataclass
import re

from clients.openai_web_search import OpenAIWebSearchClient, WebSearchError
from services.company_matcher import company_match_score


@dataclass(frozen=True, slots=True)
class CompanyResolution:
    company_name: str
    status: str
    error: str = ""
    domain: str = ""
    company_linkedin_url: str = ""


class PersonCompanyResolver:
    """Resolve a person's current employer with OpenAI web search."""

    def __init__(self, web_search: OpenAIWebSearchClient) -> None:
        self.web_search = web_search

    def resolve(
        self, *, person_name: str, linkedin_url: str, supplied_company_name: str, headline: str
    ) -> CompanyResolution:
        headline_company = self._explicit_headline_company(headline)
        try:
            organization = self.web_search.person_company(linkedin_url, person_name)
            headline_matches = bool(
                headline_company
                and company_match_score(headline_company, organization.name) >= 70
            )
            if headline_company and not headline_matches:
                return CompanyResolution(
                    headline_company,
                    "linkedin_headline_verified",
                    error=(
                        f"Used explicit LinkedIn headline employer {headline_company!r}; "
                        f"web search returned conflicting company {organization.name!r}"
                    ),
                )
            return CompanyResolution(
                organization.name,
                "web_search_verified",
                domain=organization.domain,
                company_linkedin_url=organization.linkedin_url,
            )
        except WebSearchError as exc:
            if headline_company:
                return CompanyResolution(
                    headline_company,
                    "linkedin_headline_verified",
                    error=f"Web search was incomplete; used explicit LinkedIn headline: {exc}",
                )
            return CompanyResolution("", "unresolved", str(exc))

    @staticmethod
    def _explicit_headline_company(headline: str) -> str:
        match = re.search(r"(?:\bat\b|@)\s*([^|,]+)", " ".join(headline.split()), re.I)
        return match.group(1).strip(" -") if match else ""
