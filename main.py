from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Error as PlaywrightError, Playwright, async_playwright
from pydantic import ValidationError

from browser.connection import ConnectedServiceNow, FormNotFoundError, connect_to_servicenow
from browser.servicenow import SearchTechnicalError, ServiceNowChecker, SessionExpiredError, safe_filename
from clients.apollo import ApolloClient, ApolloError
from config import Settings, load_settings
from models.company import CheckStatus, CompanyRecord
from services.country_normalizer import CountryNormalizationError, country_name, normalize_country
from services.csv_service import CSVService
from utils.logger import configure_logging


LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check CSV companies against the open ServiceNow Customer Information form."
    )
    parser.add_argument("--force", action="store_true", help="Recheck rows already marked completed")
    parser.add_argument("--company", help="Process only this exact company name (case-insensitive)")
    parser.add_argument("--limit", type=int, help="Process only the first N selected rows")
    parser.add_argument("--env-file", type=Path, help="Use an alternative .env file")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    stages = parser.add_mutually_exclusive_group()
    stages.add_argument(
        "--enrich-only",
        action="store_true",
        help="Run Apollo enrichment and checkpoint the CSV without opening a browser",
    )
    stages.add_argument(
        "--automation-only",
        action="store_true",
        help="Run ServiceNow browser automation using previously enriched CSV rows",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def clean_error(exc: BaseException) -> str:
    """Keep CSV errors readable and ensure no multiline response bodies leak into logs."""

    return " ".join(str(exc).split())[:1000]


def build_apollo_client(settings: Settings) -> ApolloClient:
    return ApolloClient(
        api_key=settings.apollo_api_key,
        base_url=settings.apollo_base_url,
        timeout_seconds=settings.apollo_timeout_seconds,
        max_retries=settings.apollo_max_retries,
        match_threshold=settings.apollo_match_threshold,
    )


async def enrich_or_override(
    record: CompanyRecord, apollo: ApolloClient
) -> tuple[str, str, str, str]:
    """Return headquarters, country name, country code, and Apollo organization name."""

    if record.country_override.strip():
        try:
            code = normalize_country(record.country_override)
            LOGGER.info("Using country_override: %s - %s", code, country_name(code))
            return (
                record.headquarters or country_name(code),
                country_name(code),
                code,
                record.apollo_company_name,
            )
        except CountryNormalizationError:
            LOGGER.warning("Ignoring invalid country_override and falling back to Apollo")

    company = await asyncio.to_thread(
        apollo.enrich, record.company_name, record.linkedin_url, record.domain
    )
    LOGGER.info("Apollo: %s", company.company_name)
    LOGGER.info("Headquarters: %s", company.headquarters)
    LOGGER.info("Country: %s", company.country_code)
    return (
        company.headquarters,
        company.country,
        company.country_code,
        company.company_name,
    )


async def enrich_indices(
    csv_service: CSVService,
    indices: list[int],
    settings: Settings,
) -> None:
    """Checkpoint Apollo organization data without touching the browser."""

    apollo = build_apollo_client(settings)
    for position, index in enumerate(indices, start=1):
        try:
            record = csv_service.record(index)
        except (ValidationError, ValueError) as exc:
            csv_service.update(
                index,
                servicenow_customer="Unknown",
                check_status=CheckStatus.ERROR,
                error_message=clean_error(exc),
            )
            csv_service.save()
            LOGGER.error("[%d/%d] Invalid CSV row: %s", position, len(indices), clean_error(exc))
            continue

        LOGGER.info("[%d/%d] Enriching %s", position, len(indices), record.company_name)
        try:
            headquarters, country, country_code, apollo_name = await enrich_or_override(
                record, apollo
            )
            csv_service.update(
                index,
                headquarters=headquarters,
                country=country,
                country_code=country_code,
                apollo_company_name=apollo_name,
                servicenow_customer="",
                servicenow_matched_name="",
                servicenow_screenshot="",
                match_score="",
                check_status=CheckStatus.APOLLO_SUCCESS,
                error_message="",
                checked_at="",
            )
        except (ApolloError, CountryNormalizationError) as exc:
            csv_service.update(
                index,
                servicenow_customer="Unknown",
                check_status=CheckStatus.APOLLO_FAILED,
                error_message=clean_error(exc),
                checked_at=record.checked_now(),
            )
            LOGGER.error("Apollo enrichment failed: %s", clean_error(exc))
        except Exception as exc:
            csv_service.update(
                index,
                servicenow_customer="Unknown",
                check_status=CheckStatus.APOLLO_FAILED,
                error_message=clean_error(exc),
                checked_at=record.checked_now(),
            )
            LOGGER.exception("Unexpected Apollo enrichment failure")
        finally:
            csv_service.save()

        if position < len(indices):
            await asyncio.sleep(settings.delay_between_companies_seconds)


async def automate_indices(
    csv_service: CSVService,
    indices: list[int],
    settings: Settings,
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
    csv_service: CSVService,
    ready: list[int],
    settings: Settings,
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


async def run(args: argparse.Namespace, settings: Settings) -> int:
    csv_service = CSVService(settings.input_csv, settings.output_csv)
    indices = csv_service.selected_indices(
        force=args.force, company=args.company, limit=args.limit
    )
    if args.company and not indices:
        LOGGER.error("No eligible CSV row matched --company %r", args.company)
        return 2
    if not indices:
        LOGGER.info("No pending companies to process. Use --force to recheck completed rows.")
        return 0

    LOGGER.info("Preparing to process %d company row(s)", len(indices))
    if not args.automation_only:
        await enrich_indices(csv_service, indices, settings)
        if args.enrich_only:
            LOGGER.info("Enrichment stage finished without opening a browser.")
            return 0
    return await automate_indices(csv_service, indices, settings)


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)
    try:
        settings = load_settings(args.env_file)
        return asyncio.run(run(args, settings))
    except (ValidationError, ValueError) as exc:
        LOGGER.error("Configuration error: %s", clean_error(exc))
        return 2
    except (FileNotFoundError, PermissionError) as exc:
        LOGGER.error("File error: %s", clean_error(exc))
        return 2
    except KeyboardInterrupt:
        LOGGER.warning("Stopped by user. Previously checkpointed CSV progress is preserved.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
