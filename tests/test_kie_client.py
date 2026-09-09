from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from services.kie_client import KieAPIError, KieClient


def _event(event: dict[str, object]) -> str:
    return "data: " + json.dumps(event)


def test_kie_client_consumes_streamed_responses_output() -> None:
    final_response = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "KIE_OK",
                        "annotations": [{"url": "https://example.com/source"}],
                    }
                ],
            }
        ],
    }
    response = MagicMock(status_code=200)
    response.iter_lines.return_value = iter(
        [
            "event: response.output_text.delta",
            _event({"type": "response.output_text.delta", "delta": "KIE_"}),
            _event({"type": "response.output_text.delta", "delta": "OK"}),
            _event({"type": "response.output_text.done", "text": "KIE_OK"}),
            _event({"type": "response.completed", "response": final_response}),
            "data: [DONE]",
        ]
    )
    session = MagicMock()
    session.post.return_value = response

    result = KieClient(
        "kie-test",
        base_url="https://api.kie.ai/codex/v1",
        timeout_seconds=42,
        session=session,
    ).responses.create(
        model="gpt-6-astra",
        input="Say KIE_OK",
        reasoning={"effort": "high"},
    )

    assert result.output_text == "KIE_OK"
    assert result.model_dump() == final_response
    call = session.post.call_args
    assert call.args[0] == "https://api.kie.ai/codex/v1/responses"
    assert call.kwargs["json"]["stream"] is True
    assert call.kwargs["json"]["model"] == "gpt-6-astra"
    assert call.kwargs["headers"]["Authorization"] == "Bearer kie-test"
    assert call.kwargs["timeout"] == 42
    response.close.assert_called_once()


def test_kie_client_requires_api_key() -> None:
    with pytest.raises(KieAPIError, match="KIE_API_KEY"):
        KieClient("")


def test_kie_client_reports_stream_error_message() -> None:
    response = MagicMock(status_code=200)
    response.iter_lines.return_value = iter(
        [
            "event: error",
            _event(
                {
                    "type": "error",
                    "code": "upstream_error",
                    "message": "The upstream search service is unavailable.",
                }
            ),
        ]
    )
    session = MagicMock()
    session.post.return_value = response

    with pytest.raises(KieAPIError, match="upstream search service is unavailable"):
        KieClient("kie-test", session=session).responses.create(
            model="gpt-6-astra", input="Search the web", tools=[{"type": "web_search"}]
        )

    response.close.assert_called_once()
