from __future__ import annotations

from dataclasses import dataclass
import re

from clients.openai_websearch import OpenAIResearchError, OpenAIWebSearchClient
from services.company_matcher import company_match_score


@dataclass(frozen=True, slots=True)
class CompanyResolution:
    company_name: str
    status: str
    error: str = ""
    domain: str = ""
    company_linkedin_url: str = ""
    headquarters: str = ""
    country: str = ""
    country_code: str = ""


class PersonCompanyResolver:
    """Resolve the current employer from public web evidence via OpenAI web search."""

    def __init__(self, research: OpenAIWebSearchClient) -> None:
        self.research = research

    def resolve(
        self, *, person_name: str, linkedin_url: str, supplied_company_name: str, headline: str
    ) -> CompanyResolution:
        headline_company = self._explicit_headline_company(headline)
        try:
            organization = self.research.person_company(
                person_name=person_name,
                linkedin_url=linkedin_url,
                headline=headline,
                supplied_company_name=supplied_company_name,
            )
            if (
                headline_company
                and company_match_score(headline_company, organization.company_name) < 70
            ):
                detail = (
                    f"OpenAI web search verified {organization.company_name!r}; "
                    f"headline mentioned {headline_company!r}"
                )
            else:
                detail = organization.evidence
            return CompanyResolution(
                organization.company_name,
                "openai_verified",
                error=detail,
                domain=organization.domain,
                company_linkedin_url=organization.linkedin_url,
                headquarters=organization.headquarters,
                country=organization.country,
                country_code=organization.country_code,
            )
        except OpenAIResearchError as exc:
            if headline_company:
                return CompanyResolution(
                    headline_company,
                    "linkedin_headline_verified",
                    error=f"OpenAI web search was incomplete; used explicit LinkedIn headline: {exc}",
                )
            return CompanyResolution("", "unresolved", str(exc))

    @staticmethod
    def _explicit_headline_company(headline: str) -> str:
        match = re.search(r"(?:\bat\b|@)\s*([^|,]+)", " ".join(headline.split()), re.I)
        return match.group(1).strip(" -") if match else ""
