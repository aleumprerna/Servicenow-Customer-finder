from types import SimpleNamespace

import pytest

from playwright_service import service as service_module
from playwright_service.browser.errors import PreparationError
from playwright_service.browser.servicenow import SearchTechnicalError, SessionExpiredError
from playwright_service.config import AutomationSettings
from playwright_service.models import CheckStatus, SearchResult
from playwright_service.schemas import CheckCompaniesResponse, CompanyInput
from playwright_service.service import AutomationService


def inputs():
    return [CompanyInput(company_name=name, headquarters="India") for name in ["A", "B", "C"]]


@pytest.mark.asyncio
async def test_batch_preserves_order_and_continues_after_company_failure(monkeypatch):
    settings = AutomationSettings(delay_between_companies_seconds=0)
    service = AutomationService(settings)
    calls = []

    class Checker:
        async def search_with_retry(self, name, code):
            calls.append((name, code))
            if name == "A":
                return SearchResult(customer="Yes", matched_name="A Inc", match_score=100,
                                    status=CheckStatus.COMPLETED, returned_names=("A Inc",))
            if name == "B":
                raise SearchTechnicalError("Temporary failure")
            return SearchResult(customer="Unknown", status=CheckStatus.MANUAL_REVIEW,
                                error_message="Unrecognized result HTML")

    monkeypatch.setattr(service, "_checker", lambda _connection: Checker())
    rows = await service._check_in_session(object(), object(), inputs())
    assert calls == [("A", "IN"), ("B", "IN"), ("C", "IN")]
    response = CheckCompaniesResponse.from_results(rows)
    assert not response.success
    assert response.completed == response.errors == response.manual_review == 1
    assert [row.servicenow_customer for row in rows] == ["Yes", "Unknown", "Unknown"]
    assert rows[0].returned_names == ["A Inc"]
    assert rows[1].error_message == "Temporary failure"


@pytest.mark.asyncio
async def test_session_loss_returns_error_for_every_unprocessed_company(monkeypatch):
    service = AutomationService(AutomationSettings(delay_between_companies_seconds=0))
    calls = []

    class Checker:
        async def search_with_retry(self, name, code):
            calls.append(name)
            raise SessionExpiredError("Please log in again")

    async def reconnect(*_args):
        return object()

    monkeypatch.setattr(service, "_checker", lambda _connection: Checker())
    monkeypatch.setattr(service_module, "connect_to_servicenow", reconnect)
    rows = await service._check_in_session(object(), object(), inputs())
    assert calls == ["A", "A"]
    assert len(rows) == 3
    assert all(row.check_status == "error" and row.servicenow_customer == "Unknown" for row in rows)
    assert rows[1].error_message == "Not searched: Please log in again"


@pytest.mark.asyncio
async def test_replaced_page_is_reconnected_and_searched_once_more(monkeypatch):
    service = AutomationService(AutomationSettings(delay_between_companies_seconds=0))
    calls = []

    class Checker:
        def __init__(self, page):
            self.page = page

        async def search_with_retry(self, name, code):
            calls.append(self.page)
            if self.page == "old":
                raise SessionExpiredError("Page replaced")
            return SearchResult(customer="No", status=CheckStatus.COMPLETED)

    async def reconnect(*_args):
        return SimpleNamespace(page="new")

    monkeypatch.setattr(service, "_checker", lambda connection: Checker(connection.page))
    monkeypatch.setattr(service_module, "connect_to_servicenow", reconnect)
    rows = await service._check_in_session(object(), SimpleNamespace(page="old"), inputs()[:1])
    assert calls == ["old", "new"]
    assert rows[0].servicenow_customer == "No"


@pytest.mark.asyncio
async def test_service_prepares_authenticated_session_and_leaves_browser_open(monkeypatch):
    events = []

    class PlaywrightContext:
        async def __aenter__(self):
            async def connect(*_args, **_kwargs):
                events.append("connect")
                return object()
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

        async def __aexit__(self, *_args):
            events.append("disconnect")

    async def prepare(*_args, **_kwargs):
        events.append("prepare")
        return object()

    async def check(*_args):
        events.append("check")
        return []

    service = AutomationService(AutomationSettings())
    monkeypatch.setattr(service_module, "async_playwright", PlaywrightContext)
    monkeypatch.setattr(service_module, "prepare_existing_session", prepare)
    monkeypatch.setattr(service, "_check_in_session", check)
    assert await service.check_companies(inputs()) == []
    assert events == ["connect", "prepare", "check", "disconnect"]


@pytest.mark.asyncio
async def test_missing_preparation_controls_are_reported_as_setup_failure(monkeypatch):
    class PlaywrightContext:
        async def __aenter__(self):
            async def connect(*_args, **_kwargs):
                return object()
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

        async def __aexit__(self, *_args):
            pass

    async def prepare(*_args, **_kwargs):
        raise AssertionError("Customer form was not visible")

    monkeypatch.setattr(service_module, "async_playwright", PlaywrightContext)
    monkeypatch.setattr(service_module, "prepare_existing_session", prepare)
    with pytest.raises(PreparationError, match="Customer form was not visible"):
        await AutomationService(AutomationSettings()).check_companies(inputs())
