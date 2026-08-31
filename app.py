from __future__ import annotations

import csv
import html
import io
import json
import os
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse

from browser.servicenow import safe_filename
from config import PROJECT_ROOT, load_settings
from workflow.database import WorkflowDatabase
from workflow.presentation import parse_n8n_evidence
from workflow.service import (
    launch_chrome,
    parse_people_csv,
    run_collection,
    send_negatives_to_n8n,
)


DATABASE = WorkflowDatabase(PROJECT_ROOT / "data" / "workflow.db")
app = FastAPI(title="ServiceNow Partner Workflow", docs_url=None, redoc_url=None)


class _SafeHtml(str):
    """HTML generated only by trusted dashboard rendering helpers."""


@app.on_event("startup")
def initialize_database() -> None:
    DATABASE.initialize()


def _escape(value: Any) -> str:
    if isinstance(value, _SafeHtml):
        return str(value)
    return html.escape(str(value or ""))


def _message(request: Request) -> str:
    message = request.query_params.get("message", "")
    kind = request.query_params.get("kind", "success")
    return f'<p class="message {kind}">{_escape(message)}</p>' if message else ""


def _n8n_cell(row: dict[str, Any]) -> str:
    evidence = parse_n8n_evidence(
        str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")
    )
    parts = [
        f'<span class="badge">Delivery: {_escape(evidence.delivery_status or "Waiting")}</span>'
    ]
    if evidence.servicenow_status:
        parts.append(
            f'<div class="evidence-status">ServiceNow usage: '
            f'{_escape(evidence.servicenow_status)}</div>'
        )
    if evidence.source_type:
        parts.append(f'<span class="badge source">Source: {_escape(evidence.source_type)}</span>')
    if evidence.verification_status:
        parts.append(f'<div class="verification">{_escape(evidence.verification_status)}</div>')
    if evidence.evidence_strength:
        parts.append(f'<small>Evidence: {_escape(evidence.evidence_strength)}</small>')
    if evidence.evidence_note:
        parts.append(f'<div class="evidence-note">{_escape(evidence.evidence_note)}</div>')
    if evidence.citations:
        links: list[str] = []
        for citation in evidence.citations:
            label = f"{citation.citation_type}: {citation.title}"
            if citation.url:
                links.append(
                    f'<li><a href="{_escape(citation.url)}" target="_blank" '
                    f'rel="noreferrer">{_escape(label)}</a></li>'
                )
            else:
                links.append(f'<li>{_escape(label)} <small>(no URL supplied)</small></li>')
        parts.append(
            f'<div class="citations"><strong>Citations</strong><ul>{"".join(links)}</ul></div>'
        )
    elif evidence.research_sources:
        parts.append(
            f'<small>Research sources: {_escape(", ".join(evidence.research_sources))} '
            '(no citation URL supplied)</small>'
        )
    return "".join(parts)


def _screenshot_path(row: dict[str, Any]) -> Path | None:
    if str(row.get("servicenow_customer") or "").casefold() != "yes":
        return None
    candidates: list[Path] = []
    stored = str(row.get("screenshot_path") or "").strip()
    if stored:
        candidates.append(Path(stored))
    # Existing runs predate per-run screenshot paths. Their result images are
    # still available in the legacy debug folder.
    company = str(row.get("company_name") or "")
    candidates.append(
        PROJECT_ROOT / "debug" / "screenshots" / f"{safe_filename(company)}_results.png"
    )
    allowed_roots = (
        (PROJECT_ROOT / "data" / "runs").resolve(),
        (PROJECT_ROOT / "debug" / "screenshots").resolve(),
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and any(resolved.is_relative_to(root) for root in allowed_roots):
            return resolved
    return None


def _company_cell(row: dict[str, Any]) -> str:
    company = _escape(row.get("company_name")) or "—"
    status = _escape(row.get("resolution_status"))
    error = _escape(row.get("resolution_error"))
    parts = [company, f"<br><small>{status}</small>"]
    if error:
        parts.append(f'<br><small class="resolution-error">{error}</small>')
    if str(row.get("resolution_status") or "") not in {
        "apollo_verified",
        "apollo_cross_verified",
        "linkedin_headline_verified",
        "manual_verified",
    }:
        parts.append(
            f'<form class="company-override" method="post" '
            f'action="/people/{int(row["person_id"])}/company">'
            f'<input type="hidden" name="run_id" value="{int(row["run_id"])}">'
            '<input name="company_name" required placeholder="Correct company name">'
            '<button>Use company</button></form>'
        )
    return "".join(parts)


def _page(request: Request, selected_run: int | None = None) -> str:
    summaries = DATABASE.summary()
    if selected_run is None and summaries:
        selected_run = int(summaries[0]["id"])
    rows = DATABASE.report_rows(selected_run) if selected_run else []
    run = DATABASE.run(selected_run) if selected_run else None
    # Reuse the existing table layout while replacing the raw JSON value with
    # trusted, structured evidence HTML.
    for row in rows:
        row["company_cell"] = _SafeHtml(_company_cell(row))
        row["n8n_status"] = _SafeHtml(_n8n_cell(row))
        row["n8n_response"] = ""
        screenshot = _screenshot_path(row)
        if screenshot:
            matched_name = _escape(row.get("servicenow_matched_name"))
            row["servicenow_matched_name"] = _SafeHtml(
                f'{matched_name}<br><a class="screenshot-link" '
                f'href="/screenshots/{int(row["person_id"])}" target="_blank">View screenshot</a>'
            )
    options = "".join(
        f'<option value="{item["id"]}" {"selected" if item["id"] == selected_run else ""}>'
        f'Run #{item["id"]} — {_escape(item["status"])} ({item["people_count"]} people)</option>'
        for item in summaries
    )
    report_rows = "".join(
        "<tr>"
        f"<td>{_escape(row['person_name'])}<br><a href=\"{_escape(row['linkedin_url'])}\" target=\"_blank\" rel=\"noreferrer\">LinkedIn</a></td>"
        f"<td>{_escape(row['company_cell'])}</td>"
        f"<td>{_escape(row['servicenow_customer']) or '—'}<br><small>{_escape(row['servicenow_matched_name'])}</small></td>"
        f"<td>{_escape(row['check_status']) or 'Waiting'}</td>"
        f"<td>{_escape(row['n8n_status']) or '—'}<br><small>{_escape(row['n8n_response'])[:180]}</small></td>"
        "</tr>"
        for row in rows
    ) or '<tr><td colspan="5">No people have been uploaded yet.</td></tr>'
    run_actions = ""
    run_log = ""
    if run:
        run_actions = f"""
            <form method="post" action="/runs/{selected_run}/launch-browser"><button>1. Run instance</button></form>
            <form method="post" action="/runs/{selected_run}/collect"><button class="primary" {'disabled' if run['status'] == 'collecting' else ''}>2. Start collection</button></form>
            <form method="post" action="/runs/{selected_run}/send-n8n"><button>Send/Retry No results to n8n</button></form>
            <a class="button" href="/reports.csv?run_id={selected_run}">Download report CSV</a>
        """
        if run["collection_log"]:
            run_log = f"<details><summary>Collection log</summary><pre>{_escape(run['collection_log'])}</pre></details>"

    refresh = '<meta http-equiv="refresh" content="12">' if run and run["status"] == "collecting" else ""
    return f"""<!doctype html>
    <html><head><meta charset="utf-8">{refresh}<title>ServiceNow Partner Workflow</title>
    <style>
      body {{ font-family: system-ui, sans-serif; max-width: 1180px; margin: 32px auto; color:#1b2430; padding:0 16px; }}
      h1 {{ margin-bottom: 4px; }} .muted, small {{ color:#617084; }}
      section {{ border:1px solid #d7dee8; border-radius:10px; padding:18px; margin:18px 0; }}
      form {{ display:inline-block; margin: 4px 8px 4px 0; }} input, select, button {{ padding:9px 12px; font:inherit; }}
      button, .button {{ border:0; border-radius:6px; background:#eef2f7; color:#172033; text-decoration:none; cursor:pointer; display:inline-block; }}
      button.primary {{ background:#1463d9; color:white; }} button:disabled {{ opacity:.5; cursor:not-allowed; }}
      table {{ width:100%; border-collapse:collapse; font-size:14px; }} th, td {{ border-bottom:1px solid #e4e9ef; text-align:left; vertical-align:top; padding:10px 8px; }}
      .message {{ padding:10px; border-radius:6px; }} .success {{ background:#e5f7eb; }} .error {{ background:#fde8e8; }}
      .badge {{ display:inline-block; background:#e8edf4; border-radius:999px; padding:3px 8px; margin:0 4px 5px 0; font-size:12px; }}
      .badge.source {{ background:#e7f0ff; color:#164e9b; }} .evidence-status {{ font-weight:700; margin:5px 0; }}
      .verification {{ margin:4px 0; }} .evidence-note {{ color:#4b586a; margin:5px 0; max-width:420px; }}
      .citations {{ margin-top:7px; }} .citations ul {{ margin:3px 0 0; padding-left:18px; }}
      .screenshot-link {{ display:inline-block; margin-top:6px; font-weight:600; }}
      .resolution-error {{ color:#a12622; }}
      .company-override {{ display:flex; gap:6px; margin-top:8px; }}
      .company-override input {{ min-width:180px; padding:6px 8px; }}
      .company-override button {{ padding:6px 8px; }}
      pre {{ white-space:pre-wrap; max-height:300px; overflow:auto; background:#111827; color:#e5e7eb; padding:12px; border-radius:6px; }}
    </style></head><body>
      <h1>ServiceNow Partner Workflow</h1>
      <p class="muted">Upload people → log into ServiceNow → collect customer checks → send verified “No” results to n8n.</p>
      {_message(request)}
      <section><h2>Upload people CSV</h2>
        <p class="muted">Required headings: person name and LinkedIn URL. Your existing <code>Name</code> and <code>Profile URL</code> export works. Apollo resolves the current employer from the LinkedIn profile; headline text is stored only as report context.</p>
        <form method="post" action="/runs" enctype="multipart/form-data"><input type="file" name="file" accept=".csv,text/csv" required><button class="primary">Upload CSV</button></form>
      </section>
      <section><h2>Current run</h2>
        <form method="get" action="/"><select name="run_id" onchange="this.form.submit()">{options}</select></form>
        <div>{run_actions}</div>
        <p class="muted">Run instance opens Chrome at the ServiceNow deployment-registration page. Log in and wait until the Customer Information form is visible before Start collection.</p>
        {run_log}
      </section>
      <section><h2>Database maintenance</h2>
        <p class="muted">Clears local workflow runs and reports only. Your CSV files, configuration, Chrome profile, and source code remain unchanged.</p>
        <form method="post" action="/database/clear" onsubmit="return confirm('Delete every local workflow run and report? This cannot be undone.');"><button>Clear database</button></form>
      </section>
      <section><h2>Reports</h2>
        <table><thead><tr><th>Person</th><th>Company</th><th>ServiceNow</th><th>Automation</th><th>n8n</th></tr></thead><tbody>{report_rows}</tbody></table>
      </section>
    </body></html>"""


@app.get("/", response_class=HTMLResponse)
def home(request: Request, run_id: int | None = None) -> HTMLResponse:
    return HTMLResponse(_page(request, run_id))


@app.post("/runs")
async def upload_csv(file: UploadFile = File(...)) -> RedirectResponse:
    if not (file.filename or "").lower().endswith(".csv"):
        return RedirectResponse(url="/?kind=error&message=Please+upload+a+CSV+file", status_code=303)
    try:
        people = parse_people_csv(await file.read())
        run_id = DATABASE.create_run(file.filename or "uploaded.csv", people)
    except ValueError as exc:
        return RedirectResponse(url=f"/?kind=error&message={str(exc).replace(' ', '+')}", status_code=303)
    return RedirectResponse(url=f"/?run_id={run_id}&message=CSV+uploaded", status_code=303)


@app.post("/database/clear")
def clear_database() -> RedirectResponse:
    DATABASE.clear_all()
    return RedirectResponse(url="/?message=Local+workflow+database+cleared", status_code=303)


@app.post("/people/{person_id}/company")
def set_company_override(
    person_id: int,
    company_name: str = Form(...),
    run_id: int = Form(...),
) -> RedirectResponse:
    company = " ".join(company_name.split())
    if not company:
        return RedirectResponse(
            url=f"/?run_id={run_id}&kind=error&message=Company+name+is+required",
            status_code=303,
        )
    row = next(
        (
            item
            for item in DATABASE.report_rows(run_id)
            if int(item["person_id"]) == person_id
        ),
        None,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Person not found in this run")
    DATABASE.update_person_resolution(
        person_id,
        company_name=company,
        status="manual_verified",
        error="Company supplied through dashboard override",
    )
    return RedirectResponse(
        url=f"/?run_id={run_id}&message=Company+override+saved",
        status_code=303,
    )


@app.post("/runs/{run_id}/launch-browser")
def open_browser(run_id: int) -> RedirectResponse:
    if not DATABASE.run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        launch_chrome()
        message, kind = "Chrome+opened.+Log+in+and+open+Customer+Information.", "success"
    except FileNotFoundError as exc:
        message, kind = str(exc).replace(" ", "+"), "error"
    return RedirectResponse(url=f"/?run_id={run_id}&kind={kind}&message={message}", status_code=303)


@app.post("/runs/{run_id}/collect")
def collect(run_id: int, background_tasks: BackgroundTasks) -> RedirectResponse:
    run = DATABASE.run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] == "collecting":
        return RedirectResponse(url=f"/?run_id={run_id}&message=Collection+is+already+running", status_code=303)
    background_tasks.add_task(run_collection, DATABASE, run_id)
    return RedirectResponse(url=f"/?run_id={run_id}&message=Collection+started", status_code=303)


@app.post("/runs/{run_id}/send-n8n")
def send_to_n8n(run_id: int) -> RedirectResponse:
    if not DATABASE.run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    for row in DATABASE.report_rows(run_id):
        evidence = parse_n8n_evidence(
            str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")
        )
        if (
            str(row.get("servicenow_customer") or "").lower() == "no"
            and row.get("n8n_status") in {"sent", "received"}
            and evidence.parse_error
        ):
            DATABASE.mark_n8n_for_retry(int(row["person_id"]))
    settings = load_settings()
    sent = send_negatives_to_n8n(DATABASE, run_id, settings)
    message = f"Sent+{sent}+No+result(s)+to+n8n"
    kind = "success" if sent else "error"
    return RedirectResponse(url=f"/?run_id={run_id}&kind={kind}&message={message}", status_code=303)


@app.get("/api/reports")
def reports_api(run_id: int | None = None) -> list[dict[str, Any]]:
    return DATABASE.report_rows(run_id)


@app.get("/screenshots/{person_id}")
def view_screenshot(person_id: int) -> FileResponse:
    row = next(
        (item for item in DATABASE.report_rows() if int(item["person_id"]) == person_id),
        None,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Report row not found")
    screenshot = _screenshot_path(row)
    if not screenshot:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(
        screenshot,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{screenshot.name}"'},
    )


@app.get("/reports.csv")
def reports_csv(run_id: int | None = None) -> StreamingResponse:
    rows = DATABASE.report_rows(run_id)
    stream = io.StringIO()
    if rows:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return StreamingResponse(
        iter([stream.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=servicenow-workflow-report.csv"},
    )


@app.post("/api/webhooks/n8n")
async def n8n_callback(request: Request) -> dict[str, str]:
    token = os.getenv("N8N_CALLBACK_TOKEN", "").strip()
    if token and request.headers.get("X-Workflow-Token") != token:
        raise HTTPException(status_code=401, detail="Invalid workflow token")
    try:
        payload = await request.json()
        person_id = int(payload["person_id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="JSON body must include integer person_id") from None
    DATABASE.set_n8n_result(
        person_id, status="received", response=json.dumps(payload, ensure_ascii=False), received=True
    )
    return {"status": "stored"}
