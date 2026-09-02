from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from typing import Any

import requests

from clients.apollo import ApolloClient
from config import PROJECT_ROOT, Settings, load_settings
from workflow.database import WorkflowDatabase, now
from workflow.person_company import PersonCompanyResolver


RUNS_DIR = PROJECT_ROOT / "data" / "runs"
TRUSTED_COMPANY_STATUSES = {
    "apollo_structurally_verified",
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
    if not person_key or not linkedin_key:
        raise ValueError(
            "CSV needs person name and LinkedIn URL columns (for example: Name, Profile URL)"
        )

    people: list[dict[str, Any]] = []
    for source_row, row in enumerate(reader, start=2):
        person_name = (row.get(person_key) or "").strip()
        linkedin_url = (row.get(linkedin_key) or "").strip()
        if not person_name and not linkedin_url:
            continue
        if not person_name or not linkedin_url:
            raise ValueError(f"Row {source_row} needs both a person name and LinkedIn URL")
        people.append(
            {
                "person_name": person_name,
                "linkedin_url": linkedin_url,
                "company_name": (row.get(company_key) or "").strip() if company_key else "",
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
    resolver = PersonCompanyResolver(_apollo(settings))
    people = database.people_for_run(run_id)
    for person in people:
        # A trusted company is already resolved. Reusing it keeps repeat
        # enrichment runs scoped to newly corrected or unresolved records.
        if (
            person["company_name"].strip()
            and person["resolution_status"] in TRUSTED_COMPANY_STATUSES
        ):
            continue
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
    # discard the last automation-ready checkpoint.
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


def build_automation_checkpoint(
    database: WorkflowDatabase, run_id: int
) -> tuple[Path, Path, int]:
    """Rebuild the browser queue from every database row that has usable country data."""

    rows = [
        row
        for row in database.report_rows(run_id)
        if str(row.get("country_code") or "").strip()
        and str(row.get("check_status") or "").casefold()
        in {"apollo_success", "searching", "completed", "manual_review", "error"}
    ]
    run_dir = RUNS_DIR / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    input_path = run_dir / "companies.csv"
    output_path = run_dir / "companies_checked.csv"
    input_fields = [
        "company_name", "linkedin_url", "source_person_id", "person_name",
        "source_person_linkedin_url", "headline", "domain",
    ]
    output_fields = [
        "company_name", "linkedin_url", "headquarters", "country", "country_code",
        "apollo_company_name", "servicenow_customer", "servicenow_matched_name",
        "servicenow_screenshot", "match_score", "check_status", "error_message",
        "checked_at", "domain", "country_override", "source_person_id", "person_name",
        "source_person_linkedin_url", "headline",
    ]
    with input_path.open("w", newline="", encoding="utf-8") as input_file, output_path.open(
        "w", newline="", encoding="utf-8"
    ) as output_file:
        input_writer = csv.DictWriter(input_file, fieldnames=input_fields)
        output_writer = csv.DictWriter(output_file, fieldnames=output_fields)
        input_writer.writeheader()
        output_writer.writeheader()
        for row in rows:
            identity = {
                "company_name": row.get("company_name") or "",
                "linkedin_url": row.get("company_linkedin_url") or "",
                "source_person_id": row.get("person_id") or "",
                "person_name": row.get("person_name") or "",
                "source_person_linkedin_url": row.get("linkedin_url") or "",
                "headline": row.get("headline") or "",
                "domain": row.get("company_domain") or "",
            }
            input_writer.writerow(identity)
            output_writer.writerow(
                {
                    **identity,
                    "headquarters": row.get("headquarters") or "",
                    "country": row.get("country") or "",
                    "country_code": row.get("country_code") or "",
                    "apollo_company_name": row.get("apollo_company_name") or "",
                    "servicenow_customer": row.get("servicenow_customer") or "",
                    "servicenow_matched_name": row.get("servicenow_matched_name") or "",
                    "servicenow_screenshot": row.get("screenshot_path") or "",
                    "match_score": row.get("match_score") or "",
                    "check_status": row.get("check_status") or "apollo_success",
                    "error_message": row.get("error_message") or "",
                    "checked_at": row.get("checked_at") or "",
                    "country_override": "",
                }
            )
    return input_path, output_path, len(rows)


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


def n8n_payload(check: dict[str, Any], app_base_url: str) -> dict[str, Any]:
    """Stable webhook contract; n8n can use person_id to send an async callback."""

    return {
        "event": "servicenow.customer_not_found",
        "run_id": check["run_id"],
        "person_id": check["person_id"],
        "person_name": check["person_name"],
        "linkedin_url": check["linkedin_url"],
        "headline": check["headline"],
        "company_name": check["company_name"],
        "servicenow_customer": check["servicenow_customer"],
        "servicenow_matched_name": check["servicenow_matched_name"],
        "match_score": check["match_score"],
        "check_status": check["check_status"],
        "headquarters": check["headquarters"],
        "country": check["country"],
        "country_code": check["country_code"],
        "apollo_company_name": check["apollo_company_name"],
        "checked_at": check["checked_at"],
        "callback_url": app_base_url.rstrip("/") + "/api/webhooks/n8n",
    }


def send_negatives_to_n8n(database: WorkflowDatabase, run_id: int, settings: Settings) -> int:
    checks = database.unsent_negative_checks(run_id)
    if not checks:
        return 0
    if not settings.n8n_webhook_url:
        for check in checks:
            database.set_n8n_result(
                check["person_id"], status="not_configured", response="N8N_WEBHOOK_URL is not configured"
            )
        return 0
    def deliver(check: dict[str, Any]) -> bool:
        payload = n8n_payload(check, settings.app_base_url)
        try:
            response = requests.post(settings.n8n_webhook_url, json=payload, timeout=30)
            response.raise_for_status()
            # Keep valid JSON intact. Truncating a large response can cut through
            # a string and make status/citation fields impossible to parse.
            body = response.text
            database.set_n8n_result(check["person_id"], status="sent", response=body, sent=True)
            return True
        except requests.RequestException as exc:
            database.set_n8n_result(check["person_id"], status="failed", response=str(exc))
            return False

    # n8n may keep a webhook request open until its workflow finishes. Deliver
    # independently so one slow workflow cannot block every later company.
    with ThreadPoolExecutor(max_workers=min(4, len(checks))) as executor:
        return sum(executor.map(deliver, checks))


def _pipeline_process(
    *, input_path: Path, output_path: Path, stage: str, force: bool,
    progress_callback: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["INPUT_CSV"] = str(input_path)
    environment["OUTPUT_CSV"] = str(output_path)
    environment["DEBUG_DIR"] = str(input_path.parent / "debug")
    environment["SAVE_SCREENSHOTS"] = "true"
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
    """Resolve people and enrich organizations without starting browser automation."""

    try:
        settings = load_settings()
        database.update_run(run_id, status="enriching", started_at=now(), collection_log="")
        input_path, output_path, resolved_count = build_pipeline_csv(database, run_id, settings)
        if not resolved_count:
            report_rows = database.report_rows(run_id)
            already_enriched = any(
                str(row.get("check_status") or "").casefold()
                in {"apollo_success", "searching", "completed", "manual_review", "error"}
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
        )
        synced_count = sync_pipeline_results(database, run_id, output_path)
        report_rows = database.report_rows(run_id)
        enriched_count = sum(
            str(row.get("check_status") or "").casefold()
            in {"apollo_success", "searching", "completed", "manual_review", "error"}
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


def run_collection(database: WorkflowDatabase, run_id: int) -> None:
    """Run only ServiceNow browser automation against enriched records."""

    try:
        settings = load_settings()
        database.update_run(run_id, status="collecting", started_at=now(), collection_log="")
        input_path, output_path, ready_count = build_automation_checkpoint(database, run_id)
        if not ready_count:
            database.update_run(
                run_id,
                status="needs_attention",
                finished_at=now(),
                collection_log="No enriched records found. Click Enrich records first.",
            )
            return

        process = _pipeline_process(
            input_path=input_path,
            output_path=output_path,
            stage="--automation-only",
            force=False,
            progress_callback=lambda: sync_pipeline_results(database, run_id, output_path),
        )
        sync_count = sync_pipeline_results(database, run_id, output_path)
        sent_count = send_negatives_to_n8n(database, run_id, settings)
        log = (process.stdout + "\n" + process.stderr).strip()[-20_000:]
        report_rows = database.report_rows(run_id)
        completed_checks = sum(row["check_status"] == "completed" for row in report_rows)
        people_count = len(report_rows)
        resolved_count = sum(
            bool(str(row["company_name"] or "").strip())
            and row["resolution_status"] in TRUSTED_COMPANY_STATUSES
            for row in report_rows
        )
        status = (
            "completed"
            if process.returncode == 0
            and resolved_count == people_count
            and completed_checks == resolved_count
            else "needs_attention"
        )
        summary = (
            f"Resolved {resolved_count}/{people_count}; ServiceNow completed "
            f"{completed_checks}/{people_count}; synced {sync_count}; sent {sent_count} "
            f"negative result(s) to n8n.\n{log}"
        )
        database.update_run(run_id, status=status, finished_at=now(), collection_log=summary)
    except Exception as exc:
        database.update_run(run_id, status="failed", finished_at=now(), collection_log=str(exc))


def chrome_executable() -> Path:
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Google Chrome was not found in a standard installation path")


def launch_chrome() -> None:
    subprocess.Popen(
        [
            str(chrome_executable()),
            "--remote-debugging-port=9222",
            "--user-data-dir=C:\\playwright-servicenow-profile",
            "https://partnerportal.servicenow.com/partnerhome?id=deployment_registration&spa=1",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
