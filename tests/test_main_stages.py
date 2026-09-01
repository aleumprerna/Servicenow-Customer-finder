from types import SimpleNamespace

import pytest

import main
from browser.servicenow import SessionExpiredError
from models.company import CheckStatus, SearchResult


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


@pytest.mark.asyncio
async def test_automation_reconnects_when_servicenow_replaces_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CSV:
        updates: list[dict[str, object]] = []

        def record(self, index: int) -> SimpleNamespace:
            return SimpleNamespace(
                company_name="Example Corp",
                country_code="US",
                checked_now=lambda: "2026-09-01T00:00:00+00:00",
            )

        def update(self, index: int, **values: object) -> None:
            self.updates.append(values)

        def save(self) -> None:
            pass

    class PlaywrightContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            pass

    connections = iter(
        [
            SimpleNamespace(page="closed-page", frame="old-frame"),
            SimpleNamespace(page="replacement-page", frame="new-frame"),
        ]
    )
    connect_count = 0

    async def connect(*_args: object) -> SimpleNamespace:
        nonlocal connect_count
        connect_count += 1
        return next(connections)

    class Checker:
        def __init__(self, page: str) -> None:
            self.page = page

        async def assert_session_active(self) -> None:
            pass

        async def search_with_retry(self, company: str, country: str) -> SearchResult:
            if self.page == "closed-page":
                raise SessionExpiredError("The ServiceNow browser page was closed")
            return SearchResult(customer="No", status=CheckStatus.COMPLETED)

    async def no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(main, "async_playwright", lambda: PlaywrightContext())
    monkeypatch.setattr(main, "connect_to_servicenow", connect)
    monkeypatch.setattr(main, "ServiceNowChecker", lambda **values: Checker(values["page"]))
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    stage_settings = SimpleNamespace(
        chrome_cdp_url="http://localhost:9222",
        search_timeout_seconds=20,
        match_threshold=85,
        review_threshold=70,
        save_screenshots=False,
        debug_dir="debug",
        result_selectors=(),
        output_csv="output.csv",
        delay_between_companies_seconds=0,
    )
    csv_service = CSV()

    assert await main.automate_indices(csv_service, [0], stage_settings) == 0  # type: ignore[arg-type]
    assert connect_count == 2
    assert csv_service.updates[-1]["check_status"] == CheckStatus.COMPLETED
