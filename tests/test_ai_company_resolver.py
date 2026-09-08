from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.gemini_client import GeminiResult
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
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o",
        )

    assert result["success"] is True
    assert result["company_name"] == "Harbour Energy"
    assert result["confidence"] == "high"
    assert result["source"] == "openai_web_search"


def test_resolve_company_with_glm_chat_completions() -> None:
    mock_response = MagicMock()
    mock_response.choices = [
        MagicMock(
            message=MagicMock(
                content=(
                    '{"company_name":"Harbour Energy","confidence":"high",'
                    '"reason":"The supplied headline names the employer."}'
                )
            )
        )
    ]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("openai.OpenAI", return_value=mock_client) as client_class:
        result = resolve_company_from_web(
            person_name="Regitze Reeh",
            linkedin_url="https://linkedin.com/in/regitze-reeh",
            headline="Head of Corporate Affairs at Harbour Energy",
            api_key="glm-fake-key",
            provider="glm",
            base_url="https://api.tokenrouter.com/v1",
            model="z-ai/glm-5.3",
        )

    client_class.assert_called_once_with(
        api_key="glm-fake-key", base_url="https://api.tokenrouter.com/v1"
    )
    call = mock_client.chat.completions.create.call_args.kwargs
    assert call["model"] == "z-ai/glm-5.3"
    assert result["success"] is True
    assert result["source"] == "glm_profile_context"


def test_resolve_company_with_gemini_grounded_search() -> None:
    gemini = MagicMock()
    gemini.generate.return_value = GeminiResult(
        text=(
            '{"company_name":"Harbour Energy","headquarters":"Aberdeen, Scotland",'
            '"country":"United Kingdom","country_code":"GB",'
            '"company_domain":"harbourenergy.com",'
            '"company_linkedin_url":"https://linkedin.com/company/harbour-energy",'
            '"confidence":"high",'
            '"reason":"Current employer confirmed by search."}'
        ),
        source_urls={"https://example.com/source"},
    )

    with patch("services.ai_company_resolver.GeminiClient", return_value=gemini) as client_class:
        result = resolve_company_from_web(
            person_name="Regitze Reeh",
            linkedin_url="https://linkedin.com/in/regitze-reeh",
            headline="Head of Corporate Affairs at Harbour Energy",
            api_key="gemini-fake-key",
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-3-flash-preview",
        )

    client_class.assert_called_once_with(
        "gemini-fake-key",
        model="gemini-3-flash-preview",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        max_retries=4,
        retry_base_seconds=2.0,
    )
    gemini.generate.assert_called_once()
    assert gemini.generate.call_args.kwargs == {
        "use_google_search": True,
        "use_url_context": True,
    }
    assert result["success"] is True
    assert result["source"] == "gemini_google_search"
    assert result["headquarters"] == "Aberdeen, Scotland"
    assert result["country"] == "United Kingdom"
    assert result["country_code"] == "GB"
    assert result["company_domain"] == "harbourenergy.com"


def test_suggest_company_does_not_approve_gemini_result_without_headquarters() -> None:
    gemini = MagicMock()
    gemini.generate.return_value = GeminiResult(
        text=(
            '{"company_name":"Harbour Energy","headquarters":"",'
            '"country":"","country_code":"","confidence":"medium"}'
        ),
        source_urls={"https://example.com/source"},
    )

    with patch("services.ai_company_resolver.GeminiClient", return_value=gemini):
        result = resolve_company_from_web(
            person_name="Regitze Reeh",
            linkedin_url="https://linkedin.com/in/regitze-reeh",
            headline="Head of Corporate Affairs at Harbour Energy",
            api_key="gemini-fake-key",
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-3-flash-preview",
            require_headquarters=True,
            require_grounding=True,
        )

    assert result["success"] is False
    assert "headquarters" in result["error"]


def test_suggest_company_accepts_source_urls_returned_inside_gemini_json() -> None:
    gemini = MagicMock()
    gemini.generate.return_value = GeminiResult(
        text=(
            '{"company_name":"SKF India Ltd.","headquarters":"Pune, Maharashtra",'
            '"country":"India","country_code":"IN","company_domain":"skf.com",'
            '"company_linkedin_url":"https://linkedin.com/company/skf-india",'
            '"source_urls":["https://www.skf.com/in/about-skf-india"],'
            '"confidence":"high","reason":"Official SKF source."}'
        ),
        source_urls=set(),
    )

    with patch("services.ai_company_resolver.GeminiClient", return_value=gemini):
        result = resolve_company_from_web(
            person_name="Example Person",
            linkedin_url="https://linkedin.com/in/example-person",
            api_key="gemini-fake-key",
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-3-flash-preview",
            require_headquarters=True,
            require_grounding=True,
        )

    assert result["success"] is True
    assert result["company_name"] == "SKF India Ltd."
    assert result["headquarters"] == "Pune, Maharashtra"
    assert result["source_urls"] == ["https://www.skf.com/in/about-skf-india"]


def test_resolve_company_from_web_fallback_on_openai_error() -> None:
    with patch("openai.OpenAI", side_effect=Exception("API connection timeout")):
        result = resolve_company_from_web(
            person_name="Regitze Reeh",
            linkedin_url="https://linkedin.com/in/regitze-reeh",
            headline="Head of Corporate Affairs at Harbour Energy",
            api_key="sk-fake-key",
            provider="openai",
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
            provider="openai",
        )

    assert result["success"] is False
    assert result["company_name"] == ""
    assert "error" in result
