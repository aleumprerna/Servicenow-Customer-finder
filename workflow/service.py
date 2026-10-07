from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path
from typing import Any

from clients.apollo import ApolloClient
from config import PROJECT_ROOT, Settings, load_settings
from workflow.database import WorkflowDatabase, now
from workflow.person_company import PersonCompanyResolver


RUNS_DIR = PROJECT_ROOT / "data" / "runs"
TRUSTED_COMPANY_STATUSES = {
    "apollo_structurally_verified",
    "csv_supplied",
    "manual_verified",
}


def _clean_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def parse_people_csv(raw: bytes) -> list[dict[str, Any]]:
    """Accept the user's LinkedIn-export headings as well as simple app headings."""

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be saved as UTF-8") from exc
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise ValueError("The uploaded CSV has no header row")
    headers = {_clean_header(header): header for header in reader.fieldnames if header}

    def find(*names: str) -> str | None:
        return next((headers[name] for name in names if name in headers), None)

    person_key = find("personname", "name", "fullname")
    linkedin_key = find("linkedinurl", "profileurl", "linkedinprofileurl", "profile")
    company_key = find("companyname", "company", "organization", "employer")
    headline_key = find("headline", "headlinecurrentrole", "currentrole", "title")
    if not company_key and (not person_key or not linkedin_key):
        raise ValueError(
            "CSV needs a company column, or person name and LinkedIn URL columns "
            "(for example: Company, or Name and Profile URL)"
        )

    people: list[dict[str, Any]] = []
    for source_row, row in enumerate(reader, start=2):
        person_name = (row.get(person_key) or "").strip() if person_key else ""
        linkedin_url = (row.get(linkedin_key) or "").strip() if linkedin_key else ""
        company_name = (row.get(company_key) or "").strip() if company_key else ""
        if not person_name and not linkedin_url and not company_name:
            continue
        if not company_name and (not person_name or not linkedin_url):
            raise ValueError(
                f"Row {source_row} needs a company name, or both a person name and LinkedIn URL"
            )
        if company_name and not person_name and not linkedin_url:
            person_name = "x_person"
        people.append(
            {
                "person_name": person_name,
                "linkedin_url": linkedin_url,
                "company_name": company_name,
                "headline": (row.get(headline_key) or "").strip() if headline_key else "",
                "raw_input": row,
            }
        )
    if not people:
        raise ValueError("The uploaded CSV has no usable person rows")
    return people


def _apollo(settings: Settings) -> ApolloClient:
    return ApolloClient(
        api_key=settings.apollo_api_key,
        base_url=settings.apollo_base_url,
        timeout_seconds=settings.apollo_timeout_seconds,
        max_retries=settings.apollo_max_retries,
        match_threshold=settings.apollo_match_threshold,
    )


def resolve_people(database: WorkflowDatabase, run_id: int, settings: Settings) -> list[dict[str, Any]]:
    resolver: PersonCompanyResolver | None = None
    people = database.people_for_run(run_id)
    for person in people:
        # A trusted company is already resolved. Reusing it keeps repeat
        # enrichment runs scoped to newly corrected or unresolved records.
        if (
            person["company_name"].strip()
            and person["resolution_status"] in TRUSTED_COMPANY_STATUSES
        ):
            continue
        supplied_company = person["supplied_company_name"].strip()
        if supplied_company:
            database.update_person_resolution(
                person["id"], company_name=supplied_company, status="csv_supplied"
            )
            continue
        if resolver is None:
            resolver = PersonCompanyResolver(_apollo(settings))
        result = resolver.resolve(
            person_name=person["person_name"],
            linkedin_url=person["linkedin_url"],
            supplied_company_name=person["supplied_company_name"],
            headline=person["headline"],
        )
        database.update_person_resolution(
            person["id"], company_name=result.company_name, status=result.status, error=result.error,
            domain=result.domain, company_linkedin_url=result.company_linkedin_url,
        )
    return database.people_for_run(run_id)


def build_pipeline_csv(database: WorkflowDatabase, run_id: int, settings: Settings) -> tuple[Path, Path, int]:
    people = resolve_people(database, run_id, settings)
    reports = {int(row["person_id"]): row for row in database.report_rows(run_id)}
    resolved: list[dict[str, Any]] = []
    for person in people:
        if (
            not person["company_name"].strip()
            or person["resolution_status"] not in TRUSTED_COMPANY_STATUSES
        ):
            continue
        current = reports.get(int(person["id"])) or {}
        check_status = str(current.get("check_status") or "").casefold()
        checked_company = str(current.get("check_company_name") or "").strip()
        needs_enrichment = (
            not check_status
            or check_status in {"pending", "apollo_failed"}
            or checked_company.casefold() != person["company_name"].strip().casefold()
        )
        if needs_enrichment:
            resolved.append(person)
    run_dir = RUNS_DIR / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    input_path = run_dir / "companies.csv"
    output_path = run_dir / "companies_checked.csv"
    # CSVService resumes from an existing output file. Remove the checkpoint
    # only when there is actual work to run; a no-op Enrich click must not
    # discard the last enrichment checkpoint.
    if resolved:
        output_path.unlink(missing_ok=True)
    fields = [
        "company_name", "linkedin_url", "source_person_id", "person_name",
        "source_person_linkedin_url", "headline", "domain",
    ]
    with input_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for person in resolved:
            # The personal profile is retained separately. Apollo's organization
            # identifiers are the only values passed to organization enrichment.
            writer.writerow(
                {
                    "company_name": person["company_name"],
                    "linkedin_url": person["company_linkedin_url"],
                    "source_person_id": person["id"],
                    "person_name": person["person_name"],
                    "source_person_linkedin_url": person["linkedin_url"],
                    "headline": person["headline"],
                    "domain": person["company_domain"],
                }
            )
    return input_path, output_path, len(resolved)


def sync_pipeline_results(database: WorkflowDatabase, run_id: int, output_path: Path) -> int:
    if not output_path.exists():
        return 0
    # Read the checkpoint into memory and close it before touching SQLite.
    # Keeping the CSV handle open across per-row database writes prevents
    # CSVService's atomic os.replace() from succeeding on Windows.
    try:
        checkpoint = output_path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, PermissionError):
        return 0
    count = 0
    for row in csv.DictReader(StringIO(checkpoint)):
        try:
            person_id = int(row.get("source_person_id", ""))
        except ValueError:
            continue
        values = {
            "company_name": row.get("company_name", ""),
            "servicenow_customer": row.get("servicenow_customer", ""),
            "servicenow_matched_name": row.get("servicenow_matched_name", ""),
            "screenshot_path": row.get("servicenow_screenshot", ""),
            "match_score": row.get("match_score", ""),
            "check_status": row.get("check_status", ""),
            "headquarters": row.get("headquarters", ""),
            "country": row.get("country", ""),
            "country_code": row.get("country_code", ""),
            "apollo_company_name": row.get("apollo_company_name", ""),
            "error_message": row.get("error_message", ""),
            "checked_at": row.get("checked_at", ""),
        }
        database.upsert_check(person_id, run_id, values)
        count += 1
    return count


def _pipeline_process(
    *, input_path: Path, output_path: Path, stage: str, force: bool,
    progress_callback: Any | None = None,
    metrics_database_path: Path | None = None,
    metrics_run_id: int | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["INPUT_CSV"] = str(input_path)
    environment["OUTPUT_CSV"] = str(output_path)
    if metrics_database_path is not None and metrics_run_id is not None:
        environment["AI_METRICS_DATABASE"] = str(metrics_database_path)
        environment["AI_METRICS_RUN_ID"] = str(metrics_run_id)
    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    python_executable = str(venv_python) if venv_python.is_file() else sys.executable
    command = [python_executable, "main.py"]
    if force:
        command.append("--force")
    command.append(stage)
    log_path = input_path.parent / ".pipeline.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        while process.poll() is None:
            if progress_callback:
                progress_callback()
            time.sleep(0.5)
        returncode = process.wait()
    if progress_callback:
        progress_callback()
    output = log_path.read_text(encoding="utf-8", errors="replace")
    return subprocess.CompletedProcess(command, returncode, stdout=output, stderr="")


def run_enrichment(database: WorkflowDatabase, run_id: int) -> None:
    """Resolve people and enrich organizations and save the results."""

    try:
        settings = load_settings()
        database.update_run(run_id, status="enriching", started_at=now(), collection_log="")
        input_path, output_path, resolved_count = build_pipeline_csv(database, run_id, settings)
        if not resolved_count:
            report_rows = database.report_rows(run_id)
            already_enriched = any(
                str(row.get("check_status") or "").casefold()
                in {"apollo_success", "ai_success", "searching", "completed", "manual_review", "error"}
                for row in report_rows
            )
            database.update_run(
                run_id,
                status="enriched" if already_enriched else "needs_attention",
                finished_at=now(),
                collection_log=(
                    "No company is waiting for organization enrichment."
                    if already_enriched
                    else "No confirmed company is ready for organization enrichment. "
                    "Review Apollo-only/conflicting rows and confirm the correct company."
                ),
            )
            return

        process = _pipeline_process(
            input_path=input_path,
            output_path=output_path,
            stage="--enrich-only",
            force=True,
            progress_callback=lambda: sync_pipeline_results(database, run_id, output_path),
            metrics_database_path=database.path,
            metrics_run_id=run_id,
        )
        synced_count = sync_pipeline_results(database, run_id, output_path)
        report_rows = database.report_rows(run_id)
        enriched_count = sum(
            str(row.get("check_status") or "").casefold()
            in {"apollo_success", "ai_success", "searching", "completed", "manual_review", "error"}
            for row in report_rows
        )
        pending_enrichment = sum(
            row["resolution_status"] in TRUSTED_COMPANY_STATUSES
            and str(row.get("check_status") or "").casefold()
            in {"", "pending", "apollo_failed"}
            for row in report_rows
        )
        status = (
            "enriched"
            if process.returncode == 0 and enriched_count and not pending_enrichment
            else "needs_attention"
        )
        log = (process.stdout + "\n" + process.stderr).strip()[-20_000:]
        database.update_run(
            run_id,
            status=status,
            finished_at=now(),
            collection_log=(
                f"Company enrichment queued {resolved_count}; checkpointed {synced_count}; "
                f"{enriched_count} total records are enrichment-ready.\n{log}"
            ),
        )
    except Exception as exc:
        database.update_run(run_id, status="failed", finished_at=now(), collection_log=str(exc))
