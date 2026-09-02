import pytest

import browser.session_monitor as session_monitor
from browser.connection import ConnectedServiceNow
from browser.session_monitor import LoginSessionMonitor


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False


class FakeContext:
    def __init__(self, *urls: str) -> None:
        self.pages = [FakePage(url) for url in urls]


class FakeBrowser:
    def __init__(self, *urls: str) -> None:
        self.contexts = [FakeContext(*urls)]

    def is_connected(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_monitor_reports_logged_in_when_authenticated_form_exists(monkeypatch) -> None:
    async def find_context(_browser, *, allow_partner: bool):
        assert allow_partner is True
        return ConnectedServiceNow(
            browser=_browser,
            page=object(),  # type: ignore[arg-type]
            frame=object(),  # type: ignore[arg-type]
            page_kind="customer_information",
        )

    monkeypatch.setattr(session_monitor, "find_servicenow_context", find_context)
    monitor = LoginSessionMonitor()

    await monitor._inspect(FakeBrowser("https://partnerportal.servicenow.com/partnerhome"))

    assert monitor.snapshot.status == "Logged In"
    assert monitor.snapshot.logged_in is True
    assert monitor.snapshot.browser_connected is True


@pytest.mark.asyncio
async def test_monitor_returns_to_waiting_when_login_form_disappears(monkeypatch) -> None:
    results = [
        ConnectedServiceNow(
            browser=FakeBrowser("https://partnerportal.servicenow.com/partnerhome"),
            page=object(),  # type: ignore[arg-type]
            frame=object(),  # type: ignore[arg-type]
            page_kind="partner_information",
        ),
        None,
    ]

    async def find_context(_browser, *, allow_partner: bool):
        assert allow_partner is True
        return results.pop(0)

    monkeypatch.setattr(session_monitor, "find_servicenow_context", find_context)
    monitor = LoginSessionMonitor()
    browser = FakeBrowser("https://partnerportal.servicenow.com/partnerhome")

    await monitor._inspect(browser)
    await monitor._inspect(browser)

    assert monitor.snapshot.status == "Waiting for Login"
    assert monitor.snapshot.logged_in is False
    assert monitor.snapshot.browser_connected is True


@pytest.mark.asyncio
async def test_monitor_recognizes_a_login_redirect(monkeypatch) -> None:
    async def find_context(_browser, *, allow_partner: bool):
        assert allow_partner is True
        return None

    monkeypatch.setattr(session_monitor, "find_servicenow_context", find_context)
    monitor = LoginSessionMonitor()

    await monitor._inspect(FakeBrowser("https://login.microsoftonline.com/example"))

    assert monitor.snapshot.status == "Waiting for Login"
    assert monitor.snapshot.detail == "ServiceNow login page detected"
