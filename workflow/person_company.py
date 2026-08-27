from __future__ import annotations

from dataclasses import dataclass

from clients.apollo import ApolloClient, ApolloError


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
        try:
            organization = self.apollo.person_company(linkedin_url, person_name)
            return CompanyResolution(
                organization.name,
                "apollo_person_match",
                domain=organization.domain,
                company_linkedin_url=organization.linkedin_url,
            )
        except ApolloError as exc:
            return CompanyResolution("", "unresolved", str(exc))
