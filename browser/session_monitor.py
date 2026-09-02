from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Browser, Playwright, async_playwright

from browser.connection import find_servicenow_context


LOGIN_URL_MARKERS = (
    "/login",
    "auth_redirect",
    "login.microsoftonline.com",
    "signin",
    "sso",
)


@dataclass(frozen=True, slots=True)
class LoginSnapshot:
    status: str = "Waiting for Login"
    logged_in: bool = False
    browser_connected: bool = False
    detail: str = "Waiting for Chrome and an authenticated ServiceNow page"
    checked_at: str = ""
    tone: str = "waiting"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SessionPhase:
    status: str
    detail: str
    tone: str = "working"


class LoginSessionMonitor:
    """Observe the Chrome session used by the existing automation.

    The monitor only attaches over CDP. It never launches or closes Chrome, so
    the user's manual login and the automation continue in the same session.
    """

    def __init__(self, *, poll_interval_seconds: float = 2.0) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self._snapshot = LoginSnapshot()
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._phase: SessionPhase | None = None

    @property
    def snapshot(self) -> LoginSnapshot:
        return self._snapshot

    def set_phase(self, status: str, detail: str = "", *, tone: str = "working") -> None:
        self._phase = SessionPhase(status=status, detail=detail or status, tone=tone)
        self._publish_snapshot(
            logged_in=self._snapshot.logged_in,
            connected=self._snapshot.browser_connected,
            detail=self._snapshot.detail,
            default_status="Logged In" if self._snapshot.logged_in else "Waiting for Login",
            default_tone="logged-in" if self._snapshot.logged_in else "waiting",
        )

    def clear_phase(self) -> None:
        self._phase = None
        self._publish_snapshot(
            logged_in=self._snapshot.logged_in,
            connected=self._snapshot.browser_connected,
            detail=self._snapshot.detail,
            default_status="Logged In" if self._snapshot.logged_in else "Waiting for Login",
            default_tone="logged-in" if self._snapshot.logged_in else "waiting",
        )

    async def start(self, cdp_url: str) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(cdp_url), name="servicenow-login-monitor")

    async def stop(self) -> None:
        if not self._task:
            return
        if self._stop_event:
            self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self, cdp_url: str) -> None:
        browser: Browser | None = None
        try:
            async with async_playwright() as playwright:
                while self._stop_event and not self._stop_event.is_set():
                    if browser is None or not browser.is_connected():
                        browser = await self._connect(playwright, cdp_url)
                    if browser is not None and browser.is_connected():
                        await self._inspect(browser)
                    await asyncio.sleep(self.poll_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set_waiting(False, f"Login monitor error: {exc}")

    async def _connect(self, playwright: Playwright, cdp_url: str) -> Browser | None:
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=5_000)
        except Exception:
            self._set_waiting(False, "Chrome is not connected")
            return None
        return browser

    async def _inspect(self, browser: Browser) -> None:
        try:
            match = await find_servicenow_context(browser, allow_partner=True)
            if match:
                detail = (
                    "Authenticated ServiceNow Customer Information form detected"
                    if match.page_kind == "customer_information"
                    else "Authenticated ServiceNow Partner Information page detected"
                )
                self._publish_snapshot(
                    logged_in=True,
                    connected=True,
                    detail=detail,
                    default_status="Logged In",
                    default_tone="logged-in",
                )
                return

            page_urls = [
                page.url.casefold()
                for context in browser.contexts
                for page in context.pages
                if not page.is_closed()
            ]
            if any(marker in url for url in page_urls for marker in LOGIN_URL_MARKERS):
                detail = "ServiceNow login page detected"
            else:
                detail = "Waiting for an authenticated ServiceNow page"
            self._set_waiting(True, detail)
        except Exception:
            self._set_waiting(browser.is_connected(), "Waiting for the ServiceNow session")

    def _set_waiting(self, connected: bool, detail: str) -> None:
        self._publish_snapshot(
            logged_in=False,
            connected=connected,
            detail=detail,
            default_status="Waiting for Login",
            default_tone="waiting",
        )

    def _publish_snapshot(
        self,
        *,
        logged_in: bool,
        connected: bool,
        detail: str,
        default_status: str,
        default_tone: str,
    ) -> None:
        phase = self._phase if logged_in else None
        self._snapshot = LoginSnapshot(
            status=phase.status if phase else default_status,
            logged_in=logged_in,
            browser_connected=connected,
            detail=phase.detail if phase else detail,
            checked_at=_utc_now(),
            tone=phase.tone if phase else default_tone,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
