from __future__ import annotations

from types import SimpleNamespace

from services.ai_metrics import (
    AIPricing,
    estimated_cost_usd,
    extract_token_usage,
    monitored_ai_call,
)
from services.gemini_client import GeminiResult
from workflow.database import WorkflowDatabase


def test_extracts_responses_usage_and_estimates_cost() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(
            model_dump=lambda: {
                "input_tokens": 1_000,
                "input_tokens_details": {"cached_tokens": 400},
                "output_tokens": 200,
                "output_tokens_details": {"reasoning_tokens": 50},
                "total_tokens": 1_200,
            }
        )
    )
    usage = extract_token_usage(response)
    assert usage.input_tokens == 1_000
    assert usage.cached_input_tokens == 400
    assert usage.output_tokens == 200
    assert usage.reasoning_tokens == 50
    assert estimated_cost_usd(
        usage,
        AIPricing(
            input_per_million=2.0,
            cached_input_per_million=1.0,
            output_per_million=8.0,
            web_search_per_call=0.01,
        ),
        web_search_calls=1,
    ) == 0.0132


def test_extracts_gemini_usage_including_thinking_tokens() -> None:
    response = GeminiResult(
        text="{}",
        source_urls=set(),
        usage_metadata={
            "promptTokenCount": 100,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 30,
            "totalTokenCount": 150,
        },
    )
    usage = extract_token_usage(response)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.reasoning_tokens == 30
    assert usage.total_tokens == 150


def test_database_aggregates_ai_metrics_by_run_and_record(tmp_path) -> None:
    database = WorkflowDatabase(tmp_path / "workflow.db")
    database.initialize()
    run_id = database.create_run(
        "people.csv",
        [{"person_name": "Ada", "linkedin_url": "https://example.test/ada"}],
    )
    person_id = int(database.people_for_run(run_id)[0]["id"])
    database.record_ai_metric(
        run_id=run_id,
        person_id=person_id,
        operation="company_resolution",
        provider="openai",
        model="test-model",
        started_at="2026-01-01T00:00:00+00:00",
        latency_ms=1250,
        input_tokens=100,
        output_tokens=25,
        total_tokens=125,
        estimated_cost_usd=0.002,
        status="completed",
    )
    database.record_ai_metric(
        run_id=run_id,
        person_id=person_id,
        operation="deep_research.classification",
        provider="openai",
        model="test-model",
        started_at="2026-01-01T00:00:02+00:00",
        latency_ms=750,
        input_tokens=50,
        output_tokens=10,
        total_tokens=60,
        estimated_cost_usd=0.001,
        status="completed",
    )

    summary = database.ai_metrics_summary(run_id)
    assert summary["call_count"] == 2
    assert summary["total_tokens"] == 185
    assert summary["model_latency_ms"] == 2000
    assert summary["estimated_cost_usd"] == 0.003
    assert summary["records"][0]["person_name"] == "Ada"
    assert len(database.ai_metrics(run_id)) == 2


def test_telemetry_failure_does_not_fail_model_call() -> None:
    result = monitored_ai_call(
        lambda: {"usage": {"input_tokens": 1, "output_tokens": 1}},
        operation="test",
        provider="test",
        model="test",
        callback=lambda _event: (_ for _ in ()).throw(RuntimeError("database locked")),
    )
    assert result["usage"]["input_tokens"] == 1
