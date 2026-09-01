from types import SimpleNamespace

import pytest

import main


class FakeCSVService:
    def selected_indices(self, *, force: bool, company: str | None, limit: int | None) -> list[int]:
        assert force is True
        assert company is None
        assert limit is None
        return [0]


def args(*, enrich_only: bool = False, automation_only: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        force=True,
        company=None,
        limit=None,
        enrich_only=enrich_only,
        automation_only=automation_only,
    )


def settings() -> SimpleNamespace:
    return SimpleNamespace(input_csv="input.csv", output_csv="output.csv")


@pytest.mark.asyncio
async def test_enrich_only_never_starts_browser_automation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(main, "CSVService", lambda *_args: FakeCSVService())

    async def enrich(*_args: object) -> None:
        calls.append("enrich")

    async def automate(*_args: object) -> int:
        calls.append("automate")
        return 0

    monkeypatch.setattr(main, "enrich_indices", enrich)
    monkeypatch.setattr(main, "automate_indices", automate)

    assert await main.run(args(enrich_only=True), settings()) == 0  # type: ignore[arg-type]
    assert calls == ["enrich"]


@pytest.mark.asyncio
async def test_automation_only_never_calls_apollo_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(main, "CSVService", lambda *_args: FakeCSVService())

    async def enrich(*_args: object) -> None:
        calls.append("enrich")

    async def automate(*_args: object) -> int:
        calls.append("automate")
        return 0

    monkeypatch.setattr(main, "enrich_indices", enrich)
    monkeypatch.setattr(main, "automate_indices", automate)

    assert await main.run(args(automation_only=True), settings()) == 0  # type: ignore[arg-type]
    assert calls == ["automate"]
