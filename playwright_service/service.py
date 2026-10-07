from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable

from playwright.async_api import Error as PlaywrightError, Playwright, async_playwright

from .browser.connection import ConnectedServiceNow, FormNotFoundError, connect_to_servicenow
from .browser.errors import PreparationError
from .browser.preparation import prepare_existing_session
from .browser.servicenow import ServiceNowChecker, SessionExpiredError
from .config import AutomationSettings
from .models import SearchResult
from .schemas import CompanyInput, CompanyResponse, PreparationResponse


LOGGER = logging.getLogger(__name__)


def clean_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:1000] or type(exc).__name__


class AutomationService:
    def __init__(self, settings: AutomationSettings) -> None:
        self.settings = settings

    def _checker(self, connection: ConnectedServiceNow) -> ServiceNowChecker:
        return ServiceNowChecker(
            page=connection.page, frame=connection.frame,
            timeout_seconds=self.settings.search_timeout_seconds,
            match_threshold=self.settings.match_threshold,
            review_threshold=self.settings.review_threshold,
            save_screenshots=self.settings.save_screenshots,
            debug_dir=self.settings.debug_dir,
            result_selectors=self.settings.result_selectors,
        )

    async def inspect_session(self) -> dict[str, object]:
        async with async_playwright() as playwright:
            connection = await connect_to_servicenow(
                playwright, self.settings.chrome_cdp_url, allow_partner=True,
            )
            return {"browser_connected": True, "authenticated": True, "page_kind": connection.page_kind}

    async def prepare_session(
        self, *, status_callback: Callable[[str, str, str], None] | None = None,
    ) -> PreparationResponse:
        """Prepare the logged-in form and stop before searching any companies."""
        async with async_playwright() as playwright:
            await self._prepare_connection(playwright, status_callback=status_callback)
            return PreparationResponse()

    async def _prepare_connection(
        self, playwright: Playwright, *,
        status_callback: Callable[[str, str, str], None] | None = None,
    ) -> ConnectedServiceNow:
        # Fail fast when Chrome is unavailable, then allow a bounded manual-login wait.
        try:
            await playwright.chromium.connect_over_cdp(self.settings.chrome_cdp_url, timeout=15_000)
        except PlaywrightError as exc:
            raise ConnectionError(
                f"Could not connect to Chrome at {self.settings.chrome_cdp_url}. "
                "Start Chrome with remote debugging enabled."
            ) from exc
        try:
            return await prepare_existing_session(
                playwright, self.settings.chrome_cdp_url,
                login_timeout_seconds=self.settings.login_timeout_seconds,
                action_timeout_seconds=self.settings.action_timeout_seconds,
                status_callback=status_callback,
            )
        except (PlaywrightError, AssertionError) as exc:
            raise PreparationError("Preparing Customer Information", clean_error(exc)) from exc

    async def check_companies(self, companies: list[CompanyInput]) -> list[CompanyResponse]:
        async with async_playwright() as playwright:
            connection = await self._prepare_connection(playwright)
            return await self._check_in_session(playwright, connection, companies)

    async def _check_in_session(
        self, playwright: Playwright, connection: ConnectedServiceNow, companies: list[CompanyInput],
    ) -> list[CompanyResponse]:
        checker = self._checker(connection)
        responses: list[CompanyResponse] = []
        session_error: str | None = None
        for index, company in enumerate(companies):
            if session_error:
                responses.append(self._response(company, error=f"Not searched: {session_error}"))
                continue
            try:
                for attempt in range(2):
                    try:
                        result = await checker.search_with_retry(company.company_name, company.country_code or "")
                        break
                    except SessionExpiredError:
                        if attempt:
                            raise
                        # The portal sometimes closes a tab and opens a replacement.
                        for reconnect_attempt in range(5):
                            try:
                                connection = await connect_to_servicenow(playwright, self.settings.chrome_cdp_url)
                                checker = self._checker(connection)
                                break
                            except (ConnectionError, FormNotFoundError) as exc:
                                if reconnect_attempt == 4:
                                    raise SessionExpiredError(clean_error(exc)) from exc
                                await asyncio.sleep(1)
                responses.append(self._response(company, result=result))
            except SessionExpiredError as exc:
                session_error = clean_error(exc)
                responses.append(self._response(company, error=session_error))
            except Exception as exc:
                LOGGER.exception("ServiceNow check failed for %s", company.company_name)
                responses.append(self._response(company, error=clean_error(exc)))
            if index + 1 < len(companies) and not session_error:
                await asyncio.sleep(self.settings.delay_between_companies_seconds)
        return responses

    @staticmethod
    def _response(
        company: CompanyInput, *, result: SearchResult | None = None, error: str = "",
    ) -> CompanyResponse:
        return CompanyResponse(
            company_name=company.company_name, headquarters=company.headquarters,
            country=company.country or "", country_code=company.country_code or "",
            servicenow_customer=result.customer if result else "Unknown",
            servicenow_matched_name=result.matched_name if result else "",
            match_score=result.match_score if result and result.matched_name else None,
            check_status=result.status.value if result else "error",
            returned_names=list(result.returned_names) if result else [],
            error_message=result.error_message if result else error,
            checked_at=datetime.now(timezone.utc),
        )
