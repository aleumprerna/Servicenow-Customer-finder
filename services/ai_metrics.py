from __future__ import annotations

import time
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar


T = TypeVar("T")
MetricCallback = Callable[[dict[str, Any]], None]
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AIPricing:
    """Optional USD rates. Token rates are expressed per one million tokens."""

    input_per_million: float | None = None
    cached_input_per_million: float | None = None
    output_per_million: float | None = None
    web_search_per_call: float | None = None

    @property
    def configured(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input_per_million,
                self.cached_input_per_million,
                self.output_per_million,
                self.web_search_per_call,
            )
        )


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


def pricing_from_settings(settings: Any) -> AIPricing:
    return AIPricing(
        input_per_million=getattr(settings, "llm_input_cost_per_million", None),
        cached_input_per_million=getattr(
            settings, "llm_cached_input_cost_per_million", None
        ),
        output_per_million=getattr(settings, "llm_output_cost_per_million", None),
        web_search_per_call=getattr(settings, "llm_web_search_cost_per_call", None),
    )


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        candidate = dump()
        return candidate if isinstance(candidate, dict) else {}
    return {}


def _integer(mapping: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
    return 0


def extract_token_usage(response: Any) -> TokenUsage:
    """Normalize Responses, Chat Completions, KIE, and Gemini usage metadata."""

    payload = _payload(response)
    usage_value = getattr(response, "usage", None)
    usage = _payload(usage_value) or _payload(payload.get("usage"))
    gemini = _payload(getattr(response, "usage_metadata", None)) or _payload(
        payload.get("usageMetadata")
    )
    if gemini:
        input_tokens = _integer(gemini, "promptTokenCount", "prompt_token_count")
        cached_tokens = _integer(
            gemini, "cachedContentTokenCount", "cached_content_token_count"
        )
        candidate_tokens = _integer(
            gemini, "candidatesTokenCount", "candidates_token_count"
        )
        reasoning_tokens = _integer(gemini, "thoughtsTokenCount", "thoughts_token_count")
        output_tokens = candidate_tokens + reasoning_tokens
        total_tokens = _integer(gemini, "totalTokenCount", "total_token_count")
        return TokenUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens or input_tokens + output_tokens,
        )

    input_tokens = _integer(usage, "input_tokens", "prompt_tokens")
    output_tokens = _integer(usage, "output_tokens", "completion_tokens")
    input_details = _payload(usage.get("input_tokens_details")) or _payload(
        usage.get("prompt_tokens_details")
    )
    output_details = _payload(usage.get("output_tokens_details")) or _payload(
        usage.get("completion_tokens_details")
    )
    cached_tokens = _integer(input_details, "cached_tokens")
    reasoning_tokens = _integer(output_details, "reasoning_tokens")
    total_tokens = _integer(usage, "total_tokens")
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens or input_tokens + output_tokens,
    )


def estimated_cost_usd(
    usage: TokenUsage, pricing: AIPricing, *, web_search_calls: int = 0
) -> float | None:
    if not pricing.configured:
        return None
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    input_rate = pricing.input_per_million or 0.0
    cached_rate = (
        pricing.cached_input_per_million
        if pricing.cached_input_per_million is not None
        else input_rate
    )
    output_rate = pricing.output_per_million or 0.0
    search_rate = pricing.web_search_per_call or 0.0
    return round(
        (uncached * input_rate / 1_000_000)
        + (usage.cached_input_tokens * cached_rate / 1_000_000)
        + (usage.output_tokens * output_rate / 1_000_000)
        + (max(0, web_search_calls) * search_rate),
        10,
    )


def _emit(callback: MetricCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception as exc:  # Telemetry must never fail the business operation.
        LOGGER.warning("Could not persist AI telemetry: %s", exc)


def monitored_ai_call(
    call: Callable[[], T],
    *,
    operation: str,
    provider: str,
    model: str,
    callback: MetricCallback | None = None,
    pricing: AIPricing | None = None,
    web_search_calls: int = 0,
) -> T:
    """Measure a model call and emit one normalized, non-sensitive metric event."""

    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        response = call()
    except Exception as exc:
        if callback:
            _emit(
                callback,
                {
                    "operation": operation,
                    "provider": provider,
                    "model": model,
                    "started_at": started_wall.isoformat(timespec="milliseconds"),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "web_search_calls": 0,
                    "estimated_cost_usd": None,
                    "status": "failed",
                    "error": " ".join(str(exc).split())[:1000],
                },
            )
        raise

    usage = extract_token_usage(response)
    if callback:
        event = asdict(usage)
        event.update(
            {
                "operation": operation,
                "provider": provider,
                "model": model,
                "started_at": started_wall.isoformat(timespec="milliseconds"),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "web_search_calls": max(0, web_search_calls),
                "estimated_cost_usd": estimated_cost_usd(
                    usage, pricing or AIPricing(), web_search_calls=web_search_calls
                ),
                "status": "completed",
                "error": "",
            }
        )
        _emit(callback, event)
    return response
