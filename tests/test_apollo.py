from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from clients.apollo import (
    ApolloClient,
    ApolloCurrentCompanyUnavailableError,
    ApolloNoMatchError,
    linkedin_profile_matches,
    normalize_url,
)


class FakeSession:
    def __init__(self, responses: list[requests.Response]) -> None:
        self.headers: dict[str, str] = {}
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def response(status: int, payload: dict[str, Any]) -> requests.Response:
    item = requests.Response()
    item.status_code = status
    item.url = "https://api.apollo.io/test"
    item._content = json.dumps(payload).encode("utf-8")
    item.headers["content-type"] = "application/json"
    return item


def client(session: FakeSession, retries: int = 3) -> ApolloClient:
    return ApolloClient(
        api_key="not-a-real-key",
        base_url="https://api.apollo.io/api/v1",
        timeout_seconds=1,
        max_retries=retries,
        match_threshold=80,
        session=session,  # type: ignore[arg-type]
    )


def test_linkedin_url_normalization() -> None:
    assert normalize_url("http://www.linkedin.com/company/Microsoft/?trk=test") == (
        "https://linkedin.com/company/microsoft"
    )
    assert normalize_url("https://in.linkedin.com/in/Ada/") == "https://linkedin.com/in/ada"
    assert normalize_url("https://de.linkedin.com/in/Annett-Hufe/en") == (
        "https://linkedin.com/in/annett-hufe"
    )
    assert linkedin_profile_matches(
        "https://linkedin.com/in/prakhar-s-42bbb310a",
        "https://linkedin.com/in/prakhar-singh-42bbb310a",
    )
    assert linkedin_profile_matches(
        "https://linkedin.com/in/regitze-reeh",
        "https://linkedin.com/in/regitze-reeh-899193",
    )


def test_direct_enrichment_uses_linkedin_and_name() -> None:
    session = FakeSession(
        [
            response(
                200,
                {
                    "organization": {
                        "name": "Microsoft Corporation",
                        "linkedin_url": "https://linkedin.com/company/microsoft",
                        "country": "United States",
                        "city": "Redmond",
                        "state": "Washington",
                    }
                },
            )
        ]
    )
    result = client(session).enrich(
        "Microsoft", "https://www.linkedin.com/company/microsoft"
    )
    assert result.country_code == "US"
    assert result.headquarters == "Redmond, Washington, United States"
    assert session.calls[0][2]["params"]["name"] == "Microsoft"
    assert "linkedin_url" in session.calls[0][2]["params"]


def test_temporary_http_failure_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clients.apollo.time.sleep", lambda _seconds: None)
    session = FakeSession(
        [
            response(503, {}),
            response(
                200,
                {
                    "organization": {
                        "name": "Adobe Inc",
                        "country": "United States",
                    }
                },
            ),
        ]
    )
    result = client(session).enrich("Adobe", "https://linkedin.com/company/adobe")
    assert result.company_name == "Adobe Inc"
    assert len(session.calls) == 2


def test_name_only_enrichment_uses_organization_search() -> None:
    session = FakeSession(
        [
            response(
                200,
                {"organizations": [{"name": "Adobe", "country": "United States"}]},
            )
        ]
    )
    result = client(session).enrich("Adobe")
    assert result.country_code == "US"
    assert session.calls[0][1].endswith("/mixed_companies/search")


def test_search_candidate_without_country_is_enriched_by_domain() -> None:
    session = FakeSession(
        [
            response(200, {"organizations": [{"name": "Adobe", "primary_domain": "adobe.com"}]}),
            response(200, {"organization": {"name": "Adobe", "country": "United States"}}),
        ]
    )
    result = client(session).enrich("Adobe")
    assert result.country_code == "US"
    assert session.calls[1][2]["params"]["domain"] == "adobe.com"


def test_person_company_uses_person_linkedin_url_and_returns_organization_identifiers() -> None:
    session = FakeSession(
        [
            response(
                200,
                {"person": {
                    "linkedin_url": "https://www.linkedin.com/in/ada-lovelace",
                    "headline": "Engineer at Adobe",
                    "organization_id": "org-1",
                    "organization": {"id": "org-1", "name": "Adobe", "primary_domain": "adobe.com",
                    "linkedin_url": "https://www.linkedin.com/company/adobe/"},
                    "employment_history": [{
                        "current": True,
                        "organization_id": "org-1",
                        "organization_name": "Adobe",
                        "title": "Engineer",
                        "start_date": "2024-01-01",
                    }] }},
            )
        ]
    )
    result = client(session).person_company("https://www.linkedin.com/in/ada-lovelace", "Ada")
    assert result.name == "Adobe"
    assert result.domain == "adobe.com"
    assert result.linkedin_url == "https://linkedin.com/company/adobe"
    assert result.organization_id == "org-1"
    assert result.person_headline == "Engineer at Adobe"
    assert result.current_employments[0].organization_name == "Adobe"
    assert result.current_employments[0].start_date == "2024-01-01"
    assert session.calls[0][2]["params"]["linkedin_url"] == "https://linkedin.com/in/ada-lovelace"


def test_person_company_marks_a_different_profile_as_unverified() -> None:
    session = FakeSession(
        [response(200, {"person": {
            "linkedin_url": "https://linkedin.com/in/someone-else",
            "organization": {"name": "Wrong Company"},
        }})]
    )
    result = client(session).person_company("https://linkedin.com/in/ada-lovelace", "Ada")
    assert result.name == "Wrong Company"
    assert result.profile_matched is False


def test_person_company_uses_single_current_employment_when_primary_is_missing() -> None:
    session = FakeSession(
        [response(200, {"person": {
            "linkedin_url": "https://linkedin.com/in/ada-lovelace",
            "employment_history": [{
                "current": True,
                "organization_id": "org-1",
                "organization_name": "Current Company",
            }],
        }})]
    )
    result = client(session).person_company("https://linkedin.com/in/ada-lovelace", "Ada")
    assert result.name == "Current Company"


def test_person_company_fetches_complete_record_when_match_is_partial() -> None:
    session = FakeSession(
        [
            response(200, {"person": {
                "id": "person-1",
                "name": "Ada Lovelace",
                "linkedin_url": "https://linkedin.com/in/ada-lovelace",
            }}),
            response(200, {"person": {
                "id": "person-1",
                "name": "Ada Lovelace",
                "linkedin_url": "https://linkedin.com/in/ada-lovelace",
                "organization": {"name": "Complete Company", "primary_domain": "complete.test"},
            }}),
        ]
    )
    result = client(session).person_company("https://linkedin.com/in/ada-lovelace", "Ada Lovelace")
    assert result.name == "Complete Company"
    assert result.domain == "complete.test"
    assert session.calls[1][0] == "GET"
    assert session.calls[1][1].endswith("/people/person-1")


def test_person_company_reports_matched_person_without_current_employer() -> None:
    session = FakeSession(
        [
            response(200, {"person": {
                "id": "person-1",
                "name": "Raymond Moore",
                "linkedin_url": "https://linkedin.com/in/raymond-moore-901b7551",
                "organization_id": None,
            }}),
            response(200, {"person": {
                "id": "person-1",
                "name": "Raymond Moore",
                "linkedin_url": "https://linkedin.com/in/raymond-moore-901b7551",
                "organization_id": None,
                "employment_history": [
                    {"organization_name": "Past Company", "current": False}
                ],
            }}),
        ]
    )
    with pytest.raises(ApolloCurrentCompanyUnavailableError, match="organization_id is empty"):
        client(session).person_company(
            "https://linkedin.com/in/raymond-moore-901b7551", "Raymond Moore"
        )


def test_unrelated_organizations_are_rejected() -> None:
    session = FakeSession(
        [
            response(200, {"organization": {"name": "Unrelated Company", "country": "US"}}),
            response(
                200,
                {"organizations": [{"name": "Another Business", "country": "US"}]},
            ),
        ]
    )
    with pytest.raises(ApolloNoMatchError):
        client(session).enrich("Microsoft", "https://linkedin.com/company/microsoft")
