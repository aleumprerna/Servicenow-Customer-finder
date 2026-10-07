from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from playwright.async_api import Error as PlaywrightError, Playwright, async_playwright
from pydantic import ValidationError

from .browser.connection import ConnectedServiceNow, FormNotFoundError, connect_to_servicenow
from .browser.preparation import prepare_existing_session
from .browser.servicenow import SearchTechnicalError, ServiceNowChecker, SessionExpiredError
from .country_normalizer import CountryNormalizationError, country_name, normalize_country
from .filenames import safe_filename
from .models import CheckStatus

LOGGER = logging.getLogger(__name__)


async def prepare_and_automate_indices(
    csv_service: Any,
    indices: list[int],
    settings: Any,
    *,
    status_callback: Callable[[str, str, str], None] | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> int:
    """Adapter for the existing application's CSV checkpoint interface."""
    async with async_playwright() as playwright:
        connection = await prepare_existing_session(
            playwright,
            settings.chrome_cdp_url,
            status_callback=status_callback,
            action_timeout_seconds=max(settings.search_timeout_seconds, 60.0),
        )
        if status_callback:
            status_callback(
                "Automation Running",
                "Processing the prepared Customer Information page",
                "running",
            )
        return await automate_indices(
            csv_service, indices, settings,
            playwright=playwright, connected=connection,
            progress_callback=progress_callback,
        )


def clean_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:1000]

async def automate_indices(
    csv_service: Any,
    indices: list[int],
    settings: Any,
    *,
    playwright: Playwright | None = None,
    connected: ConnectedServiceNow | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> int:
    """Run ServiceNow checks using only previously checkpointed enrichment."""

    ready: list[int] = []
    for index in indices:
        try:
            record = csv_service.record(index)
            normalize_country(record.country_code)
        except (ValidationError, ValueError, CountryNormalizationError):
            LOGGER.warning("Skipping row %d because it has not been enriched", index + 2)
            continue
        ready.append(index)
    if not ready:
        LOGGER.error("No enriched rows are ready. Run the enrichment stage first.")
        return 2

    def make_checker(connection: object) -> ServiceNowChecker:
        return ServiceNowChecker(
            page=connection.page,  # type: ignore[attr-defined]
            frame=connection.frame,  # type: ignore[attr-defined]
            timeout_seconds=settings.search_timeout_seconds,
            match_threshold=settings.match_threshold,
            review_threshold=settings.review_threshold,
            save_screenshots=settings.save_screenshots,
            debug_dir=settings.debug_dir,
            result_selectors=settings.result_selectors,
        )

    if connected is not None or playwright is not None:
        if connected is None or playwright is None:
            raise ValueError("connected and playwright must be supplied together")
        LOGGER.info("Continuing with the existing attached Chrome session.")
        return await _automate_ready_rows(
            csv_service,
            ready,
            settings,
            playwright=playwright,
            connected=connected,
            make_checker=make_checker,
            progress_callback=progress_callback,
        )

    async with async_playwright() as local_playwright:
        try:
            local_connected = await connect_to_servicenow(local_playwright, settings.chrome_cdp_url)
        except (ConnectionError, FormNotFoundError) as exc:
            LOGGER.error("%s", clean_error(exc))
            return 3
        LOGGER.info("Connected to existing Chrome and found the ServiceNow customer form.")
        return await _automate_ready_rows(
            csv_service,
            ready,
            settings,
            playwright=local_playwright,
            connected=local_connected,
            make_checker=make_checker,
            progress_callback=progress_callback,
        )


async def _automate_ready_rows(
    csv_service: Any,
    ready: list[int],
    settings: Any,
    *,
    playwright: Playwright,
    connected: ConnectedServiceNow,
    make_checker: Callable[[Any], ServiceNowChecker],
    progress_callback: Callable[[], None] | None,
) -> int:
    checker = make_checker(connected)

    for position, index in enumerate(ready, start=1):
        record = csv_service.record(index)
        country_code = normalize_country(record.country_code)
        LOGGER.info("[%d/%d] %s", position, len(ready), record.company_name)
        csv_service.update(index, check_status=CheckStatus.SEARCHING)
        csv_service.save()
        if progress_callback:
            progress_callback()
        LOGGER.info("Selecting country: %s - %s", country_code, country_name(country_code))
        LOGGER.info("Searching customer: %s", record.company_name)

        try:
            for session_attempt in range(2):
                try:
                    await checker.assert_session_active()
                    result = await checker.search_with_retry(record.company_name, country_code)
                    break
                except SessionExpiredError as exc:
                    if session_attempt > 0:
                        raise
                    LOGGER.warning(
                        "%s; looking for a replacement ServiceNow page and retrying once",
                        clean_error(exc),
                    )
                    reconnect_error: Exception = exc
                    for _ in range(5):
                        await asyncio.sleep(1)
                        try:
                            connected = await connect_to_servicenow(
                                playwright, settings.chrome_cdp_url
                            )
                            checker = make_checker(connected)
                            LOGGER.info(
                                "Reconnected to the ServiceNow form after the page changed"
                            )
                            break
                        except (ConnectionError, FormNotFoundError) as candidate_error:
                            reconnect_error = candidate_error
                    else:
                        raise SessionExpiredError(
                            "The ServiceNow page closed and no replacement form appeared: "
                            f"{clean_error(reconnect_error)}"
                        ) from reconnect_error
            screenshot_path = ""
            if result.customer.casefold() == "yes" and settings.save_screenshots:
                screenshot_path = str(
                    settings.debug_dir
                    / "screenshots"
                    / f"{safe_filename(record.company_name)}_results.png"
                )
            csv_service.update(
                index,
                servicenow_customer=result.customer,
                servicenow_matched_name=result.matched_name,
                servicenow_screenshot=screenshot_path,
                match_score=result.match_score if result.matched_name else "",
                check_status=result.status,
                error_message=result.error_message,
                checked_at=record.checked_now(),
            )
            if result.returned_names:
                for number, name in enumerate(result.returned_names, start=1):
                    LOGGER.info("ServiceNow result %d: %s", number, name)
            if result.matched_name:
                LOGGER.info("Best match: %s (score %d)", result.matched_name, result.match_score)
            LOGGER.info("ServiceNow Customer: %s", result.customer.upper())
        except SessionExpiredError as exc:
            csv_service.update(
                index,
                servicenow_customer="Unknown",
                check_status=CheckStatus.ERROR,
                error_message=clean_error(exc),
                checked_at=record.checked_now(),
            )
            csv_service.save()
            if progress_callback:
                progress_callback()
            LOGGER.error("%s", clean_error(exc))
            return 4
        except (SearchTechnicalError, PlaywrightError) as exc:
            csv_service.update(
                index,
                servicenow_customer="Unknown",
                check_status=CheckStatus.ERROR,
                error_message=clean_error(exc),
                checked_at=record.checked_now(),
            )
            LOGGER.error("ServiceNow automation failed: %s", clean_error(exc))
        except Exception:
            csv_service.update(
                index,
                servicenow_customer="Unknown",
                check_status=CheckStatus.ERROR,
                error_message="Unexpected ServiceNow automation failure",
                checked_at=record.checked_now(),
            )
            LOGGER.exception("Unexpected company-processing failure")
        finally:
            csv_service.save()
            if progress_callback:
                progress_callback()
            LOGGER.info("CSV updated: %s", settings.output_csv)
            LOGGER.info("%s", "-" * 50)

        if position < len(ready):
            await asyncio.sleep(settings.delay_between_companies_seconds)

    LOGGER.info("Finished %d company row(s). Existing Chrome was left open.", len(ready))
    return 0


