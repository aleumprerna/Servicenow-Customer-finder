from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests


class GeminiAPIError(RuntimeError):
    pass


class GeminiRateLimitError(GeminiAPIError):
    pass


@dataclass(frozen=True, slots=True)
class GeminiResult:
    text: str
    source_urls: set[str]


class GeminiClient:
    """Small native Gemini client with Google Search and URL-context support."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str,
        timeout_seconds: float = 180.0,
        max_retries: int = 4,
        retry_base_seconds: float = 2.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not str(api_key or "").strip():
            raise GeminiAPIError("GEMINI_API_KEY is required.")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.session = session or requests.Session()
        self.sleep = sleep

    def generate(
        self,
        prompt: str,
        *,
        use_google_search: bool = False,
        use_url_context: bool = False,
    ) -> GeminiResult:
        tools: list[dict[str, Any]] = []
        if use_google_search:
            tools.append({"google_search": {}})
        if use_url_context:
            tools.append({"url_context": {}})
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        generation_config: dict[str, Any] = {"maxOutputTokens": 8192}
        if self.model.casefold().startswith("gemini-3"):
            generation_config["thinkingConfig"] = {"thinkingLevel": "low"}
            # Gemini 3 supports structured output together with built-in tools.
            generation_config["responseMimeType"] = "application/json"
        if tools:
            payload["tools"] = tools
        else:
            generation_config["responseMimeType"] = "application/json"
        payload["generationConfig"] = generation_config
        response: requests.Response | None = None
        empty_retries = 0
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/models/{self.model}:generateContent",
                    headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise GeminiAPIError(f"Gemini API request failed after retries: {exc}") from exc
                self.sleep(self._retry_delay(attempt, None, None))
                continue

            error = self._error_details(response)
            status = response.status_code
            retryable = status in {408, 429} or 500 <= status <= 599
            quota_exhausted = status == 429 and self._is_non_transient_quota(error)
            if retryable and not quota_exhausted and attempt < self.max_retries:
                self.sleep(self._retry_delay(attempt, response, error))
                continue
            if status >= 400:
                message = self._error_message(error) or response.reason or f"HTTP {status}"
                if status == 429:
                    raise GeminiRateLimitError(
                        f"Gemini quota/rate limit exceeded ({message}). "
                        "Check the project's RPM, token, daily, and billing limits in Google AI Studio."
                    )
                raise GeminiAPIError(f"Gemini API returned HTTP {status}: {message}")
            try:
                data = response.json()
            except ValueError as exc:
                raise GeminiAPIError("Gemini API returned invalid JSON.") from exc
            text = self._response_text(data)
            if text:
                return GeminiResult(text=text, source_urls=self._source_urls(data))
            detail = self._empty_response_detail(data)
            if (
                empty_retries < 1
                and attempt < self.max_retries
                and self._empty_response_is_retryable(data)
            ):
                empty_retries += 1
                self.sleep(self._retry_delay(attempt, response, None))
                continue
            raise GeminiAPIError(f"Gemini returned no text response ({detail}).")
        else:  # pragma: no cover - loop always returns or raises
            raise GeminiAPIError("Gemini API request failed.")

    def _retry_delay(
        self,
        attempt: int,
        response: requests.Response | None,
        error: dict[str, Any] | None,
    ) -> float:
        retry_after = str(response.headers.get("Retry-After") or "") if response else ""
        explicit = self._seconds(retry_after) or self._retry_info_seconds(error)
        base = explicit if explicit is not None else self.retry_base_seconds * (2**attempt)
        return min(60.0, max(0.1, base) + random.uniform(0, min(1.0, base * 0.2)))

    @staticmethod
    def _seconds(value: str) -> float | None:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*s?\s*", value or "")
        return float(match.group(1)) if match else None

    @classmethod
    def _retry_info_seconds(cls, error: dict[str, Any] | None) -> float | None:
        if not error:
            return None
        details = error.get("details")
        if not isinstance(details, list):
            return None
        for detail in details:
            if isinstance(detail, dict) and detail.get("retryDelay"):
                parsed = cls._seconds(str(detail["retryDelay"]))
                if parsed is not None:
                    return parsed
        message = cls._error_message(error)
        match = re.search(r"retry in\s+(\d+(?:\.\d+)?)s", message, re.IGNORECASE)
        return float(match.group(1)) if match else None

    @staticmethod
    def _error_details(response: requests.Response) -> dict[str, Any]:
        if response.status_code < 400:
            return {}
        try:
            payload = response.json()
        except ValueError:
            return {}
        error = payload.get("error") if isinstance(payload, dict) else None
        return error if isinstance(error, dict) else {}

    @staticmethod
    def _error_message(error: dict[str, Any] | None) -> str:
        return str((error or {}).get("message") or (error or {}).get("status") or "").strip()

    @classmethod
    def _is_non_transient_quota(cls, error: dict[str, Any] | None) -> bool:
        text = str(error or "").casefold()
        return any(
            marker in text
            for marker in (
                "perday",
                "per day",
                "daily quota",
                "requests per day",
                "spend-based",
                "limit: 0",
            )
        ) and cls._retry_info_seconds(error) is None

    @staticmethod
    def _response_text(data: dict[str, Any]) -> str:
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return ""
        return "\n".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and part.get("thought") is not True
        ).strip()

    @staticmethod
    def _empty_response_is_retryable(data: dict[str, Any]) -> bool:
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            # No candidates with no explicit prompt block is commonly transient.
            prompt_feedback = data.get("promptFeedback")
            return not isinstance(prompt_feedback, dict) or not prompt_feedback.get("blockReason")
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        finish_reason = str(candidate.get("finishReason") or "").upper()
        return finish_reason in {
            "", "STOP", "MAX_TOKENS", "OTHER", "MALFORMED_RESPONSE", "TOO_MANY_TOOL_CALLS"
        }

    @staticmethod
    def _empty_response_detail(data: dict[str, Any]) -> str:
        details: list[str] = []
        prompt_feedback = data.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            details.append(f"prompt blocked: {prompt_feedback['blockReason']}")
        candidates = data.get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            candidate = candidates[0]
            if candidate.get("finishReason"):
                details.append(f"finishReason={candidate['finishReason']}")
            if candidate.get("finishMessage"):
                details.append(f"finishMessage={candidate['finishMessage']}")
        usage = data.get("usageMetadata")
        if isinstance(usage, dict):
            for key in ("promptTokenCount", "thoughtsTokenCount", "candidatesTokenCount"):
                if key in usage:
                    details.append(f"{key}={usage[key]}")
        return ", ".join(details) or "no candidate or diagnostic metadata"

    @staticmethod
    def _source_urls(data: dict[str, Any]) -> set[str]:
        urls: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if (
                        key in {"uri", "url", "retrievedUrl", "retrieved_url"}
                        and isinstance(item, str)
                        and item.startswith(("http://", "https://"))
                    ):
                        urls.add(item.rstrip("/"))
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(data)
        return urls
