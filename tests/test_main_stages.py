from types import SimpleNamespace

import pytest

import main
from clients.apollo import ApolloNoMatchError
from models.company import CheckStatus


class FakeCSVService:
    def selected_indices(self, *, force: bool, company: str | None, limit: int | None) -> list[int]:
        assert force is True
        assert company is None
        assert limit is None
        return [0]


def args(*, enrich_only: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        force=True,
        company=None,
        limit=None,
        enrich_only=enrich_only,
    )


def settings() -> SimpleNamespace:
    return SimpleNamespace(input_csv="input.csv", output_csv="output.csv")


@pytest.mark.asyncio
async def test_apollo_failure_uses_grounded_ai_headquarters_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CSV:
        updates: list[dict[str, object]] = []

        def record(self, _index: int) -> SimpleNamespace:
            return SimpleNamespace(
                company_name="Maersk Oil",
                linkedin_url="",
                domain="",
                country_override="",
                checked_now=lambda: "2026-09-08T00:00:00+00:00",
            )

        def update(self, _index: int, **values: object) -> None:
            self.updates.append(values)

        def save(self) -> None:
            pass

    class Apollo:
        def enrich(self, *_args: object) -> None:
            raise ApolloNoMatchError("multiple organizations")

    monkeypatch.setattr(main, "build_apollo_client", lambda _settings: Apollo())
    monkeypatch.setattr(
        main,
        "resolve_company_headquarters",
        lambda *_args, **_kwargs: {
            "success": True,
            "headquarters": "Copenhagen, Capital Region",
            "country": "Denmark",
            "country_code": "DK",
        },
    )
    stage_settings = SimpleNamespace(
        llm_api_key="key",
        llm_base_url="https://example.test",
        llm_model="model",
        llm_provider="gemini",
        delay_between_companies_seconds=0,
    )
    csv_service = CSV()

    await main.enrich_indices(csv_service, [0], stage_settings)  # type: ignore[arg-type]

    assert csv_service.updates[-1]["headquarters"] == "Copenhagen, Capital Region"
    assert csv_service.updates[-1]["country_code"] == "DK"
    assert csv_service.updates[-1]["check_status"] == CheckStatus.AI_SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize("enrich_only", [True, False])
async def test_cli_runs_company_enrichment(monkeypatch, enrich_only):
    calls = []
    monkeypatch.setattr(main, "CSVService", lambda *_args: FakeCSVService())

    async def enrich(*_args):
        calls.append("enrich")

    monkeypatch.setattr(main, "enrich_indices", enrich)
    assert await main.run(args(enrich_only=enrich_only), settings()) == 0
    assert calls == ["enrich"]
