from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.ai_company_resolver import (
    extract_company_from_headline,
    resolve_company_from_web,
)


def test_extract_company_from_headline() -> None:
    assert (
        extract_company_from_headline("Head of Corporate Affairs at Harbour Energy")
        == "Harbour Energy"
    )
    assert (
        extract_company_from_headline("Software Engineer @ Google | Python enthusiast")
        == "Google"
    )
    assert (
        extract_company_from_headline("Chief Executive Officer at Acme Corp.")
        == "Acme Corp"
    )
    assert extract_company_from_headline("") == ""
    assert extract_company_from_headline("Looking for opportunities") == ""


def test_resolve_company_from_web_openai_success() -> None:
    mock_response = MagicMock()
    mock_response.output_text = (
        '{"company_name": "Harbour Energy", "confidence": "high", "reason": "Verified via LinkedIn"}'
    )

    mock_client = MagicMock()
    mock_client.responses.create.return_value = mock_response

    with patch("openai.OpenAI", return_value=mock_client):
        result = resolve_company_from_web(
            person_name="Regitze Reeh",
            linkedin_url="https://linkedin.com/in/regitze-reeh",
            headline="Head of Corporate Affairs at Harbour Energy",
            api_key="sk-fake-key",
        )

    assert result["success"] is True
    assert result["company_name"] == "Harbour Energy"
    assert result["confidence"] == "high"
    assert result["source"] == "openai_web_search"


def test_resolve_company_from_web_fallback_on_openai_error() -> None:
    with patch("openai.OpenAI", side_effect=Exception("API connection timeout")):
        result = resolve_company_from_web(
            person_name="Regitze Reeh",
            linkedin_url="https://linkedin.com/in/regitze-reeh",
            headline="Head of Corporate Affairs at Harbour Energy",
            api_key="sk-fake-key",
        )

    assert result["success"] is True
    assert result["company_name"] == "Harbour Energy"
    assert result["source"] == "headline_fallback"


def test_resolve_company_from_web_unresolved() -> None:
    with patch("openai.OpenAI", side_effect=Exception("API failure")):
        result = resolve_company_from_web(
            person_name="Jane Doe",
            linkedin_url="https://linkedin.com/in/janedoe",
            headline="Exploring new horizons",
            api_key="sk-fake-key",
        )

    assert result["success"] is False
    assert result["company_name"] == ""
    assert "error" in result
