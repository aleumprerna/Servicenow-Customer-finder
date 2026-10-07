from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from playwright_service.csv_runner import automate_indices
from clients.apollo import ApolloClient, ApolloError
from config import Settings, load_settings
from models.company import CheckStatus, CompanyRecord
from services.country_normalizer import CountryNormalizationError, country_name, normalize_country
from services.ai_company_resolver import resolve_company_headquarters
from services.csv_service import CSVService
from utils.logger import configure_logging
from workflow.database import WorkflowDatabase


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
    metrics_database: WorkflowDatabase | None = None
    metrics_run_id = 0
    metrics_path = os.getenv("AI_METRICS_DATABASE", "").strip()
    try:
        metrics_run_id = int(os.getenv("AI_METRICS_RUN_ID", "0"))
    except ValueError:
        metrics_run_id = 0
    if metrics_path and metrics_run_id:
        metrics_database = WorkflowDatabase(Path(metrics_path))
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
        enrichment_status = CheckStatus.APOLLO_SUCCESS
        try:
            try:
                headquarters, country, country_code, apollo_name = await enrich_or_override(
                    record, apollo
                )
            except ApolloError as apollo_exc:
                LOGGER.warning(
                    "Apollo enrichment failed for %s; trying grounded AI headquarters lookup: %s",
                    record.company_name,
                    clean_error(apollo_exc),
                )
                metrics_callback = None
                if metrics_database is not None and "source_person_id" in csv_service.frame.columns:
                    try:
                        metric_person_id = int(
                            str(csv_service.frame.at[index, "source_person_id"] or "0")
                        )
                    except ValueError:
                        metric_person_id = 0
                    metrics_callback = (
                        lambda metric, person_id=metric_person_id: metrics_database.record_ai_metric(
                            run_id=metrics_run_id, person_id=person_id or None, **metric
                        )
                    )
                ai_result = await asyncio.to_thread(
                    resolve_company_headquarters,
                    record.company_name,
                    settings.llm_api_key,
                    company_domain=record.domain,
                    base_url=settings.llm_base_url,
                    model=settings.llm_model,
                    provider=settings.llm_provider,
                    metrics_callback=metrics_callback,
                )
                if not ai_result.get("success"):
                    raise apollo_exc
                headquarters = str(ai_result["headquarters"])
                country = str(ai_result["country"])
                country_code = str(ai_result["country_code"])
                apollo_name = ""
                enrichment_status = CheckStatus.AI_SUCCESS
                LOGGER.info(
                    "AI headquarters fallback: %s, %s (%s)",
                    headquarters,
                    country,
                    country_code,
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
                check_status=enrichment_status,
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
