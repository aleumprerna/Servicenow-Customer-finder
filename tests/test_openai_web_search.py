from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clients.openai_web_search import OpenAIWebSearchClient, WebSearchError


class FakeResponses:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(self.payload))


def fake_client(payload: dict[str, object]) -> tuple[OpenAIWebSearchClient, FakeResponses]:
    responses = FakeResponses(payload)
    sdk = SimpleNamespace(responses=responses)
    return OpenAIWebSearchClient(api_key="test", client=sdk), responses


def test_person_company_uses_responses_web_search_and_structured_output() -> None:
    client, responses = fake_client(
        {
            "company_name": "Example Corp",
            "domain": "example.com",
            "company_linkedin_url": "https://linkedin.com/company/example",
            "headquarters_city": "",
            "headquarters_state": "",
            "headquarters_country": "",
            "confidence": 91,
        }
    )
    result = client.person_company("https://linkedin.com/in/ada", "Ada Lovelace")
    assert result.name == "Example Corp"
    call = responses.calls[0]
    assert call["tools"] == [{"type": "web_search"}]
    assert call["tool_choice"] == {"type": "web_search"}
    assert call["model"] == "gpt-5.4-mini"
    assert call["text"]["format"]["type"] == "json_schema"  # type: ignore[index]


def test_company_enrichment_normalizes_headquarters_country() -> None:
    client, _ = fake_client(
        {
            "company_name": "Microsoft Corporation",
            "domain": "microsoft.com",
            "company_linkedin_url": "https://linkedin.com/company/microsoft",
            "headquarters_city": "Redmond",
            "headquarters_state": "Washington",
            "headquarters_country": "United States",
            "confidence": 96,
        }
    )
    result = client.enrich("Microsoft", domain="microsoft.com")
    assert result.country_code == "US"
    assert result.headquarters == "Redmond, Washington, United States"
    assert result.match_score == 100


def test_low_confidence_result_is_rejected() -> None:
    client, _ = fake_client(
        {
            "company_name": "Maybe Corp",
            "domain": "",
            "company_linkedin_url": "",
            "headquarters_city": "",
            "headquarters_state": "",
            "headquarters_country": "",
            "confidence": 40,
        }
    )
    with pytest.raises(WebSearchError):
        client.person_company("https://linkedin.com/in/ada", "Ada")
