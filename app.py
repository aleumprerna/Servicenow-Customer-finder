from __future__ import annotations

import csv
import html
import io
import json
import os
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from browser.servicenow import safe_filename
from browser.session_monitor import LoginSessionMonitor
from config import PROJECT_ROOT, load_settings
from services.ai_company_resolver import resolve_company_from_web
from workflow.database import WorkflowDatabase
from workflow.presentation import parse_n8n_evidence
from workflow.service import (
    TRUSTED_COMPANY_STATUSES,
    launch_chrome,
    parse_people_csv,
    run_collection,
    run_enrichment,
    send_negatives_to_n8n,
)


DATABASE = WorkflowDatabase(PROJECT_ROOT / "data" / "workflow.db")
LOGIN_MONITOR = LoginSessionMonitor()
app = FastAPI(title="ServiceNow Partner Workflow", docs_url=None, redoc_url=None)

ENRICHED_CHECK_STATUSES = {"apollo_success", "searching", "completed", "manual_review", "error"}
ENRICHMENT_TERMINAL_STATUSES = ENRICHED_CHECK_STATUSES | {"apollo_failed"}
AUTOMATION_FINISHED_STATUSES = {"completed", "manual_review", "error"}


class _SafeHtml(str):
    """HTML generated only by trusted dashboard rendering helpers."""


@app.on_event("startup")
def initialize_database() -> None:
    DATABASE.initialize()


@app.on_event("startup")
async def start_login_monitor() -> None:
    await LOGIN_MONITOR.start(load_settings().chrome_cdp_url)


@app.on_event("shutdown")
async def stop_login_monitor() -> None:
    await LOGIN_MONITOR.stop()


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
    if str(row.get("resolution_status") or "") not in TRUSTED_COMPANY_STATUSES:
        parts.append(
            f'<form class="company-override" method="post" '
            f'action="/people/{int(row["person_id"])}/company">'
            f'<input type="hidden" name="run_id" value="{int(row["run_id"])}">'
            f'<div class="company-input-group">'
            f'<input name="company_name" required placeholder="Correct company name">'
            f'<button type="button" class="ai-resolve-btn" data-person-id="{int(row["person_id"])}" '
            f'data-run-id="{int(row["run_id"])}" title="Auto-find company with AI web search from LinkedIn">'
            f'✨ AI</button>'
            f'</div>'
            f'<div class="ai-status-msg" style="display:none;"></div>'
            f'<button>Use company</button></form>'
        )
    return "".join(parts)


def _pretty_status(value: Any, fallback: str = "Not available") -> str:
    text = str(value or "").strip()
    return text.replace("_", " ").replace("-", " ").title() if text else fallback


def _workflow_counts(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    enriched = sum(
        str(row.get("check_status") or "").casefold() in ENRICHED_CHECK_STATUSES
        for row in rows
    )
    automated = sum(
        str(row.get("check_status") or "").casefold() in AUTOMATION_FINISHED_STATUSES
        for row in rows
    )
    approved = sum(
        str(row.get("resolution_status") or "") in TRUSTED_COMPANY_STATUSES for row in rows
    )
    return enriched, automated, approved


def _enrichment_processed_count(rows: list[dict[str, Any]]) -> int:
    return sum(
        str(row.get("check_status") or "").casefold() in ENRICHMENT_TERMINAL_STATUSES
        for row in rows
    )


def _company_approval_status(row: dict[str, Any]) -> str:
    if str(row.get("resolution_status") or "") in TRUSTED_COMPANY_STATUSES:
        return ""
    return '<span class="company-approval-status">Needs approval</span>'


def _relationship(row: dict[str, Any], evidence: Any) -> str:
    researched = str(evidence.servicenow_status or "").strip()
    normalized = researched.casefold()
    if normalized in {"yes", "customer", "confirmed customer"}:
        return "Customer"
    if "partner" in normalized:
        return researched
    if researched and normalized not in {"not verified", "unknown", "no"}:
        return researched

    integration = str(row.get("servicenow_customer") or "").strip().casefold()
    if integration == "yes":
        return "Customer"
    if normalized == "not verified":
        return "Not verified"
    if integration == "no":
        return "Not found"
    if integration == "unknown":
        return "Needs review"
    return "Pending"


def _relationship_tone(label: str) -> str:
    normalized = label.casefold()
    if any(word in normalized for word in ("customer", "partner", "likely", "yes")):
        return "positive"
    if any(word in normalized for word in ("error", "review", "unknown")):
        return "warning"
    if any(word in normalized for word in ("not found", "not verified", "no")):
        return "neutral"
    return "pending"


def _report_card(row: dict[str, Any]) -> str:
    evidence = parse_n8n_evidence(
        str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")
    )
    relationship = _relationship(row, evidence)
    source_tags = list(evidence.source_tags)
    if str(row.get("servicenow_customer") or "").casefold() == "yes":
        source_tags.insert(0, "ServiceNow integration app")
    source_tags = list(dict.fromkeys(source_tags))
    tag_html = "".join(
        f'<span class="source-tag">{_escape(tag)}</span>' for tag in source_tags
    ) or '<span class="source-tag source-empty">No confirming source</span>'

    company = _escape(row.get("company_name")) or "Company unresolved"
    person = _escape(row.get("person_name")) or "Unnamed person"
    linkedin = _escape(row.get("linkedin_url"))
    person_html = (
        f'<a class="person-name" href="{linkedin}" target="_blank" '
        f'rel="noreferrer">{person}</a>'
        if linkedin
        else f'<span class="person-name">{person}</span>'
    )
    company_approval_status = _company_approval_status(row)
    location = ", ".join(
        item for item in (_escape(row.get("headquarters")), _escape(row.get("country"))) if item
    ) or "Not available"
    confidence = _escape(row.get("match_score"))
    confidence = f"{confidence}%" if confidence else "Not available"
    screenshot = _screenshot_path(row)

    citations = ""
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
                links.append(f'<li>{_escape(label)} <span class="muted">(URL unavailable)</span></li>')
        citations = f'<div class="evidence-block"><h4>Citations</h4><ul class="citation-list">{"".join(links)}</ul></div>'

    screenshot_html = ""
    if screenshot:
        screenshot_url = f'/screenshots/{int(row["person_id"])}'
        screenshot_html = f"""
          <div class="evidence-block screenshot-block">
            <h4>ServiceNow screenshot</h4>
            <a href="{screenshot_url}" target="_blank" title="Open full-size screenshot">
              <img src="{screenshot_url}" loading="lazy" alt="ServiceNow match screenshot for {company}">
            </a>
          </div>"""

    note_parts = []
    if evidence.verification_status:
        note_parts.append(f'<p class="verification">{_escape(evidence.verification_status)}</p>')
    if evidence.evidence_strength:
        note_parts.append(f'<p><strong>Evidence strength:</strong> {_escape(evidence.evidence_strength)}</p>')
    if evidence.evidence_note:
        note_parts.append(f'<p class="evidence-note">{_escape(evidence.evidence_note)}</p>')
    if evidence.parse_error:
        note_parts.append('<p class="detail-alert">The stored n8n response could not be fully read.</p>')
    notes = "".join(note_parts) or '<p class="muted">No additional verification notes.</p>'

    resolution = _company_cell(row)
    errors = "".join(
        f'<p class="detail-alert">{_escape(value)}</p>'
        for value in (row.get("resolution_error"), row.get("error_message")) if value
    )
    linkedin_html = (
        f'<a href="{linkedin}" target="_blank" rel="noreferrer">Open LinkedIn profile</a>'
        if linkedin else "Not available"
    )

    return f"""
      <details class="report-card">
        <summary>
          <span class="summary-main">
            {person_html}
            <span class="company-name">{company}</span>
            {company_approval_status}
          </span>
          <span class="relationship {_relationship_tone(relationship)}">
            <span class="status-dot"></span>{_escape(relationship)}
          </span>
          <span class="source-tags">{tag_html}</span>
          <span class="expand-label"><span class="show-more">View details</span><span class="show-less">Close</span><span class="chevron" aria-hidden="true">⌄</span></span>
        </summary>
        <div class="report-details">
          <div class="detail-grid">
            <div class="detail-group">
              <h3>Person &amp; company</h3>
              <dl>
                <div><dt>LinkedIn</dt><dd>{linkedin_html}</dd></div>
                <div><dt>Headline</dt><dd>{_escape(row.get("headline")) or "Not available"}</dd></div>
                <div><dt>Company resolution</dt><dd>{resolution}</dd></div>
                <div><dt>Apollo company</dt><dd>{_escape(row.get("apollo_company_name")) or "Not available"}</dd></div>
                <div><dt>Location</dt><dd>{location}</dd></div>
                <div><dt>Match confidence</dt><dd>{confidence}</dd></div>
              </dl>
            </div>
            <div class="detail-group">
              <h3>Workflow</h3>
              <dl>
                <div><dt>Integration app result</dt><dd>{_pretty_status(row.get("servicenow_customer"), "Pending")}</dd></div>
                <div><dt>Matched name</dt><dd>{_escape(row.get("servicenow_matched_name")) or "Not available"}</dd></div>
                <div><dt>Collection status</dt><dd>{_pretty_status(row.get("check_status"), "Waiting")}</dd></div>
                <div><dt>n8n delivery</dt><dd>{_pretty_status(evidence.delivery_status, "Waiting")}</dd></div>
                <div><dt>Checked</dt><dd>{_escape(row.get("checked_at")) or "Not yet"}</dd></div>
              </dl>
              {errors}
            </div>
          </div>
          <div class="evidence-section">
            <div class="evidence-block"><h3>Verification</h3>{notes}</div>
            {citations}
            {screenshot_html}
          </div>
        </div>
      </details>"""


def _table_person_link(row: dict[str, Any]) -> str:
    person = _escape(row.get("person_name")) or "Unnamed person"
    linkedin = _escape(row.get("linkedin_url"))
    if not linkedin:
        return f'<span class="table-person">{person}</span>'
    return (
        f'<a class="table-person" href="{linkedin}" target="_blank" '
        f'rel="noreferrer">{person}</a>'
    )


def _status_pill(label: str, tone: str = "neutral") -> str:
    return f'<span class="status-pill {tone}">{_escape(label)}</span>'


def _enrichment_table(rows: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for row in rows:
        trusted = str(row.get("resolution_status") or "") in TRUSTED_COMPANY_STATUSES
        approval = _status_pill("Ready", "success") if trusted else _status_pill("Needs approval", "warning")
        company = _escape(row.get("company_name")) or "Company unresolved"
        resolution_details = ""
        if not trusted:
            reason = _escape(row.get("resolution_error")) or "Apollo could not provide enough evidence to verify this company automatically."
            resolution_details = f"""
              <details class="table-review">
                <summary>Review company</summary>
                <p>{reason}</p>
                <form class="company-override" method="post" action="/people/{int(row['person_id'])}/company">
                  <input type="hidden" name="run_id" value="{int(row['run_id'])}">
                  <div class="company-input-group">
                    <input name="company_name" required placeholder="Correct company name">
                    <button type="button" class="ai-resolve-btn" data-person-id="{int(row['person_id'])}" data-run-id="{int(row['run_id'])}" title="Auto-find company with AI web search from LinkedIn">
                      ✨ AI
                    </button>
                  </div>
                  <div class="ai-status-msg" style="display:none;"></div>
                  <button>Approve company</button>
                </form>
              </details>"""
        location = ", ".join(
            item for item in (_escape(row.get("headquarters")), _escape(row.get("country"))) if item
        ) or "Not available"
        body.append(
            f"""
            <tr>
              <td>{_table_person_link(row)}<span class="cell-secondary">{_escape(row.get('headline')) or 'No headline returned'}</span></td>
              <td><strong>{company}</strong><span class="cell-secondary">{_escape(row.get('apollo_company_name')) or 'No Apollo organization name'}</span></td>
              <td>{approval}{resolution_details}</td>
              <td>{location}</td>
            </tr>"""
        )
    if not body:
        body.append('<tr><td class="table-empty" colspan="4">Upload a CSV to see enriched records.</td></tr>')
    return f"""
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Person</th><th>Resolved company</th><th>Approval</th><th>Company location</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table></div>"""


def _automation_table(rows: list[dict[str, Any]]) -> str:
    body: list[str] = []
    status_labels = {
        "apollo_success": ("Ready to run", "info"),
        "completed": ("Completed", "success"),
        "manual_review": ("Manual review", "warning"),
        "error": ("Error", "danger"),
    }
    for row in rows:
        raw_status = str(row.get("check_status") or "")
        label, tone = status_labels.get(raw_status, ("Waiting for enrichment", "neutral"))
        customer = str(row.get("servicenow_customer") or "")
        customer_tone = "success" if customer.casefold() == "yes" else "neutral"
        customer_html = _status_pill(_pretty_status(customer, "Not checked"), customer_tone)
        score = _escape(row.get("match_score"))
        match = _escape(row.get("servicenow_matched_name")) or "No match recorded"
        if score:
            match += f' <span class="cell-secondary">{score}% confidence</span>'
        evidence_link = "Not available"
        if _screenshot_path(row):
            evidence_link = f'<a href="/screenshots/{int(row["person_id"])}" target="_blank">View screenshot</a>'
        error = _escape(row.get("error_message"))
        error_html = f'<span class="cell-error">{error}</span>' if error else ""
        body.append(
            f"""
            <tr>
              <td>{_table_person_link(row)}<span class="cell-secondary">{_escape(row.get('company_name')) or 'Company unresolved'}</span></td>
              <td>{_status_pill(label, tone)}{error_html}</td>
              <td>{customer_html}</td>
              <td>{match}</td>
              <td>{_escape(row.get('checked_at')) or 'Not checked yet'}</td>
              <td>{evidence_link}</td>
            </tr>"""
        )
    if not body:
        body.append('<tr><td class="table-empty" colspan="6">No records are ready for web automation.</td></tr>')
    return f"""
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Person &amp; company</th><th>Automation status</th><th>ServiceNow customer</th><th>Matched result</th><th>Checked</th><th>Evidence</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table></div>"""


def _final_results_table(rows: list[dict[str, Any]]) -> str:
    records: list[str] = []
    for row in rows:
        evidence = parse_n8n_evidence(
            str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")
        )
        relationship = _relationship(row, evidence)
        relationship_tone = _relationship_tone(relationship)
        source_tags = list(evidence.source_tags)
        if str(row.get("servicenow_customer") or "").casefold() == "yes":
            source_tags.insert(0, "ServiceNow integration app")
        source_tags = list(dict.fromkeys(source_tags))
        sources = "".join(
            f'<span class="source-tag">{_escape(tag)}</span>' for tag in source_tags
        ) or '<span class="cell-secondary">No confirming source</span>'
        trusted = str(row.get("resolution_status") or "") in TRUSTED_COMPANY_STATUSES
        approval = "" if trusted else _status_pill("Needs approval", "warning")
        screenshot = _screenshot_path(row)
        if screenshot:
            screenshot_url = f'/screenshots/{int(row["person_id"])}'
            evidence_html = f"""
              <div class="final-evidence-card screenshot-evidence">
                <div class="evidence-title"><span>ServiceNow evidence</span><small>Screenshot captured during automation</small></div>
                <a href="{screenshot_url}" target="_blank" title="Open full-size screenshot">
                  <img src="{screenshot_url}" loading="lazy" alt="ServiceNow result for {_escape(row.get('company_name'))}">
                </a>
              </div>"""
        elif evidence.citations:
            citation_items: list[str] = []
            for citation in evidence.citations:
                label = f"{citation.citation_type}: {citation.title}"
                if citation.url:
                    citation_items.append(
                        f'<li><a href="{_escape(citation.url)}" target="_blank" '
                        f'rel="noreferrer">{_escape(label)}</a></li>'
                    )
                else:
                    citation_items.append(f'<li>{_escape(label)} <span class="muted">(URL unavailable)</span></li>')
            evidence_html = f"""
              <div class="final-evidence-card">
                <div class="evidence-title"><span>n8n research citations</span><small>Shown because no ServiceNow screenshot is available</small></div>
                <ul class="citation-list">{''.join(citation_items)}</ul>
              </div>"""
        else:
            evidence_html = """
              <div class="final-evidence-card evidence-empty">
                <div class="evidence-title"><span>No visual evidence or citation available</span><small>The record has not returned a screenshot or an n8n citation yet.</small></div>
              </div>"""

        verification_note = (
            _escape(evidence.evidence_note)
            or _escape(evidence.verification_status)
            or "No additional verification note was returned."
        )
        records.append(
            f"""
            <details class="final-record">
              <summary class="final-record-summary">
                <span class="final-cell record-person">{_table_person_link(row)}<span class="cell-secondary">{_escape(row.get('company_name')) or 'Company unresolved'}</span>{approval}</span>
                <span class="final-cell">{_status_pill(relationship, relationship_tone)}</span>
                <span class="final-cell">{_pretty_status(row.get('servicenow_customer'), 'Not checked')}</span>
                <span class="final-cell"><span class="source-tags">{sources}</span></span>
                <span class="final-cell">{_status_pill(evidence.delivery_status or 'Waiting', 'info' if evidence.delivery_status else 'neutral')}</span>
                <span class="record-expand"><span class="expand-text">Show record</span><span class="chevron" aria-hidden="true">⌄</span></span>
              </summary>
              <div class="final-record-details">
                <div class="final-record-facts">
                  <div><span>Matched result</span><strong>{_escape(row.get('servicenow_matched_name')) or 'No match recorded'}</strong></div>
                  <div><span>Checked</span><strong>{_escape(row.get('checked_at')) or 'Not checked yet'}</strong></div>
                  <div><span>Evidence strength</span><strong>{_escape(evidence.evidence_strength) or 'Not available'}</strong></div>
                  <div><span>Company resolution</span><strong>{_pretty_status(row.get('resolution_status'), 'Waiting')}</strong></div>
                </div>
                <div class="final-record-evidence">
                  {evidence_html}
                  <div class="final-evidence-card verification-card"><div class="evidence-title"><span>Verification note</span></div><p>{verification_note}</p></div>
                </div>
              </div>
            </details>"""
        )
    if not records:
        records.append('<div class="table-empty">Final results will appear after processing.</div>')
    return f"""
      <div class="final-records">
        <div class="final-record-header" aria-hidden="true"><span>Person &amp; company</span><span>Final status</span><span>ServiceNow app</span><span>Sources</span><span>n8n delivery</span><span></span></div>
        {''.join(records)}
      </div>"""


def _run_progress(run_id: int) -> dict[str, Any]:
    run = DATABASE.run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    rows = DATABASE.report_rows(run_id)
    total = len(rows)
    enriched, automated, approved = _workflow_counts(rows)
    target = approved or total
    processed = _enrichment_processed_count(rows)
    failed_enrichment = sum(
        str(row.get("check_status") or "").casefold() == "apollo_failed" for row in rows
    )
    automation_target = enriched
    enrichment_complete = approved > 0 and processed >= approved
    automation_complete = automation_target > 0 and automated >= automation_target
    busy = run["status"] in {"enriching", "collecting"}
    enrich_state = "active" if run["status"] == "enriching" or not enrichment_complete else "complete"
    automation_state = (
        "locked"
        if run["status"] == "enriching"
        else "active"
        if run["status"] == "collecting" or (enriched > 0 and not automation_complete)
        else "complete" if automation_complete else "locked"
    )
    confirmed = sum(
        _relationship_tone(
            _relationship(
                row,
                parse_n8n_evidence(
                    str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")
                ),
            )
        )
        == "positive"
        for row in rows
    )

    def percent(value: int, maximum: int) -> int:
        return min(100, round(100 * value / maximum)) if maximum else 0

    return {
        "run_id": run_id,
        "run_status": str(run["status"]),
        "run_status_label": _pretty_status(run["status"]),
        "busy": busy,
        "total": total,
        "approved": approved,
        "target": target,
        "processed": processed,
        "failed_enrichment": failed_enrichment,
        "enriched": enriched,
        "automated": automated,
        "automation_target": automation_target,
        "confirmed": confirmed,
        "enrichment_percent": percent(processed, target),
        "automation_percent": percent(automated, automation_target),
        "enrichment_complete": enrichment_complete,
        "automation_complete": automation_complete,
        "enrich_state": enrich_state,
        "automation_state": automation_state,
        "can_enrich": not busy,
        "can_automate": not busy and enriched > 0,
        "enrich_label": "Enriching records…" if run["status"] == "enriching" else "Enrich records",
        "automation_label": "Automation running…" if run["status"] == "collecting" else "Start web automation",
    }


def _page(request: Request, selected_run: int | None = None) -> str:
    login_snapshot = LOGIN_MONITOR.snapshot
    login_tone = login_snapshot.tone or ("logged-in" if login_snapshot.logged_in else "waiting")
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
    relationships = [
        _relationship(
            row,
            parse_n8n_evidence(
                str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")
            ),
        )
        for row in rows
    ]
    confirmed_count = sum(_relationship_tone(label) == "positive" for label in relationships)
    integration_count = sum(
        str(row.get("servicenow_customer") or "").casefold() == "yes" for row in rows
    )
    attention_count = sum(
        str(row.get("check_status") or "").casefold() in {"apollo_failed", "error", "manual_review"}
        or str(row.get("resolution_status") or "") not in TRUSTED_COMPANY_STATUSES
        for row in rows
    )
    report_stats = f"""
      <div class="report-stats">
        <div><strong>{len(rows)}</strong><span>Total reports</span></div>
        <div><strong>{confirmed_count}</strong><span>Customers / partners</span></div>
        <div><strong>{integration_count}</strong><span>Integration app matches</span></div>
        <div><strong>{attention_count}</strong><span>Need attention</span></div>
      </div>"""
    enriched_count, automation_count, approved_count = _workflow_counts(rows)
    enrichment_processed_count = _enrichment_processed_count(rows)
    failed_enrichment_count = sum(
        str(row.get("check_status") or "").casefold() == "apollo_failed" for row in rows
    )
    completed_count = sum(
        str(row.get("check_status") or "").casefold() == "completed" for row in rows
    )
    overview_stats = f"""
      <div class="overview-stats" aria-label="Run summary">
        <div class="overview-stat"><span class="stat-icon records" aria-hidden="true">{len(rows)}</span><div><strong>{len(rows)}</strong><span>People in run</span></div></div>
        <div class="overview-stat"><span class="stat-icon approved" aria-hidden="true">✓</span><div><strong>{approved_count}</strong><span>Companies approved</span></div></div>
        <div class="overview-stat"><span class="stat-icon checked" aria-hidden="true">↗</span><div><strong>{completed_count}</strong><span>Checks completed</span></div></div>
        <div class="overview-stat"><span class="stat-icon confirmed" aria-hidden="true">◆</span><div><strong>{confirmed_count}</strong><span>Relationships found</span></div></div>
      </div>"""

    workflow_steps = '<div class="empty-state"><strong>No active run</strong><span>Upload a CSV to start the workflow.</span></div>'
    run_log = ""
    if run:
        busy = run["status"] in {"enriching", "collecting"}
        enrichment_total = approved_count or len(rows)
        automation_total = enriched_count
        enrichment_complete = approved_count > 0 and enrichment_processed_count >= approved_count
        automation_complete = automation_total > 0 and automation_count >= automation_total
        final_complete = run["status"] == "completed"
        enrichment_percent = round(100 * enrichment_processed_count / enrichment_total) if enrichment_total else 0
        automation_percent = round(100 * automation_count / automation_total) if automation_total else 0
        step_one_state = "active" if run["status"] == "enriching" or not enrichment_complete else "complete"
        step_two_state = (
            "locked" if run["status"] == "enriching"
            else "active" if run["status"] == "collecting" or (enriched_count > 0 and not automation_complete)
            else "complete" if automation_complete else "locked"
        )
        step_three_state = "complete" if final_complete else "active" if automation_complete else "locked"
        enrich_disabled = "disabled" if busy else ""
        automation_disabled = "disabled" if busy or not enriched_count else ""
        final_disabled = "disabled" if busy or not automation_complete else ""
        enrich_label = "Enriching records…" if run["status"] == "enriching" else "Enrich records"
        automation_label = "Automation running…" if run["status"] == "collecting" else "Start web automation"
        download_action = (
            f'<a class="button primary-link" href="/reports.csv?run_id={selected_run}">Download CSV</a>'
            if automation_complete
            else '<span class="button disabled-link" aria-disabled="true">Download CSV</span>'
        )
        workflow_steps = f"""
          <div class="workflow-progress" id="workflow-progress" data-run-id="{selected_run}" data-busy="{str(busy).lower()}">
            <article class="workflow-step {step_one_state}" data-stage-card="enrich">
              <div class="step-top"><span class="step-number" data-step-number="enrich">{'✓' if enrichment_complete else '1'}</span><span class="step-state" data-approved-count>{approved_count}/{len(rows)} approved</span></div>
              <h3>Enrich records</h3>
              <p>Resolve each LinkedIn profile and enrich its company with Apollo.</p>
              <div class="step-metric"><strong data-enriched-count>{enrichment_processed_count}</strong><span>of <span data-enrichment-total>{enrichment_total}</span> processed<span data-enrichment-failures>{f' · {failed_enrichment_count} needs attention' if failed_enrichment_count else ''}</span></span></div>
              <div class="stage-progress" role="progressbar" aria-label="Enrichment progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{enrichment_percent}" data-progress="enrich"><span style="width:{enrichment_percent}%"></span></div>
              <form class="async-stage-form" data-stage="enrich" method="post" action="/runs/{selected_run}/enrich"><button class="primary step-action" {enrich_disabled}>{enrich_label}</button></form>
            </article>
            <article class="workflow-step {step_two_state}" data-stage-card="automation">
              <div class="step-top"><span class="step-number" data-step-number="automation">{'✓' if automation_complete else '2'}</span><span class="step-state" data-automation-state>{automation_count}/{automation_total} checked</span></div>
              <h3>Run web automation</h3>
              <p>Open ServiceNow, sign in, then start the browser checks using saved enrichment.</p>
              <div class="step-metric automation-metric"><strong data-automation-count>{automation_count}</strong><span>of <span data-automation-total>{automation_total}</span> ready records checked</span></div>
              <div class="stage-progress" role="progressbar" aria-label="Web automation progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{automation_percent}" data-progress="automation"><span style="width:{automation_percent}%"></span></div>
              <div class="step-actions">
                <form method="post" action="/runs/{selected_run}/launch-browser"><button {automation_disabled}>Open ServiceNow</button></form>
                <form class="async-stage-form" data-stage="automation" method="post" action="/runs/{selected_run}/collect"><button class="primary" {automation_disabled}>{automation_label}</button></form>
              </div>
            </article>
            <article class="workflow-step {step_three_state}">
              <div class="step-top"><span class="step-number">{'✓' if final_complete else '3'}</span><span class="step-state">{confirmed_count} confirmed</span></div>
              <h3>Review final results</h3>
              <p>Review verification evidence, retry deliveries, or export the completed table.</p>
              <div class="step-actions">
                <form method="post" action="/runs/{selected_run}/send-n8n"><button {final_disabled}>Retry n8n delivery</button></form>
                {download_action}
              </div>
            </article>
          </div>"""
        if run["collection_log"]:
            run_log = f'<details class="run-log"><summary>View latest run log</summary><pre>{_escape(run["collection_log"])}</pre></details>'

    default_tab = "enriched"
    enriched_active = default_tab == "enriched"
    automation_active = default_tab == "automation"
    final_active = default_tab == "final"
    final_export = (
        f'<a class="button" href="/reports.csv?run_id={selected_run}">Export CSV</a>'
        if selected_run
        else '<span class="button disabled-link" aria-disabled="true">Export CSV</span>'
    )
    record_tabs = f"""
      <div class="record-tabs" role="tablist" aria-label="Workflow records">
        <button type="button" class="tab-button {'active' if enriched_active else ''}" role="tab" aria-selected="{str(enriched_active).lower()}" aria-controls="panel-enriched" id="tab-enriched" data-tab="enriched">
          <span>Enriched records</span><strong data-tab-count="enriched">{enriched_count}/{len(rows)}</strong>
        </button>
        <button type="button" class="tab-button {'active' if automation_active else ''}" role="tab" aria-selected="{str(automation_active).lower()}" aria-controls="panel-automation" id="tab-automation" data-tab="automation">
          <span>Web automation</span><strong data-tab-count="automation">{automation_count}/{len(rows)}</strong>
        </button>
        <button type="button" class="tab-button {'active' if final_active else ''}" role="tab" aria-selected="{str(final_active).lower()}" aria-controls="panel-final" id="tab-final" data-tab="final">
          <span>Final table</span><strong data-tab-count="final">{confirmed_count}/{len(rows)}</strong>
        </button>
      </div>
      <div class="tab-panel" id="panel-enriched" role="tabpanel" aria-labelledby="tab-enriched" {'hidden' if not enriched_active else ''}>
        <div class="panel-heading"><div><h3>Enriched records</h3><p>Identity, Apollo company resolution, approval status, and organization data.</p></div></div>
        <div data-workspace-table="enriched">{_enrichment_table(rows)}</div>
      </div>
      <div class="tab-panel" id="panel-automation" role="tabpanel" aria-labelledby="tab-automation" {'hidden' if not automation_active else ''}>
        <div class="panel-heading"><div><h3>Web automation</h3><p>ServiceNow browser-check progress, matches, confidence, and screenshots.</p></div></div>
        <div data-workspace-table="automation">{_automation_table(rows)}</div>
      </div>
      <div class="tab-panel" id="panel-final" role="tabpanel" aria-labelledby="tab-final" {'hidden' if not final_active else ''}>
        <div class="panel-heading"><div><h3>Final results</h3><p>Combined ServiceNow result, verification sources, and n8n delivery status.</p></div>{final_export}</div>
        <div data-report-stats>{report_stats}</div>
        <div data-workspace-table="final">{_final_results_table(rows)}</div>
      </div>"""

    return f"""<!doctype html>
    <html><head><meta charset="utf-8"><title>ServiceNow Partner Workflow</title>
    <style>
      * {{ box-sizing:border-box; }}
      body {{ font-family:Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; max-width:1440px; margin:0 auto; color:#172033; padding:38px 20px 64px; background:#f5f7fb; }}
      h1 {{ margin:0 0 6px; letter-spacing:-.035em; font-size:32px; }} h2 {{ margin:0 0 8px; letter-spacing:-.02em; }}
      .muted, small {{ color:#68758a; }}
      section {{ background:#fff; border:1px solid #e3e8f0; border-radius:16px; padding:22px; margin:18px 0; box-shadow:0 8px 28px rgba(30,45,75,.045); }}
      form {{ display:inline-block; margin: 4px 8px 4px 0; }} input, select, button {{ padding:9px 12px; font:inherit; }}
      input, select {{ border:1px solid #d8deea; border-radius:8px; background:#fff; }}
      button, .button {{ border:1px solid #dfe5ee; border-radius:8px; background:#f2f5f9; color:#172033; text-decoration:none; cursor:pointer; display:inline-block; font-weight:650; }}
      button:hover, .button:hover {{ background:#e8edf5; }} button.primary {{ background:#1769e0; border-color:#1769e0; color:white; }} button:disabled {{ opacity:.5; cursor:not-allowed; }}
      .message {{ padding:12px 14px; border-radius:10px; }} .success {{ background:#e5f7eb; }} .error {{ background:#fde8e8; }}
      .badge {{ display:inline-block; background:#e8edf4; border-radius:999px; padding:3px 8px; margin:0 4px 5px 0; font-size:12px; }}
      .badge.source {{ background:#e7f0ff; color:#164e9b; }} .evidence-status {{ font-weight:700; margin:5px 0; }}
      .verification {{ margin:4px 0; }} .evidence-note {{ color:#4b586a; margin:5px 0; max-width:420px; }}
      .citations {{ margin-top:7px; }} .citations ul {{ margin:3px 0 0; padding-left:18px; }}
      .screenshot-link {{ display:inline-block; margin-top:6px; font-weight:600; }}
      .resolution-error {{ color:#a12622; }}
      .company-override {{ display:flex; gap:6px; margin-top:8px; }}
      .company-override input {{ min-width:180px; padding:6px 8px; }}
      .company-override button {{ padding:6px 8px; }}
      .company-input-group {{ display:flex; align-items:center; gap:6px; width:100%; }}
      .company-input-group input {{ flex:1; }}
      .ai-resolve-btn {{ display:inline-flex; align-items:center; gap:4px; padding:6px 9px; font-size:11px; font-weight:750; color:#4338ca; background:#eef2ff; border:1px solid #c7d2fe; border-radius:6px; cursor:pointer; white-space:nowrap; transition:all .15s ease; }}
      .ai-resolve-btn:hover:not(:disabled) {{ background:#e0e7ff; border-color:#a5b4fc; transform:translateY(-1px); }}
      .ai-resolve-btn:disabled {{ opacity:.6; cursor:not-allowed; }}
      .ai-resolve-btn.loading {{ background:#f1f5f9; color:#64748b; border-color:#cbd5e1; }}
      .ai-status-msg {{ font-size:11px; margin-top:4px; padding:3px 7px; border-radius:4px; max-width:280px; }}
      .ai-status-msg.success {{ background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; }}
      .ai-status-msg.error {{ background:#fef2f2; color:#991b1b; border:1px solid #fecaca; }}
      .ai-glow {{ border-color:#10b981 !important; box-shadow:0 0 0 3px rgba(16,185,129,.25) !important; transition:all .3s ease; }}
      pre {{ white-space:pre-wrap; max-height:300px; overflow:auto; background:#111827; color:#e5e7eb; padding:12px; border-radius:6px; }}
      .page-header {{ display:flex; align-items:flex-end; justify-content:space-between; gap:24px; margin-bottom:20px; }} .page-header p {{ margin:0; }}
      .upload-section {{ display:flex; align-items:center; justify-content:space-between; gap:24px; padding:18px 22px; }} .upload-copy h2 {{ font-size:17px; margin-bottom:4px; }} .upload-copy p {{ margin:0; max-width:760px; font-size:13px; }} .upload-section form {{ display:flex; align-items:center; margin:0; }}
      .section-heading {{ display:flex; align-items:center; justify-content:space-between; gap:18px; margin-bottom:20px; }} .section-heading p {{ margin:4px 0 0; }} .run-picker {{ display:flex; align-items:center; gap:9px; margin:0; }} .run-picker label {{ color:#68758a; font-size:12px; font-weight:700; }}
      .run-status {{ display:inline-flex; border-radius:999px; background:#edf3fb; color:#315b8e; padding:6px 10px; font-size:12px; font-weight:750; }}
      .session-status {{ display:inline-flex; align-items:center; gap:7px; border-radius:999px; padding:7px 11px; font-size:12px; font-weight:800; }}
      .session-status::before {{ content:""; width:8px; height:8px; border-radius:50%; background:currentColor; }}
      .session-status.waiting {{ background:#fff3d6; color:#91650a; }}
      .session-status.logged-in, .session-status.ready {{ background:#e1f7eb; color:#137345; }}
      .session-status.working {{ background:#e9f2ff; color:#245b9e; }}
      .session-status.running {{ background:#eef3ff; color:#2d4fd1; }}
      .session-status.failed {{ background:#fdebea; color:#a13b32; }}
      .workflow-progress {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
      .workflow-step {{ position:relative; display:flex; flex-direction:column; min-height:245px; padding:20px; border:1px solid #e1e7ef; border-top:4px solid #d5dce7; border-radius:13px; background:#fafbfc; }}
      .workflow-step.active {{ border-top-color:#1769e0; background:#fff; box-shadow:0 7px 20px rgba(31,74,135,.08); }} .workflow-step.complete {{ border-top-color:#20a266; background:#fbfffd; }} .workflow-step.locked {{ opacity:.7; }}
      .step-top {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }} .step-number {{ display:grid; place-items:center; width:29px; height:29px; border-radius:50%; background:#e9edf3; color:#536175; font-weight:800; font-size:13px; }}
      .workflow-step.active .step-number {{ background:#1769e0; color:#fff; }} .workflow-step.complete .step-number {{ background:#20a266; color:#fff; }} .step-state {{ color:#6d798c; font-size:12px; font-weight:700; }}
      .workflow-step h3 {{ margin:17px 0 5px; font-size:16px; }} .workflow-step p {{ min-height:42px; margin:0 0 16px; color:#68758a; font-size:13px; line-height:1.55; }} .step-metric {{ display:flex; align-items:baseline; gap:6px; margin:auto 0 12px; }} .step-metric strong {{ font-size:24px; }} .step-metric span {{ color:#68758a; font-size:12px; }}
      .step-actions {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-top:auto; }} .step-actions form, .workflow-step > form {{ margin:0; }} .step-actions button, .step-actions .button, .step-action {{ min-height:38px; }} .primary-link {{ background:#1769e0; border-color:#1769e0; color:#fff; padding:9px 12px; }} .primary-link:hover {{ background:#1157bd; }}
      .disabled-link {{ opacity:.5; cursor:not-allowed; padding:9px 12px; }}
      .run-log {{ margin-top:16px; border-top:1px solid #edf0f4; padding-top:13px; }} .run-log > summary {{ width:max-content; cursor:pointer; color:#526077; font-size:13px; font-weight:700; }} .run-log pre {{ margin-bottom:0; }}
      .records-workspace {{ padding:0; overflow:hidden; }} .records-heading {{ padding:22px 24px 0; }} .records-heading p {{ margin:4px 0 0; }}
      .record-tabs {{ display:flex; gap:6px; padding:18px 24px 0; border-bottom:1px solid #e4e9f0; overflow-x:auto; }} .tab-button {{ display:flex; align-items:center; gap:10px; padding:11px 14px 13px; border:0; border-bottom:3px solid transparent; border-radius:8px 8px 0 0; background:transparent; color:#68758a; white-space:nowrap; }} .tab-button:hover {{ background:#f5f7fa; }} .tab-button.active {{ border-bottom-color:#1769e0; background:#eef5ff; color:#1558b0; }} .tab-button strong {{ display:inline-flex; border-radius:999px; padding:2px 7px; background:#e8edf4; color:#536175; font-size:11px; }} .tab-button.active strong {{ background:#d8e9ff; color:#1558b0; }}
      .tab-panel {{ padding:0 24px 24px; }} .tab-panel[hidden] {{ display:none; }} .panel-heading {{ display:flex; align-items:center; justify-content:space-between; gap:20px; padding:22px 0 14px; }} .panel-heading h3 {{ margin:0 0 3px; font-size:17px; }} .panel-heading p {{ margin:0; color:#68758a; font-size:13px; }}
      .table-scroll {{ overflow:auto; border:1px solid #e2e7ef; border-radius:11px; }} .data-table {{ width:100%; border-collapse:collapse; background:#fff; font-size:13px; }} .data-table th {{ padding:11px 13px; background:#f7f9fb; border-bottom:1px solid #dfe5ed; color:#69758a; font-size:11px; letter-spacing:.035em; text-align:left; text-transform:uppercase; white-space:nowrap; }} .data-table td {{ min-width:120px; padding:13px; border-bottom:1px solid #edf0f4; vertical-align:top; line-height:1.45; }} .data-table tbody tr:last-child td {{ border-bottom:0; }} .data-table tbody tr:hover {{ background:#fbfcfe; }}
      .table-person {{ display:block; width:max-content; max-width:220px; color:#172033; font-weight:750; text-decoration:none; }} a.table-person:hover {{ color:#1769c2; text-decoration:underline; }} .cell-secondary {{ display:block; margin-top:3px; color:#758196; font-size:11px; font-weight:500; }} .cell-error {{ display:block; max-width:240px; margin-top:6px; color:#a13b32; font-size:11px; }}
      .status-pill {{ display:inline-flex; align-items:center; width:max-content; border-radius:999px; padding:4px 8px; background:#edf1f5; color:#59667a; font-size:11px; font-weight:750; white-space:nowrap; }} .status-pill.success, .status-pill.positive {{ background:#e7f7ee; color:#13744a; }} .status-pill.warning {{ background:#fff4dc; color:#8a5a0d; }} .status-pill.danger {{ background:#fdebea; color:#a13b32; }} .status-pill.info {{ background:#e9f2ff; color:#245b9e; }} .status-pill.pending {{ background:#edf1f5; color:#59667a; }}
      .table-review, .table-evidence {{ margin-top:7px; }} .table-review > summary, .table-evidence > summary {{ width:max-content; cursor:pointer; color:#1769c2; font-size:11px; font-weight:700; }} .table-review p {{ max-width:280px; margin:8px 0; color:#8a5a0d; font-size:11px; }} .table-review .company-override {{ flex-direction:column; align-items:flex-start; }} .table-review .company-input-group {{ width:auto; max-width:290px; }} .table-review .company-override input {{ width:200px; min-width:0; }} .table-empty {{ padding:36px !important; color:#758196; text-align:center; }} .final-table .source-tags {{ min-width:180px; }}
      .maintenance {{ margin:18px 2px 0; color:#68758a; font-size:12px; }} .maintenance > summary {{ cursor:pointer; width:max-content; font-weight:700; }} .maintenance-body {{ margin-top:10px; padding:14px; border:1px solid #e3e8f0; border-radius:10px; background:#fff; }}
      .reports-section {{ padding:0; overflow:hidden; }} .reports-heading {{ padding:24px 24px 18px; border-bottom:1px solid #e8ecf2; }}
      .reports-heading p {{ margin:0; }}
      .report-stats {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:1px; background:#e8ecf2; border-bottom:1px solid #e8ecf2; }}
      .report-stats div {{ background:#fafbfc; padding:18px 22px; display:flex; flex-direction:column; gap:2px; }}
      .report-stats strong {{ font-size:24px; letter-spacing:-.03em; }} .report-stats span {{ color:#68758a; font-size:13px; }}
      .report-list {{ padding:12px; display:flex; flex-direction:column; gap:8px; }}
      .report-card {{ border:1px solid #e2e7ef; border-radius:12px; overflow:hidden; background:#fff; transition:border-color .15s, box-shadow .15s; }}
      .report-card:hover {{ border-color:#cbd5e3; box-shadow:0 5px 16px rgba(30,45,75,.055); }}
      .report-card[open] {{ border-color:#b9c9e2; box-shadow:0 8px 24px rgba(30,45,75,.075); }}
      .report-card summary {{ list-style:none; cursor:pointer; display:grid; grid-template-columns:minmax(190px,1.1fr) minmax(145px,.6fr) minmax(240px,1fr) auto; align-items:center; gap:18px; padding:18px 20px; }}
      .report-card summary::-webkit-details-marker {{ display:none; }}
      .summary-main {{ min-width:0; display:flex; flex-direction:column; align-items:flex-start; gap:3px; }} .person-name {{ color:#172033; font-weight:750; font-size:15px; text-decoration:none; }} a.person-name:hover {{ color:#1769c2; text-decoration:underline; }}
      .company-name {{ color:#68758a; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
      .company-approval-status {{ display:inline-flex; align-items:center; width:max-content; margin-top:3px; border:1px solid #f0cf88; background:#fff4dc; color:#8a5a0d; border-radius:999px; padding:4px 8px; font-size:11px; line-height:1; font-weight:750; }}
      .relationship {{ width:max-content; display:inline-flex; align-items:center; gap:7px; border-radius:999px; padding:6px 10px; font-size:13px; font-weight:700; background:#f1f4f8; color:#526077; }}
      .status-dot {{ width:7px; height:7px; border-radius:50%; background:currentColor; }} .relationship.positive {{ background:#e8f7ef; color:#13744a; }} .relationship.warning {{ background:#fff4dc; color:#966317; }} .relationship.pending {{ background:#edf1f6; color:#65738a; }}
      .source-tags {{ display:flex; gap:6px; flex-wrap:wrap; }} .source-tag {{ display:inline-flex; align-items:center; width:max-content; border:1px solid #cee0fb; background:#edf5ff; color:#245b9e; border-radius:999px; padding:5px 9px; font-size:12px; font-weight:650; }}
      .source-tag.source-empty {{ border-color:#e1e5eb; background:#f6f7f9; color:#778297; font-weight:500; }}
      .expand-label {{ display:flex; align-items:center; gap:7px; color:#526077; font-size:12px; font-weight:700; white-space:nowrap; }} .show-less {{ display:none; }} .chevron {{ font-size:18px; transition:transform .15s; }}
      .report-card[open] .show-more {{ display:none; }} .report-card[open] .show-less {{ display:inline; }} .report-card[open] .chevron {{ transform:rotate(180deg); }}
      .report-details {{ border-top:1px solid #e8ecf2; padding:22px 20px 24px; background:#fbfcfe; }}
      .detail-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }} .detail-group, .evidence-block {{ border:1px solid #e3e8f0; border-radius:10px; background:#fff; padding:18px; min-width:0; }}
      .detail-group h3, .evidence-block h3, .evidence-block h4 {{ margin:0 0 14px; font-size:14px; }}
      dl {{ margin:0; }} dl > div {{ display:grid; grid-template-columns:145px 1fr; gap:14px; padding:9px 0; border-bottom:1px solid #eef1f5; }} dl > div:last-child {{ border-bottom:0; }}
      dt {{ color:#748096; font-size:12px; font-weight:650; }} dd {{ margin:0; font-size:13px; overflow-wrap:anywhere; }}
      .evidence-section {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; margin-top:18px; }} .evidence-block p {{ margin:7px 0; font-size:13px; line-height:1.5; }}
      .citation-list {{ margin:0; padding-left:18px; font-size:13px; }} .citation-list li {{ margin:8px 0; }} a {{ color:#1769c2; }}
      .screenshot-block img {{ display:block; width:100%; max-height:170px; object-fit:cover; object-position:top; border:1px solid #e1e6ed; border-radius:7px; }}
      .detail-alert {{ color:#a13b32; background:#fff0ee; border-radius:7px; padding:8px 10px; font-size:12px; }}
      .empty-state {{ padding:52px 20px; text-align:center; color:#68758a; display:flex; flex-direction:column; gap:5px; }}
      @media (max-width:950px) {{ .workflow-progress {{ grid-template-columns:1fr; }} .workflow-step {{ min-height:0; }} .workflow-step p {{ min-height:0; }} .step-metric {{ margin-top:4px; }} }}
      @media (max-width:850px) {{ .page-header, .upload-section, .section-heading {{ align-items:flex-start; flex-direction:column; }} .report-stats {{ grid-template-columns:1fr 1fr; }} .report-card summary {{ grid-template-columns:1fr auto; }} .source-tags {{ grid-column:1 / -1; }} .relationship {{ justify-self:end; }} .expand-label {{ grid-column:2; grid-row:2; }} .detail-grid, .evidence-section {{ grid-template-columns:1fr; }} }}
      @media (max-width:520px) {{ body {{ padding:24px 10px 40px; }} section {{ border-radius:12px; padding:16px; }} .upload-section form {{ align-items:stretch; flex-direction:column; width:100%; }} .upload-section input, .upload-section button {{ width:100%; }} .records-workspace {{ padding:0; }} .records-heading {{ padding:18px 16px 0; }} .record-tabs {{ padding-left:16px; padding-right:16px; }} .tab-panel {{ padding:0 16px 16px; }} .report-card summary {{ padding:15px; gap:12px; }} .show-more, .show-less {{ display:none !important; }} dl > div {{ grid-template-columns:1fr; gap:3px; }} }}

      /* Refined dashboard shell */
      :root {{
        color-scheme:light;
        --ink:#14213d;
        --muted:#64748b;
        --line:#e3e9f2;
        --brand:#2563eb;
        --brand-dark:#1d4ed8;
        --navy:#0b1739;
        --canvas:#f4f7fb;
        --success:#15946b;
      }}
      body {{ max-width:1520px; padding:28px 28px 60px; color:var(--ink); background:#f8f9fd; }}
      body::before {{ display:none; }}
      h1, h2, h3 {{ font-family:Manrope, Inter, ui-sans-serif, system-ui, sans-serif; }}
      h1, h2, h3, p {{ text-wrap:pretty; }}
      section {{ border-color:#e2e8f0; border-radius:14px; box-shadow:0 8px 24px rgba(30,41,59,.045); }}
      .page-header {{ position:relative; overflow:hidden; align-items:center; min-height:116px; padding:24px 26px; margin-bottom:16px; border:1px solid #e2e8f0; border-radius:14px; color:var(--ink); background:#fff; box-shadow:0 8px 24px rgba(30,41,59,.05); }}
      .page-header::before {{ content:""; position:absolute; inset:0 auto 0 0; width:4px; background:linear-gradient(#2563eb,#0f766e); }}
      .brand-lockup {{ position:relative; z-index:1; display:flex; align-items:center; gap:20px; }}
      .brand-mark {{ display:grid; place-items:center; flex:0 0 auto; width:48px; height:48px; border:1px solid #1d4ed8; border-radius:12px; color:#fff; background:#2563eb; box-shadow:0 7px 16px rgba(37,99,235,.18); font-size:14px; font-weight:850; letter-spacing:-.04em; }}
      .eyebrow {{ display:block; margin-bottom:5px; color:#2563eb; font-size:10px; font-weight:800; letter-spacing:.14em; text-transform:uppercase; }}
      .page-header h1 {{ margin:0 0 5px; color:#0f172a; font-size:clamp(25px,2.4vw,34px); line-height:1.1; }}
      .page-header p {{ max-width:720px; color:#64748b; font-size:13px; line-height:1.5; }}
      .header-meta {{ position:relative; z-index:1; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
      .live-indicator {{ display:inline-flex; align-items:center; gap:8px; padding:8px 11px; border:1px solid #cce9dd; border-radius:999px; background:#effaf5; color:#12694f; font-size:11px; font-weight:750; }}
      .live-indicator i {{ width:7px; height:7px; border-radius:50%; background:#16a374; box-shadow:0 0 0 4px rgba(22,163,116,.12); }}
      .overview-stats {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:18px 0; }}
      .overview-stat {{ display:flex; align-items:center; gap:13px; min-width:0; padding:16px 17px; border:1px solid #e2e8f0; border-radius:12px; background:#fff; box-shadow:0 5px 16px rgba(30,41,59,.035); }}
      .overview-stat > div {{ display:flex; min-width:0; flex-direction:column; }}
      .overview-stat strong {{ font-size:23px; line-height:1.1; letter-spacing:-.035em; }}
      .overview-stat > div span {{ margin-top:3px; color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
      .stat-icon {{ display:grid; place-items:center; flex:0 0 auto; width:38px; height:38px; border-radius:12px; background:#edf4ff; color:#2563eb; font-size:12px; font-weight:850; }}
      .stat-icon.approved {{ background:#eaf9f3; color:#14845f; font-size:17px; }} .stat-icon.checked {{ background:#f2edff; color:#7652d6; font-size:18px; }} .stat-icon.confirmed {{ background:#fff3e5; color:#d17312; font-size:13px; }}
      .upload-section {{ position:relative; overflow:hidden; align-items:center; padding:20px 24px; border-color:#cbdcf7; background:#f5f9ff; }}
      .upload-section::after {{ display:none; }}
      .upload-copy {{ position:relative; z-index:1; display:flex; align-items:center; gap:14px; }}
      .upload-icon {{ display:grid; place-items:center; flex:0 0 auto; width:44px; height:44px; border-radius:13px; background:#e9f2ff; color:#2563eb; }}
      .upload-icon svg {{ width:21px; height:21px; }}
      .upload-copy h2 {{ color:var(--ink); font-size:16px; }}
      .upload-section form {{ position:relative; z-index:1; gap:10px; }}
      input[type="file"] {{ max-width:300px; padding:5px; color:#637087; background:#fff; }}
      input[type="file"]::file-selector-button {{ margin-right:10px; padding:8px 11px; border:0; border-radius:7px; background:#edf2f8; color:#34435c; font:inherit; font-size:12px; font-weight:750; cursor:pointer; }}
      input:focus-visible, select:focus-visible, button:focus-visible, a:focus-visible, summary:focus-visible {{ outline:3px solid rgba(37,99,235,.22); outline-offset:2px; }}
      button, .button {{ min-height:38px; border-color:#d9e1ec; border-radius:9px; background:#f8fafc; transition:background .15s, border-color .15s, transform .15s, box-shadow .15s; }}
      button:hover, .button:hover {{ border-color:#c9d4e2; background:#f0f4f8; transform:translateY(-1px); }}
      button.primary, .primary-link {{ border-color:var(--brand); background:var(--brand); box-shadow:0 6px 14px rgba(37,99,235,.18); }}
      button.primary:hover, .primary-link:hover {{ border-color:var(--brand-dark); background:var(--brand-dark); box-shadow:0 8px 18px rgba(37,99,235,.22); }}
      .message {{ margin:18px 0; border:1px solid transparent; font-size:13px; font-weight:650; }} .message.success {{ border-color:#bcebd8; color:#11694e; background:#eafaf4; }} .message.error {{ border-color:#fac8c4; color:#9b302b; background:#fff0ef; }}
      .workflow-section {{ padding:24px; }}
      .section-heading {{ align-items:flex-end; }} .section-heading h2, .records-heading h2 {{ font-size:20px; }}
      .section-kicker {{ display:block; margin-bottom:5px; color:#2563eb; font-size:10px; font-weight:850; letter-spacing:.13em; text-transform:uppercase; }}
      .run-picker {{ flex-wrap:wrap; justify-content:flex-end; padding:6px 7px 6px 11px; border:1px solid #e0e6ef; border-radius:12px; background:#f8fafc; }}
      .run-picker select {{ max-width:310px; border:0; background:transparent; font-size:12px; font-weight:700; }}
      .run-status {{ background:#e5f5ff; color:#17658d; }}
      .workflow-progress {{ gap:18px; }}
      .workflow-step {{ min-height:255px; padding:21px; border-color:#e1e7f0; border-top-width:1px; border-radius:12px; background:#f8fafc; }}
      .workflow-step::before {{ content:""; position:absolute; inset:0; border-radius:inherit; pointer-events:none; box-shadow:inset 0 3px 0 #d3dce8; }}
      .workflow-step.active {{ border-color:#bfd4f7; background:#fff; box-shadow:0 10px 24px rgba(37,99,235,.08); }} .workflow-step.active::before {{ box-shadow:inset 0 3px 0 #2563eb; }}
      .workflow-step.complete {{ border-color:#cceadd; background:#fff; }} .workflow-step.complete::before {{ box-shadow:inset 0 3px 0 #1aa173; }}
      .workflow-step.locked {{ opacity:.62; }}
      .workflow-step:not(:last-child)::after {{ content:"→"; position:absolute; z-index:2; right:-27px; top:48%; display:grid; place-items:center; width:34px; height:34px; border:5px solid #fff; border-radius:50%; color:#8b99ad; background:#edf1f6; font-size:15px; font-weight:800; }}
      .step-number {{ width:32px; height:32px; border:1px solid #dfe5ed; background:#fff; }}
      .step-state {{ padding:5px 8px; border-radius:999px; background:#edf1f6; color:#66758a; font-size:10px; letter-spacing:.02em; }}
      .workflow-step.active .step-state {{ background:#e8f1ff; color:#245ca9; }} .workflow-step.complete .step-state {{ background:#e6f7ef; color:#157051; }}
      .workflow-step h3 {{ margin-top:20px; font-size:17px; }} .workflow-step p {{ color:#65738a; }}
      .stage-progress {{ position:relative; width:100%; height:7px; margin:0 0 15px; overflow:hidden; border-radius:999px; background:#e6ebf2; }}
      .stage-progress > span {{ display:block; width:0; height:100%; border-radius:inherit; background:#2563eb; transition:width .35s ease; }}
      .workflow-step.complete .stage-progress > span {{ background:#15946b; }}
      .automation-metric {{ margin-top:auto; }}
      .records-workspace {{ border-radius:14px; }}
      .records-heading {{ padding:24px 24px 2px; }}
      .record-tabs {{ gap:8px; margin:14px 24px 0; padding:5px; border:1px solid #e1e7ef; border-radius:12px; background:#f4f7fa; }}
      .tab-button {{ flex:0 0 auto; justify-content:center; min-height:40px; padding:9px 13px; border:0; border-radius:8px; }} .tab-button.active {{ border:0; background:#fff; color:#1f5eb8; box-shadow:0 3px 10px rgba(34,51,80,.09); }}
      .tab-panel {{ padding-top:2px; }}
      .table-scroll {{ border-radius:13px; }} .data-table th {{ position:sticky; top:0; z-index:1; padding:12px 14px; background:#f6f8fb; }} .data-table td {{ padding:14px; }}
      .data-table tbody tr {{ transition:background .12s; }} .data-table tbody tr:hover {{ background:#f7faff; }}
      .final-records {{ overflow:hidden; border:1px solid #e2e7ef; border-radius:13px; background:#fff; }}
      .final-record-header, .final-record-summary {{ display:grid; grid-template-columns:minmax(210px,1.25fr) minmax(130px,.7fr) minmax(125px,.65fr) minmax(190px,1fr) minmax(120px,.65fr) 105px; align-items:center; gap:14px; }}
      .final-record-header {{ padding:12px 14px; border-bottom:1px solid #dfe5ed; color:#69758a; background:#f6f8fb; font-size:11px; font-weight:750; letter-spacing:.035em; text-transform:uppercase; }}
      .final-record {{ border-bottom:1px solid #edf0f4; background:#fff; }} .final-record:last-child {{ border-bottom:0; }}
      .final-record-summary {{ min-height:72px; padding:13px 14px; list-style:none; cursor:pointer; transition:background .15s; }} .final-record-summary::-webkit-details-marker {{ display:none; }} .final-record-summary:hover {{ background:#f7faff; }}
      .final-record[open] > .final-record-summary {{ background:#f4f8ff; }}
      .final-cell {{ min-width:0; font-size:13px; }} .record-person {{ display:block; }}
      .record-expand {{ display:flex; align-items:center; justify-content:flex-end; gap:7px; color:#2563eb; font-size:11px; font-weight:750; white-space:nowrap; }}
      .record-expand .chevron {{ font-size:17px; transition:transform .15s; }} .final-record[open] .record-expand .chevron {{ transform:rotate(180deg); }}
      .final-record[open] .expand-text {{ font-size:0; }} .final-record[open] .expand-text::after {{ content:"Hide record"; font-size:11px; }}
      .final-record-details {{ padding:20px; border-top:1px solid #e0e8f3; background:#f8fafc; }}
      .final-record-facts {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:1px; overflow:hidden; margin-bottom:14px; border:1px solid #e2e8f0; border-radius:10px; background:#e2e8f0; }}
      .final-record-facts > div {{ display:flex; min-width:0; flex-direction:column; gap:5px; padding:13px 14px; background:#fff; }}
      .final-record-facts span {{ color:#718096; font-size:10px; font-weight:750; letter-spacing:.05em; text-transform:uppercase; }} .final-record-facts strong {{ overflow-wrap:anywhere; font-size:12px; }}
      .final-record-evidence {{ display:grid; grid-template-columns:minmax(0,1.35fr) minmax(260px,.65fr); gap:14px; }}
      .final-evidence-card {{ min-width:0; padding:16px; border:1px solid #e2e8f0; border-radius:10px; background:#fff; }}
      .evidence-title {{ display:flex; flex-direction:column; gap:3px; margin-bottom:12px; }} .evidence-title > span {{ font-size:13px; font-weight:800; }} .evidence-title small {{ font-size:11px; }}
      .screenshot-evidence img {{ display:block; width:100%; max-height:280px; object-fit:cover; object-position:top; border:1px solid #dce3ec; border-radius:8px; }}
      .verification-card p {{ margin:0; color:#56657a; font-size:12px; line-height:1.55; }} .evidence-empty {{ display:flex; align-items:center; }} .evidence-empty .evidence-title {{ margin:0; }}
      .status-pill, .source-tag, .relationship {{ border:1px solid transparent; }}
      .status-pill.success, .status-pill.positive, .relationship.positive {{ border-color:#c8ebdc; }} .status-pill.warning, .relationship.warning {{ border-color:#f3ddb1; }}
      .maintenance {{ padding:4px 8px; }}
      .maintenance > summary {{ color:#78859a; }}
      @media (prefers-reduced-motion:reduce) {{ *, *::before, *::after {{ scroll-behavior:auto !important; transition:none !important; animation:none !important; }} button:hover, .button:hover {{ transform:none; }} }}
      @media (max-width:950px) {{ .workflow-step:not(:last-child)::after {{ content:"↓"; right:auto; left:50%; top:auto; bottom:-27px; transform:translateX(-50%); }} }}
      @media (max-width:1050px) {{ .final-record-header {{ display:none; }} .final-record-summary {{ grid-template-columns:minmax(210px,1fr) repeat(2,minmax(120px,.6fr)) 90px; }} .final-record-summary .final-cell:nth-child(4), .final-record-summary .final-cell:nth-child(5) {{ display:none; }} .final-record-facts {{ grid-template-columns:1fr 1fr; }} }}
      @media (max-width:850px) {{ body {{ padding:18px 16px 48px; }} .page-header {{ min-height:auto; padding:25px; }} .header-meta {{ margin-left:76px; }} .overview-stats {{ grid-template-columns:1fr 1fr; }} .upload-section::after {{ display:none; }} .run-picker {{ align-items:stretch; width:100%; justify-content:flex-start; }} .run-picker select {{ flex:1; max-width:none; }} .final-record-evidence {{ grid-template-columns:1fr; }} }}
      @media (max-width:520px) {{ body {{ padding:10px 10px 38px; }} .page-header {{ padding:22px 18px; border-radius:14px; }} .brand-lockup {{ align-items:flex-start; gap:12px; }} .brand-mark {{ width:44px; height:44px; border-radius:12px; font-size:13px; }} .page-header h1 {{ font-size:25px; }} .header-meta {{ margin:16px 0 0 56px; }} .overview-stats {{ grid-template-columns:1fr; gap:8px; }} .overview-stat {{ padding:13px 15px; }} .upload-section {{ padding:17px; }} .upload-copy {{ align-items:flex-start; }} .upload-section form {{ gap:8px; margin-top:14px; }} input[type="file"] {{ max-width:none; }} .workflow-section {{ padding:18px 15px; }} .run-picker {{ padding:8px; }} .run-picker label {{ width:100%; }} .run-picker select {{ width:100%; }} .record-tabs {{ margin-left:16px; margin-right:16px; }} .tab-button {{ min-width:max-content; }} .final-record-summary {{ grid-template-columns:1fr auto; gap:10px; }} .final-record-summary .final-cell:nth-child(2), .final-record-summary .final-cell:nth-child(3) {{ display:none; }} .record-expand {{ grid-column:2; grid-row:1; }} .final-record-details {{ padding:13px; }} .final-record-facts {{ grid-template-columns:1fr; }} }}
    </style></head><body>
      <header class="page-header">
        <div class="brand-lockup"><div class="brand-mark" aria-hidden="true">SN</div><div><span class="eyebrow">Partner intelligence</span><h1>Customer verification workspace</h1><p>Turn LinkedIn profiles into verified ServiceNow relationships with one guided, evidence-backed workflow.</p></div></div>
        <div class="header-meta"><span class="live-indicator"><i aria-hidden="true"></i>Workflow dashboard</span><span id="login-status" class="session-status {login_tone}" title="{_escape(login_snapshot.detail)}">{_escape(login_snapshot.status)}</span></div>
      </header>
      {_message(request)}
      {overview_stats}
      <section class="upload-section"><div class="upload-copy"><span class="upload-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M5 14v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span><div><span class="section-kicker">Start a new run</span><h2>Upload people CSV</h2>
        <p class="muted">Include each person’s name and LinkedIn URL. Apollo data is used for automatic company resolution.</p></div></div>
        <form method="post" action="/runs" enctype="multipart/form-data"><input type="file" name="file" accept=".csv,text/csv" aria-label="Choose people CSV" required><button class="primary">Upload CSV</button></form>
      </section>
      <section class="workflow-section">
        <div class="section-heading"><div><span class="section-kicker">3-stage pipeline</span><h2>Workflow progress</h2><p class="muted">Complete each stage from left to right.</p></div>
          <form class="run-picker" method="get" action="/"><label for="run-select">Current run</label><select id="run-select" name="run_id" onchange="this.form.submit()">{options}</select>{f'<span class="run-status">{_pretty_status(run["status"])}</span>' if run else ''}</form>
        </div>
        {workflow_steps}
        {run_log}
      </section>
      <section class="records-workspace">
        <div class="records-heading"><span class="section-kicker">Run data</span><h2>Records</h2><p class="muted">Switch views to inspect the data produced at each workflow stage.</p></div>
        {record_tabs}
      </section>
      <details class="maintenance"><summary>Database maintenance</summary><div class="maintenance-body"><p>Clears local workflow runs and reports only. CSV files, configuration, Chrome profile, and source code remain unchanged.</p><form method="post" action="/database/clear" onsubmit="return confirm('Delete every local workflow run and report? This cannot be undone.');"><button>Clear database</button></form></div></details>
      <script>
        const tabButtons = Array.from(document.querySelectorAll('.tab-button'));
        const tabPanels = Array.from(document.querySelectorAll('.tab-panel'));
        function activateTab(name, updateHash = true) {{
          if (!tabButtons.some(button => button.dataset.tab === name)) return;
          tabButtons.forEach(button => {{
            const active = button.dataset.tab === name;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', String(active));
          }});
          tabPanels.forEach(panel => {{ panel.hidden = panel.id !== `panel-${{name}}`; }});
          if (updateHash) history.replaceState(null, '', `#${{name}}`);
        }}
        tabButtons.forEach(button => button.addEventListener('click', () => activateTab(button.dataset.tab)));
        const requestedTab = location.hash.slice(1);
        if (requestedTab) activateTab(requestedTab, false);

        const workflowProgress = document.getElementById('workflow-progress');
        const loginStatus = document.getElementById('login-status');
        let progressTimer = null;
        let workflowWasBusy = workflowProgress?.dataset.busy === 'true';

        function setText(selector, value) {{
          const element = document.querySelector(selector);
          if (element) element.textContent = value;
        }}

        function applyProgress(data) {{
          const enrichCard = document.querySelector('[data-stage-card="enrich"]');
          const automationCard = document.querySelector('[data-stage-card="automation"]');
          [[enrichCard, data.enrich_state], [automationCard, data.automation_state]].forEach(([card, state]) => {{
            if (!card) return;
            card.classList.remove('active', 'complete', 'locked');
            card.classList.add(state);
          }});
          setText('[data-step-number="enrich"]', data.enrichment_complete ? '✓' : '1');
          setText('[data-step-number="automation"]', data.automation_complete ? '✓' : '2');
          setText('[data-approved-count]', `${{data.approved}}/${{data.total}} approved`);
          setText('[data-enriched-count]', data.processed);
          setText('[data-enrichment-total]', data.target);
          setText('[data-enrichment-failures]', data.failed_enrichment ? ` · ${{data.failed_enrichment}} needs attention` : '');
          setText('[data-automation-count]', data.automated);
          setText('[data-automation-total]', data.automation_target);
          setText('[data-automation-state]', `${{data.automated}}/${{data.automation_target}} checked`);
          setText('[data-tab-count="enriched"]', `${{data.enriched}}/${{data.total}}`);
          setText('[data-tab-count="automation"]', `${{data.automated}}/${{data.total}}`);
          setText('[data-tab-count="final"]', `${{data.confirmed}}/${{data.total}}`);
          setText('.run-status', data.run_status_label);

          [['enrich', data.enrichment_percent], ['automation', data.automation_percent]].forEach(([stage, percent]) => {{
            const progress = document.querySelector(`[data-progress="${{stage}}"]`);
            if (!progress) return;
            progress.setAttribute('aria-valuenow', String(percent));
            const fill = progress.querySelector('span');
            if (fill) fill.style.width = `${{percent}}%`;
          }});

          const enrichButton = document.querySelector('.async-stage-form[data-stage="enrich"] button');
          const automationButton = document.querySelector('.async-stage-form[data-stage="automation"] button');
          const openBrowserButton = document.querySelector('[data-stage-card="automation"] .step-actions form:not(.async-stage-form) button');
          if (enrichButton) {{ enrichButton.disabled = !data.can_enrich; enrichButton.textContent = data.enrich_label; }}
          if (automationButton) {{ automationButton.disabled = !data.can_automate; automationButton.textContent = data.automation_label; }}
          if (openBrowserButton) openBrowserButton.disabled = !data.can_automate;
        }}

        async function refreshWorkspace(runId) {{
          const response = await fetch(`/api/runs/${{runId}}/workspace`, {{cache:'no-store'}});
          if (!response.ok) return;
          const workspace = await response.json();
          Object.entries(workspace).forEach(([name, markup]) => {{
            const target = document.querySelector(`[data-workspace-table="${{name}}"]`);
            if (target) target.innerHTML = markup;
          }});
        }}

        async function pollProgress() {{
          if (!workflowProgress) return;
          progressTimer = null;
          const runId = workflowProgress.dataset.runId;
          try {{
            const response = await fetch(`/api/runs/${{runId}}/progress`, {{cache:'no-store'}});
            if (!response.ok) throw new Error('Progress request failed');
            const data = await response.json();
            const finished = workflowWasBusy && !data.busy;
            applyProgress(data);
            workflowWasBusy = data.busy;
            if (finished) await refreshWorkspace(runId);
            if (data.busy) progressTimer = window.setTimeout(pollProgress, 1000);
          }} catch (_error) {{
            if (workflowWasBusy) progressTimer = window.setTimeout(pollProgress, 2000);
          }}
        }}

        document.querySelectorAll('.async-stage-form').forEach(form => {{
          form.addEventListener('submit', event => {{
            event.preventDefault();
            const button = form.querySelector('button');
            if (!button || button.disabled) return;
            button.disabled = true;
            button.textContent = form.dataset.stage === 'enrich' ? 'Starting enrichment…' : 'Starting automation…';
            workflowWasBusy = true;
            if (progressTimer) window.clearTimeout(progressTimer);
            const startRequest = fetch(form.action, {{method:'POST', redirect:'follow'}});
            progressTimer = window.setTimeout(pollProgress, 250);
            startRequest.then(() => {{
              if (!workflowWasBusy) {{
                workflowWasBusy = true;
                pollProgress();
              }}
            }}).catch(() => {{
              workflowWasBusy = false;
              if (progressTimer) window.clearTimeout(progressTimer);
              button.disabled = false;
              button.textContent = form.dataset.stage === 'enrich' ? 'Enrich records' : 'Start web automation';
            }});
          }});
        }});
        if (workflowProgress) pollProgress();

        async function pollLoginStatus() {{
          try {{
            const response = await fetch('/api/session-status', {{cache:'no-store'}});
            if (!response.ok) throw new Error('Session status request failed');
            const data = await response.json();
            if (loginStatus) {{
              loginStatus.textContent = data.status;
              loginStatus.title = data.detail || data.status;
              ['waiting', 'logged-in', 'ready', 'working', 'running', 'failed'].forEach(name => {{
                loginStatus.classList.remove(name);
              }});
              loginStatus.classList.add(data.tone || (data.logged_in ? 'logged-in' : 'waiting'));
            }}
          }} catch (_error) {{
            if (loginStatus) {{
              loginStatus.textContent = 'Waiting for Login';
              ['logged-in', 'ready', 'working', 'running', 'failed'].forEach(name => {{
                loginStatus.classList.remove(name);
              }});
              loginStatus.classList.add('waiting');
            }}
          }} finally {{
            window.setTimeout(pollLoginStatus, 2000);
          }}
        }}
        document.addEventListener('click', async (event) => {{
          const button = event.target.closest('.ai-resolve-btn');
          if (!button || button.disabled) return;

          const personId = button.dataset.personId;
          const runId = button.dataset.runId;
          const form = button.closest('form');
          const input = form ? form.querySelector('input[name="company_name"]') : null;
          const statusEl = form ? form.querySelector('.ai-status-msg') : null;

          const originalContent = button.innerHTML;
          button.disabled = true;
          button.classList.add('loading');
          button.innerHTML = '<span>✨ Searching…</span>';
          if (statusEl) {{
            statusEl.style.display = 'block';
            statusEl.className = 'ai-status-msg';
            statusEl.textContent = 'Searching web via LinkedIn profile…';
          }}

          try {{
            const formData = new URLSearchParams();
            if (runId) formData.append('run_id', runId);
            formData.append('auto_approve', 'true');

            const response = await fetch(`/api/people/${{personId}}/ai-resolve-company`, {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
              body: formData.toString(),
            }});

            const data = await response.json();
            if (!response.ok || !data.success) {{
              throw new Error(data.error || 'Could not resolve company.');
            }}

            if (input) {{
              input.value = data.company_name;
              input.classList.add('ai-glow');
            }}

            if (statusEl) {{
              statusEl.className = 'ai-status-msg success';
              statusEl.textContent = `✨ Found: ${{data.company_name}} (Approved)`;
            }}

            button.innerHTML = '<span>✓ Approved</span>';

            window.setTimeout(async () => {{
              if (runId && typeof refreshWorkspace === 'function') {{
                await refreshWorkspace(runId);
                if (typeof pollProgress === 'function') pollProgress();
              }} else if (form) {{
                form.submit();
              }} else {{
                location.reload();
              }}
            }}, 650);

          }} catch (err) {{
            button.disabled = false;
            button.classList.remove('loading');
            button.innerHTML = originalContent;
            if (statusEl) {{
              statusEl.className = 'ai-status-msg error';
              statusEl.textContent = err.message || 'AI web search failed. Please enter manually.';
            }}
          }}
        }});

        pollLoginStatus();
      </script>
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
        error="",
    )
    DATABASE.reset_check_for_company_change(person_id, run_id, company)
    DATABASE.update_run(run_id, status="needs_enrichment")
    return RedirectResponse(
        url=f"/?run_id={run_id}&message=Company+override+saved",
        status_code=303,
    )


@app.post("/api/people/{person_id}/ai-resolve-company")
def ai_resolve_company(
    person_id: int,
    run_id: int | None = Form(None),
    auto_approve: bool = Form(True),
) -> JSONResponse:
    row = DATABASE.person(person_id)
    if not row:
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": "Person record not found."},
        )

    person_name = str(row.get("person_name") or "")
    linkedin_url = str(row.get("linkedin_url") or "")
    headline = str(row.get("headline") or "")
    actual_run_id = int(run_id or row.get("run_id") or 0)

    result = resolve_company_from_web(
        person_name=person_name,
        linkedin_url=linkedin_url,
        headline=headline,
    )

    if not result.get("success") or not result.get("company_name"):
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": result.get("error") or "Could not determine company name via web search.",
            },
        )

    company = " ".join(str(result["company_name"]).split())

    if auto_approve and actual_run_id:
        DATABASE.update_person_resolution(
            person_id,
            company_name=company,
            status="manual_verified",
            error="",
        )
        DATABASE.reset_check_for_company_change(person_id, actual_run_id, company)
        DATABASE.update_run(actual_run_id, status="needs_enrichment")
        return JSONResponse(
            content={
                "success": True,
                "company_name": company,
                "approved": True,
                "confidence": result.get("confidence", "medium"),
                "reason": result.get("reason", ""),
                "message": f"Resolved and approved company: {company}",
            }
        )

    return JSONResponse(
        content={
            "success": True,
            "company_name": company,
            "approved": False,
            "confidence": result.get("confidence", "medium"),
            "reason": result.get("reason", ""),
        }
    )


@app.post("/runs/{run_id}/launch-browser")
def open_browser(run_id: int, background_tasks: BackgroundTasks) -> RedirectResponse:
    run = DATABASE.run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] in {"enriching", "collecting"}:
        return RedirectResponse(
            url=f"/?run_id={run_id}&message=This+run+is+already+busy", status_code=303
        )
    ready_rows = any(
        str(row.get("check_status") or "").casefold() in ENRICHED_CHECK_STATUSES
        for row in DATABASE.report_rows(run_id)
    )
    if not ready_rows:
        return RedirectResponse(
            url=f"/?run_id={run_id}&kind=error&message=Click+Enrich+records+first",
            status_code=303,
        )
    try:
        launch_chrome()
        DATABASE.update_run(run_id, status="collecting")
        background_tasks.add_task(run_collection, DATABASE, run_id, LOGIN_MONITOR)
        message, kind = (
            "Chrome+opened.+Log+in+and+automation+will+start+automatically.",
            "success",
        )
    except FileNotFoundError as exc:
        message, kind = str(exc).replace(" ", "+"), "error"
    return RedirectResponse(url=f"/?run_id={run_id}&kind={kind}&message={message}", status_code=303)


@app.post("/runs/{run_id}/enrich")
def enrich(run_id: int, background_tasks: BackgroundTasks) -> RedirectResponse:
    run = DATABASE.run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] in {"enriching", "collecting"}:
        return RedirectResponse(
            url=f"/?run_id={run_id}&message=This+run+is+already+busy", status_code=303
        )
    DATABASE.update_run(run_id, status="enriching")
    background_tasks.add_task(run_enrichment, DATABASE, run_id)
    return RedirectResponse(url=f"/?run_id={run_id}&message=Enrichment+started", status_code=303)


@app.post("/runs/{run_id}/collect")
def collect(run_id: int, background_tasks: BackgroundTasks) -> RedirectResponse:
    run = DATABASE.run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] in {"enriching", "collecting"}:
        return RedirectResponse(
            url=f"/?run_id={run_id}&message=This+run+is+already+busy", status_code=303
        )
    ready_rows = any(
        str(row.get("check_status") or "").casefold() in ENRICHED_CHECK_STATUSES
        for row in DATABASE.report_rows(run_id)
    )
    if not ready_rows:
        return RedirectResponse(
            url=f"/?run_id={run_id}&kind=error&message=Click+Enrich+records+first",
            status_code=303,
        )
    DATABASE.update_run(run_id, status="collecting")
    background_tasks.add_task(run_collection, DATABASE, run_id, LOGIN_MONITOR)
    return RedirectResponse(url=f"/?run_id={run_id}&message=Web+automation+started", status_code=303)


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


@app.get("/api/session-status")
def session_status() -> dict[str, Any]:
    """Return the live login state for the Chrome session used by automation."""

    return LOGIN_MONITOR.snapshot.as_dict()


@app.get("/api/runs/{run_id}/progress")
def run_progress(run_id: int) -> dict[str, Any]:
    """Small polling payload used by the dashboard without reloading the page."""

    return _run_progress(run_id)


@app.get("/api/runs/{run_id}/workspace")
def run_workspace(run_id: int) -> dict[str, str]:
    if not DATABASE.run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    rows = DATABASE.report_rows(run_id)
    return {
        "enriched": _enrichment_table(rows),
        "automation": _automation_table(rows),
        "final": _final_results_table(rows),
    }


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
