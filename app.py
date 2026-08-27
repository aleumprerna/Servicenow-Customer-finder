from __future__ import annotations

import csv
import html
import io
import json
import os
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from config import PROJECT_ROOT
from workflow.database import WorkflowDatabase
from workflow.service import launch_chrome, parse_people_csv, run_collection


DATABASE = WorkflowDatabase(PROJECT_ROOT / "data" / "workflow.db")
app = FastAPI(title="ServiceNow Partner Workflow", docs_url=None, redoc_url=None)


@app.on_event("startup")
def initialize_database() -> None:
    DATABASE.initialize()


def _escape(value: Any) -> str:
    return html.escape(str(value or ""))


def _message(request: Request) -> str:
    message = request.query_params.get("message", "")
    kind = request.query_params.get("kind", "success")
    return f'<p class="message {kind}">{_escape(message)}</p>' if message else ""


def _page(request: Request, selected_run: int | None = None) -> str:
    summaries = DATABASE.summary()
    if selected_run is None and summaries:
        selected_run = int(summaries[0]["id"])
    rows = DATABASE.report_rows(selected_run) if selected_run else []
    run = DATABASE.run(selected_run) if selected_run else None
    options = "".join(
        f'<option value="{item["id"]}" {"selected" if item["id"] == selected_run else ""}>'
        f'Run #{item["id"]} — {_escape(item["status"])} ({item["people_count"]} people)</option>'
        for item in summaries
    )
    report_rows = "".join(
        "<tr>"
        f"<td>{_escape(row['person_name'])}<br><a href=\"{_escape(row['linkedin_url'])}\" target=\"_blank\" rel=\"noreferrer\">LinkedIn</a></td>"
        f"<td>{_escape(row['company_name']) or '—'}<br><small>{_escape(row['resolution_status'])}</small></td>"
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


@app.get("/api/reports")
def reports_api(run_id: int | None = None) -> list[dict[str, Any]]:
    return DATABASE.report_rows(run_id)


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
        person_id, status="received", response=json.dumps(payload, ensure_ascii=False)[:10_000], received=True
    )
    return {"status": "stored"}
