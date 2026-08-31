from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
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
    "apollo_verified",
    "apollo_cross_verified",
    "linkedin_headline_verified",
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
    headline_key = find("headline", "title")
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
        # Older runs stored headline guesses without organization identifiers.
        # Refresh those, and any unresolved row, through Apollo People Match.
        if person["resolution_status"] in TRUSTED_COMPANY_STATUSES:
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
    resolved = [
        person for person in people
        if person["company_name"].strip()
        and person["resolution_status"] in TRUSTED_COMPANY_STATUSES
    ]
    run_dir = RUNS_DIR / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    input_path = run_dir / "companies.csv"
    output_path = run_dir / "companies_checked.csv"
    # CSVService resumes from an existing output file. A UI collection is an
    # explicit fresh run, so remove the generated checkpoint first; otherwise
    # newly resolved Apollo domains/organization URLs would never reach main.py.
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
    count = 0
    with output_path.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
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


def run_collection(database: WorkflowDatabase, run_id: int) -> None:
    """Run in FastAPI's background task after the user has logged into Chrome."""

    try:
        settings = load_settings()
        database.update_run(run_id, status="collecting", started_at=now(), collection_log="")
        input_path, output_path, resolved_count = build_pipeline_csv(database, run_id, settings)
        if not resolved_count:
            database.update_run(
                run_id,
                status="needs_attention",
                finished_at=now(),
                collection_log="No company could be resolved from the uploaded people.",
            )
            return

        environment = os.environ.copy()
        environment["INPUT_CSV"] = str(input_path)
        environment["OUTPUT_CSV"] = str(output_path)
        environment["DEBUG_DIR"] = str(input_path.parent / "debug")
        environment["SAVE_SCREENSHOTS"] = "true"
        venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        python_executable = str(venv_python) if venv_python.is_file() else sys.executable
        process = subprocess.run(
            [python_executable, "main.py", "--force"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        sync_count = sync_pipeline_results(database, run_id, output_path)
        sent_count = send_negatives_to_n8n(database, run_id, settings)
        log = (process.stdout + "\n" + process.stderr).strip()[-20_000:]
        report_rows = database.report_rows(run_id)
        completed_checks = sum(row["check_status"] == "completed" for row in report_rows)
        people_count = len(report_rows)
        status = (
            "completed"
            if process.returncode == 0 and people_count > 0 and completed_checks == people_count
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
