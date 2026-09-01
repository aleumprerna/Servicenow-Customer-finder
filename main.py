from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from playwright.async_api import Error as PlaywrightError, async_playwright
from pydantic import ValidationError

from browser.connection import FormNotFoundError, connect_to_servicenow
from browser.servicenow import SearchTechnicalError, ServiceNowChecker, SessionExpiredError, safe_filename
from clients.openai_web_search import OpenAIWebSearchClient, WebSearchError
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
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def clean_error(exc: BaseException) -> str:
    """Keep CSV errors readable and ensure no multiline response bodies leak into logs."""

    return " ".join(str(exc).split())[:1000]


def build_web_search_client(settings: Settings) -> OpenAIWebSearchClient:
    return OpenAIWebSearchClient(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timeout_seconds=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )


async def enrich_or_override(
    record: CompanyRecord, web_search: OpenAIWebSearchClient
) -> tuple[str, str, str, str]:
    """Return headquarters, country name, country code, and researched organization name."""

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
            LOGGER.warning("Ignoring invalid country_override and falling back to web search")

    company = await asyncio.to_thread(
        web_search.enrich, record.company_name, record.linkedin_url, record.domain
    )
    LOGGER.info("Web search company: %s", company.company_name)
    LOGGER.info("Headquarters: %s", company.headquarters)
    LOGGER.info("Country: %s", company.country_code)
    return (
        company.headquarters,
        company.country,
        company.country_code,
        company.company_name,
    )


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

    web_search = build_web_search_client(settings)
    LOGGER.info("Preparing to process %d company row(s)", len(indices))

    async with async_playwright() as playwright:
        try:
            connected = await connect_to_servicenow(playwright, settings.chrome_cdp_url)
        except (ConnectionError, FormNotFoundError) as exc:
            LOGGER.error("%s", clean_error(exc))
            return 3

        LOGGER.info("Connected to existing Chrome and found the ServiceNow customer form.")
        checker = ServiceNowChecker(
            page=connected.page,
            frame=connected.frame,
            timeout_seconds=settings.search_timeout_seconds,
            match_threshold=settings.match_threshold,
            review_threshold=settings.review_threshold,
            save_screenshots=settings.save_screenshots,
            debug_dir=settings.debug_dir,
            result_selectors=settings.result_selectors,
        )

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

            LOGGER.info("[%d/%d] %s", position, len(indices), record.company_name)
            try:
                await checker.assert_session_active()
            except SessionExpiredError as exc:
                csv_service.update(
                    index,
                    servicenow_customer="Unknown",
                    check_status=CheckStatus.ERROR,
                    error_message=clean_error(exc),
                    checked_at=record.checked_now(),
                )
                csv_service.save()
                LOGGER.error("%s", clean_error(exc))
                return 4

            try:
                headquarters, country, country_code, researched_name = await enrich_or_override(
                    record, web_search
                )
                csv_service.update(
                    index,
                    headquarters=headquarters,
                    country=country,
                    country_code=country_code,
                    # Keep this legacy column so old CSV/database/n8n contracts continue to work.
                    apollo_company_name=researched_name,
                    servicenow_customer="",
                    servicenow_matched_name="",
                    match_score="",
                    check_status=CheckStatus.WEB_SEARCH_SUCCESS,
                    error_message="",
                    checked_at="",
                )
                csv_service.save()
            except (WebSearchError, CountryNormalizationError) as exc:
                csv_service.update(
                    index,
                    servicenow_customer="Unknown",
                    check_status=CheckStatus.WEB_SEARCH_FAILED,
                    error_message=clean_error(exc),
                    checked_at=record.checked_now(),
                )
                csv_service.save()
                LOGGER.error("OpenAI web search enrichment failed: %s", clean_error(exc))
                LOGGER.info("CSV updated.")
                if position < len(indices):
                    await asyncio.sleep(settings.delay_between_companies_seconds)
                continue
            except Exception as exc:
                csv_service.update(
                    index,
                    servicenow_customer="Unknown",
                    check_status=CheckStatus.WEB_SEARCH_FAILED,
                    error_message=clean_error(exc),
                    checked_at=record.checked_now(),
                )
                csv_service.save()
                LOGGER.exception("Unexpected OpenAI web search enrichment failure")
                if position < len(indices):
                    await asyncio.sleep(settings.delay_between_companies_seconds)
                continue

            csv_service.update(index, check_status=CheckStatus.SEARCHING)
            csv_service.save()
            LOGGER.info("Selecting country: %s - %s", country_code, country_name(country_code))
            LOGGER.info("Searching customer: %s", record.company_name)

            try:
                result = await checker.search_with_retry(record.company_name, country_code)
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
            except Exception as exc:
                csv_service.update(
                    index,
                    servicenow_customer="Unknown",
                    check_status=CheckStatus.ERROR,
                    error_message=clean_error(exc),
                    checked_at=record.checked_now(),
                )
                LOGGER.exception("Unexpected company-processing failure")
            finally:
                csv_service.save()
                LOGGER.info("CSV updated: %s", settings.output_csv)
                LOGGER.info("%s", "-" * 50)

            if position < len(indices):
                await asyncio.sleep(settings.delay_between_companies_seconds)

        # Do not call browser.close(): this is the user's externally managed Chrome.
        LOGGER.info("Finished %d company row(s). Existing Chrome was left open.", len(indices))
        return 0


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
