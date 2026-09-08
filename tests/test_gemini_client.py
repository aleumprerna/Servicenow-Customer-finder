from unittest.mock import MagicMock, patch

import pytest

from services.gemini_client import GeminiClient, GeminiRateLimitError


def test_gemini_client_uses_search_and_url_context_and_returns_sources() -> None:
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {
        "candidates": [
            {
                "content": {"parts": [{"text": '{"company_name":"Example"}'}]},
                "groundingMetadata": {
                    "groundingChunks": [{"web": {"uri": "https://example.com/about"}}]
                },
            }
        ]
    }
    session = MagicMock()
    session.post.return_value = response
    client = GeminiClient(
        "gemini-test",
        model="gemini-3-flash-preview",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        session=session,
    )

    result = client.generate(
        "Find the company", use_google_search=True, use_url_context=True
    )

    request = session.post.call_args
    assert request.args[0].endswith(
        "/models/gemini-3-flash-preview:generateContent"
    )
    assert request.kwargs["headers"]["x-goog-api-key"] == "gemini-test"
    assert request.kwargs["json"]["tools"] == [
        {"google_search": {}},
        {"url_context": {}},
    ]
    assert "generationConfig" not in request.kwargs["json"]
    assert result.text == '{"company_name":"Example"}'
    assert result.source_urls == {"https://example.com/about"}


def test_gemini_client_requests_json_for_reasoning_without_tools() -> None:
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": '{"status":"ok"}'}]}}]
    }
    session = MagicMock()
    session.post.return_value = response
    client = GeminiClient(
        "gemini-test",
        model="gemini-3-flash-preview",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        session=session,
    )

    client.generate("Classify")

    assert session.post.call_args.kwargs["json"]["generationConfig"] == {
        "responseMimeType": "application/json"
    }


def test_gemini_client_retries_429_using_google_retry_delay() -> None:
    limited = MagicMock()
    limited.status_code = 429
    limited.headers = {}
    limited.json.return_value = {
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "message": "Please retry in 3s.",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "3s",
                }
            ],
        }
    }
    success = MagicMock()
    success.status_code = 200
    success.headers = {}
    success.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": '{"status":"ok"}'}]}}]
    }
    session = MagicMock()
    session.post.side_effect = [limited, success]
    sleeps: list[float] = []
    client = GeminiClient(
        "gemini-test",
        model="gemini-3-flash-preview",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        session=session,
        sleep=sleeps.append,
    )

    with patch("services.gemini_client.random.uniform", return_value=0):
        result = client.generate("Retry me")

    assert result.text == '{"status":"ok"}'
    assert session.post.call_count == 2
    assert sleeps == [3.0]


def test_gemini_client_does_not_retry_exhausted_daily_quota() -> None:
    limited = MagicMock()
    limited.status_code = 429
    limited.headers = {}
    limited.json.return_value = {
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "message": "Requests per day quota exceeded.",
            "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
        }
    }
    session = MagicMock()
    session.post.return_value = limited
    client = GeminiClient(
        "gemini-test",
        model="gemini-3-flash-preview",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        session=session,
        sleep=MagicMock(),
    )

    with pytest.raises(GeminiRateLimitError, match="Requests per day quota exceeded"):
        client.generate("Do not retry")

    assert session.post.call_count == 1
