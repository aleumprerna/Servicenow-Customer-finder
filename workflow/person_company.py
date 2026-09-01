from __future__ import annotations

from dataclasses import dataclass

from clients.apollo import (
    ApolloClient,
    ApolloCurrentCompanyUnavailableError,
    ApolloError,
    CurrentEmploymentEvidence,
)
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
        # Headline and supplied company remain stored as report context, but the
        # resolver deliberately uses only the person's name, LinkedIn URL, and
        # Apollo's own organization/employment response.
        del supplied_company_name, headline
        try:
            organization = self.apollo.person_company(linkedin_url, person_name)
            matching_current = self._matching_current_employments(
                organization.name,
                organization.organization_id,
                organization.current_employments,
            )
            strong_current_evidence = any(
                job.organization_name and job.start_date for job in matching_current
            )
            if not organization.profile_matched:
                return CompanyResolution(
                    "",
                    "apollo_profile_conflict",
                    error=(
                        f"Rejected Apollo candidate {organization.name!r}: Apollo returned a "
                        "different LinkedIn profile"
                    ),
                )
            if organization.selection_source != "primary_organization":
                source_label = (
                    "a named current employment entry"
                    if organization.selection_source == "current_employment"
                    else "the most recent named employment-history entry"
                )
                return CompanyResolution(
                    organization.name,
                    "apollo_employment_history_fallback",
                    error=(
                        "Apollo's primary organization name was null; used "
                        f"{source_label} {organization.name!r}. This may not be the current "
                        "employer and requires manual confirmation"
                    ),
                    domain=organization.domain,
                    company_linkedin_url=organization.linkedin_url,
                )
            if strong_current_evidence:
                return CompanyResolution(
                    organization.name,
                    "apollo_structurally_verified",
                    domain=organization.domain,
                    company_linkedin_url=organization.linkedin_url,
                )
            return CompanyResolution(
                organization.name,
                "apollo_reported_current",
                error=(
                    "Apollo reported this company, but its current-employment evidence was "
                    "incomplete: the matching employment entry needs both an organization name "
                    "and a start date. Confirm it before use"
                ),
                domain=organization.domain,
                company_linkedin_url=organization.linkedin_url,
            )
        except ApolloCurrentCompanyUnavailableError as exc:
            return CompanyResolution("", "apollo_current_company_unavailable", str(exc))
        except ApolloError as exc:
            return CompanyResolution("", "unresolved", str(exc))

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
