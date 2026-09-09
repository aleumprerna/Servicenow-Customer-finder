from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests


class KieAPIError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KieResponse:
    output_text: str
    payload: dict[str, Any]

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return self.payload


class _KieResponses:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout_seconds: float,
        session: requests.Session,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session

    def create(self, **request: Any) -> KieResponse:
        payload = dict(request)
        payload["stream"] = True
        try:
            response = self.session.post(
                f"{self.base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
                timeout=self.timeout_seconds,
                stream=True,
            )
        except requests.RequestException as exc:
            raise KieAPIError(f"KIE API request failed: {exc}") from exc

        try:
            if response.status_code >= 400:
                raise KieAPIError(self._http_error(response))
            return self._read_events(response)
        finally:
            response.close()

    @staticmethod
    def _http_error(response: requests.Response) -> str:
        message = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or error.get("code") or "").strip()
                message = message or str(body.get("message") or body.get("msg") or "").strip()
        except ValueError:
            message = str(response.text or "").strip()[:500]
        detail = message or response.reason or "request failed"
        return f"KIE API returned HTTP {response.status_code}: {detail}"

    @classmethod
    def _read_events(cls, response: requests.Response) -> KieResponse:
        deltas: list[str] = []
        completed_text = ""
        final_payload: dict[str, Any] = {}
        for raw_line in response.iter_lines(decode_unicode=True):
            line = str(raw_line or "").strip()
            if not line.startswith("data:"):
                continue
            encoded = line[5:].strip()
            if not encoded or encoded == "[DONE]":
                continue
            try:
                event = json.loads(encoded)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "response.output_text.delta":
                deltas.append(str(event.get("delta") or ""))
            elif event_type == "response.output_text.done":
                completed_text = str(event.get("text") or "")
            elif event_type == "response.completed":
                candidate = event.get("response")
                if isinstance(candidate, dict):
                    final_payload = candidate
            elif event_type in {"error", "response.failed"}:
                error = event.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or error.get("code") or "").strip()
                else:
                    candidate = event.get("response")
                    error = candidate.get("error") if isinstance(candidate, dict) else None
                    message = (
                        str(error.get("message") or error.get("code") or "").strip()
                        if isinstance(error, dict)
                        else ""
                    )
                message = message or str(event.get("message") or event.get("code") or event_type)
                raise KieAPIError(f"KIE API stream failed: {message}")

        output_text = completed_text or "".join(deltas)
        if not output_text:
            output_text = cls._payload_text(final_payload)
        if not output_text:
            raise KieAPIError("KIE API completed without a text response.")
        return KieResponse(output_text=output_text.strip(), payload=final_payload)

    @staticmethod
    def _payload_text(payload: dict[str, Any]) -> str:
        texts: list[str] = []
        output = payload.get("output")
        if not isinstance(output, list):
            return ""
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    texts.append(str(part.get("text") or ""))
        return "\n".join(texts).strip()


class KieClient:
    """Responses-compatible KIE client that consumes its SSE-only response body."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.kie.ai/codex/v1",
        timeout_seconds: float = 180.0,
        session: requests.Session | None = None,
    ) -> None:
        if not str(api_key or "").strip():
            raise KieAPIError("KIE_API_KEY is required.")
        self.responses = _KieResponses(
            api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            session=session or requests.Session(),
        )
