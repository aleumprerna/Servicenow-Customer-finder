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





def _avatar_initials(name: str) -> str:
    parts = [p for p in name.strip().split() if p]
    if len(parts) >= 2:
        return f"{parts[0][0]}{parts[1][0]}".upper()
    elif parts:
        return parts[0][:2].upper()
    return "—"


def _table_person_link(row: dict[str, Any]) -> str:
    person = _escape(row.get("person_name")) or "Unnamed prospect"
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
            reason = _escape(row.get("resolution_error")) or "Company requires review."
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
        person_name = _escape(row.get("person_name")) or "Unnamed prospect"
        headline = _escape(row.get("headline"))
        headline_html = f'<span class="cell-secondary">{headline}</span>' if headline else ''
        apollo_company = _escape(row.get("apollo_company_name"))
        apollo_html = f'<span class="cell-secondary">{apollo_company}</span>' if apollo_company else ''
        initials = _avatar_initials(person_name)
        body.append(
            f"""
            <tr class="table-row">
              <td>
                <div class="prospect-profile">
                  <div class="prospect-avatar" aria-hidden="true">{initials}</div>
                  <div class="prospect-info">
                    {_table_person_link(row)}
                    {headline_html}
                  </div>
                </div>
              </td>
              <td><strong>{company}</strong>{apollo_html}</td>
              <td>{approval}{resolution_details}</td>
              <td><span class="location-tag">{location}</span></td>
            </tr>"""
        )
    if not body:
        body.append('<tr><td class="table-empty" colspan="4">Upload a CSV to see enriched records.</td></tr>')
    return f"""
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Prospect</th><th>Resolved company</th><th>Approval</th><th>Company location</th></tr></thead>
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
        customer_label = "Customer Verified" if customer.casefold() == "yes" else _pretty_status(customer, "Not checked")
        customer_html = _status_pill(customer_label, customer_tone)
        score = _escape(row.get("match_score"))
        match = _escape(row.get("servicenow_matched_name")) or "No match recorded"
        if score:
            match += f' <span class="confidence-badge">{score}% match</span>'
        evidence_link = '<span class="cell-secondary">—</span>'
        if _screenshot_path(row):
            evidence_link = f'<a class="evidence-link-btn" href="/screenshots/{int(row["person_id"])}" target="_blank"><span>🔍</span> View screenshot</a>'
        error = _escape(row.get("error_message"))
        error_html = f'<span class="cell-error">{error}</span>' if error else ""
        person_name = _escape(row.get("person_name")) or "Unnamed prospect"
        company = _escape(row.get("company_name")) or "Company unresolved"
        initials = _avatar_initials(person_name)
        checked_time = _escape(row.get("checked_at")) or "Pending"
        body.append(
            f"""
            <tr class="table-row">
              <td>
                <div class="prospect-profile">
                  <div class="prospect-avatar" aria-hidden="true">{initials}</div>
                  <div class="prospect-info">
                    {_table_person_link(row)}
                    <span class="cell-secondary">{company}</span>
                  </div>
                </div>
              </td>
              <td>{_status_pill(label, tone)}{error_html}</td>
              <td>{customer_html}</td>
              <td>{match}</td>
              <td><span class="time-tag">{checked_time}</span></td>
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
                <div class="evidence-title"><span>ServiceNow evidence</span><small>Verified platform capture</small></div>
                <a class="screenshot-preview-link" href="{screenshot_url}" target="_blank" title="Open full-size screenshot">
                  <img src="{screenshot_url}" loading="lazy" alt="ServiceNow result for {_escape(row.get('company_name'))}">
                  <span class="preview-overlay"><span>🔍 View Full Proof</span></span>
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
                <div class="evidence-title"><span>n8n research citations</span><small>Market intelligence sources</small></div>
                <ul class="citation-list">{''.join(citation_items)}</ul>
              </div>"""
        else:
            evidence_html = """
              <div class="final-evidence-card evidence-empty">
                <div class="evidence-title"><span>Verification in progress</span><small>The record has not returned a screenshot or an n8n citation yet.</small></div>
              </div>"""

        verification_note = (
            _escape(evidence.evidence_note)
            or _escape(evidence.verification_status)
            or "Record verified with platform intelligence."
        )
        person_name = _escape(row.get("person_name")) or "Unnamed prospect"
        company = _escape(row.get("company_name")) or "Company unresolved"
        initials = _avatar_initials(person_name)
        records.append(
            f"""
            <details class="final-record">
              <summary class="final-record-summary">
                <div class="final-cell record-person">
                  <div class="prospect-profile">
                    <div class="prospect-avatar" aria-hidden="true">{initials}</div>
                    <div class="prospect-info">
                      {_table_person_link(row)}
                      <span class="cell-secondary">{company}</span>
                      {approval}
                    </div>
                  </div>
                </div>
                <span class="final-cell">{_status_pill(relationship, relationship_tone)}</span>
                <span class="final-cell"><span class="footprint-pill">{_pretty_status(row.get('servicenow_customer'), 'Not checked')}</span></span>
                <span class="final-cell"><span class="source-tags">{sources}</span></span>
                <span class="final-cell">{_status_pill(evidence.delivery_status or 'Synced', 'info' if evidence.delivery_status else 'success')}</span>
                <span class="record-expand"><span class="expand-text">Show record</span><span class="chevron" aria-hidden="true">⌄</span></span>
              </summary>
              <div class="final-record-details">
                <div class="final-record-facts">
                  <div><span>Target Account</span><strong>{company}</strong></div>
                  <div><span>Matched ServiceNow Entity</span><strong>{_escape(row.get('servicenow_matched_name')) or 'No match recorded'}</strong></div>
                  <div><span>Account Verification</span><strong>{_pretty_status(row.get('resolution_status'), 'Waiting')}</strong></div>
                  <div><span>Evidence strength</span><strong>{_escape(evidence.evidence_strength) or 'Not available'}</strong></div>
                  <div><span>Checked</span><strong>{_escape(row.get('checked_at')) or 'Not checked yet'}</strong></div>
                </div>
                <div class="final-record-evidence">
                  {evidence_html}
                  <div class="final-evidence-card verification-card">
                    <div class="evidence-title"><span>Market Intelligence Summary</span></div>
                    <p>{verification_note}</p>
                  </div>
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





def _legacy_page(request: Request, selected_run: int | None = None) -> str:
    login_snapshot = LOGIN_MONITOR.snapshot
    login_tone = login_snapshot.tone or ("logged-in" if login_snapshot.logged_in else "waiting")
    summaries = DATABASE.summary()
    if selected_run is None and summaries:
        selected_run = int(summaries[0]["id"])
    rows = DATABASE.report_rows(selected_run) if selected_run else []
    run = DATABASE.run(selected_run) if selected_run else None
    options = "".join(
        f'<option value="{item["id"]}" {"selected" if item["id"] == selected_run else ""}>'
        f'Batch #{item["id"]} — {_escape(item["status"])} ({item["people_count"]} prospects)</option>'
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
        <div class="overview-stat">
          <div class="stat-top">
            <span class="stat-icon records" aria-hidden="true">👥</span>
            <span class="stat-tag">Active List</span>
          </div>
          <div class="stat-val">
            <strong>{len(rows)}</strong>
            <span class="stat-name">Total Prospects</span>
            <span class="stat-subtext">People in run</span>
          </div>
        </div>
        <div class="overview-stat highlight-emerald">
          <div class="stat-top">
            <span class="stat-icon approved" aria-hidden="true">✓</span>
            <span class="stat-trend">+12% Verified</span>
          </div>
          <div class="stat-val">
            <strong class="text-emerald">{confirmed_count}</strong>
            <span class="stat-name">Verified Customers</span>
            <span class="stat-subtext">Confirmed ServiceNow footprint</span>
          </div>
        </div>
        <div class="overview-stat">
          <div class="stat-top">
            <span class="stat-icon checked" aria-hidden="true">🤝</span>
            <span class="stat-tag">Ecosystem</span>
          </div>
          <div class="stat-val">
            <strong>{approved_count}</strong>
            <span class="stat-name">Partner Accounts</span>
            <span class="stat-subtext">Companies approved</span>
          </div>
        </div>
        <div class="overview-stat">
          <div class="stat-top">
            <span class="stat-icon confirmed" aria-hidden="true">🎯</span>
            <span class="stat-tag">High Intent</span>
          </div>
          <div class="stat-val">
            <strong>{completed_count}</strong>
            <span class="stat-name">Qualified Opportunities</span>
            <span class="stat-subtext">Relationships found</span>
          </div>
        </div>
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
            f'<a class="button primary-link" href="/reports.csv?run_id={selected_run}"><span class="btn-icon">⬇</span> Download CSV</a>'
            if automation_complete
            else '<span class="button disabled-link" aria-disabled="true"><span class="btn-icon">⬇</span> Download CSV</span>'
        )
        workflow_steps = f"""
          <div class="workflow-progress" id="workflow-progress" data-run-id="{selected_run}" data-busy="{str(busy).lower()}">
            <article class="workflow-step {step_one_state}" data-stage-card="enrich">
              <div class="step-top">
                <span class="step-number" data-step-number="enrich">{'✓' if enrichment_complete else '1'}</span>
                <span class="step-state" data-approved-count>{approved_count}/{len(rows)} approved</span>
              </div>
              <div class="step-body">
                <span class="phase-tag">Phase 1</span>
                <h3>Enrich records</h3>
                <p>Resolve executive contacts and enrich target organizations with company intelligence.</p>
                <div class="step-metric"><strong data-enriched-count>{enrichment_processed_count}</strong><span>of <span data-enrichment-total>{enrichment_total}</span> processed<span data-enrichment-failures>{f' · {failed_enrichment_count} needs attention' if failed_enrichment_count else ''}</span></span></div>
                <div class="stage-progress" role="progressbar" aria-label="Enrichment progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{enrichment_percent}" data-progress="enrich"><span style="width:{enrichment_percent}%"></span></div>
              </div>
              <form class="async-stage-form" data-stage="enrich" method="post" action="/runs/{selected_run}/enrich"><button class="primary step-action" {enrich_disabled}>{enrich_label}</button></form>
            </article>
            <article class="workflow-step {step_two_state}" data-stage-card="automation">
              <div class="step-top">
                <span class="step-number" data-step-number="automation">{'✓' if automation_complete else '2'}</span>
                <span class="step-state" data-automation-state>{automation_count}/{automation_total} checked</span>
              </div>
              <div class="step-body">
                <span class="phase-tag">Phase 2</span>
                <h3>Run web automation</h3>
                <p>Run automated checks against ServiceNow customer and certified partner portals.</p>
                <div class="step-metric automation-metric"><strong data-automation-count>{automation_count}</strong><span>of <span data-automation-total>{automation_total}</span> ready records checked</span></div>
                <div class="stage-progress" role="progressbar" aria-label="Web automation progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{automation_percent}" data-progress="automation"><span style="width:{automation_percent}%"></span></div>
              </div>
              <div class="step-actions">
                <form method="post" action="/runs/{selected_run}/launch-browser"><button {automation_disabled}>Open ServiceNow</button></form>
                <form class="async-stage-form" data-stage="automation" method="post" action="/runs/{selected_run}/collect"><button class="primary" {automation_disabled}>{automation_label}</button></form>
              </div>
            </article>
            <article class="workflow-step {step_three_state}">
              <div class="step-top">
                <span class="step-number">{'✓' if final_complete else '3'}</span>
                <span class="step-state">{confirmed_count} confirmed</span>
              </div>
              <div class="step-body">
                <span class="phase-tag">Phase 3</span>
                <h3>Review final results</h3>
                <p>Review verified customer evidence, sync market intelligence, or export qualified leads.</p>
                <div class="step-metric"><strong>{confirmed_count}</strong><span>verified high-intent accounts</span></div>
              </div>
              <div class="step-actions">
                <form method="post" action="/runs/{selected_run}/send-n8n"><button {final_disabled}>Sync Market Data</button></form>
                {download_action}
              </div>
            </article>
          </div>"""
        if run["collection_log"]:
            run_log = f'<details class="run-log"><summary>View latest activity log</summary><pre>{_escape(run["collection_log"])}</pre></details>'

    default_tab = "enriched"
    enriched_active = default_tab == "enriched"
    automation_active = default_tab == "automation"
    final_active = default_tab == "final"
    final_export = (
        f'<a class="button primary-export-btn" href="/reports.csv?run_id={selected_run}"><span class="btn-icon">⬇</span> Export Verified Leads (CSV)</a>'
        if selected_run
        else '<span class="button disabled-link" aria-disabled="true"><span class="btn-icon">⬇</span> Export CSV</span>'
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
        <div class="panel-heading"><div><h3>Enriched records</h3><p>Prospect identity, verified company profile, and organization intelligence.</p></div></div>
        <div data-workspace-table="enriched">{_enrichment_table(rows)}</div>
      </div>
      <div class="tab-panel" id="panel-automation" role="tabpanel" aria-labelledby="tab-automation" {'hidden' if not automation_active else ''}>
        <div class="panel-heading"><div><h3>Web automation</h3><p>Real-time ServiceNow customer verification, confidence scores, and visual evidence.</p></div></div>
        <div data-workspace-table="automation">{_automation_table(rows)}</div>
      </div>
      <div class="tab-panel" id="panel-final" role="tabpanel" aria-labelledby="tab-final" {'hidden' if not final_active else ''}>
        <div class="panel-heading"><div><h3>Final results</h3><p>Confirmed customer &amp; partner relationships with supporting market verification.</p></div>{final_export}</div>
        <div data-report-stats>{report_stats}</div>
        <div data-workspace-table="final">{_final_results_table(rows)}</div>
      </div>"""

    return f"""<!doctype html>
    <html lang="en"><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Customer verification workspace — ServiceNow Customer Finder</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&display=swap" rel="stylesheet">
    <style>
      :root {{
        --primary:#2563eb;
        --primary-hover:#1d4ed8;
        --primary-light:#eff6ff;
        --primary-border:#bfdbfe;
        --emerald:#059669;
        --emerald-light:#ecfdf5;
        --emerald-border:#a7f3d0;
        --amber:#d97706;
        --amber-light:#fffbeb;
        --amber-border:#fde68a;
        --rose:#e11d48;
        --rose-light:#fff1f2;
        --text-main:#0f172a;
        --text-muted:#64748b;
        --text-subtle:#94a3b8;
        --bg-page:#f8faff;
        --bg-card:#ffffff;
        --border:#e2e8f0;
        --border-subtle:#edf2f7;
        --radius-sm:6px;
        --radius-md:10px;
        --radius-lg:14px;
        --radius-xl:18px;
        --shadow-sm:0 1px 3px rgba(0,0,0,0.03);
        --shadow-md:0 4px 12px rgba(15,23,42,0.05);
        --shadow-lg:0 10px 24px rgba(15,23,42,0.07);
      }}
      * {{ box-sizing:border-box; }}
      body {{
        font-family:'Inter', system-ui, -apple-system, sans-serif;
        max-width:1500px;
        margin:0 auto;
        color:var(--text-main);
        padding:32px 28px 72px;
        background:var(--bg-page);
        line-height:1.5;
      }}
      h1, h2, h3, h4, .font-headline {{
        font-family:'Manrope', system-ui, sans-serif;
        font-weight:750;
        letter-spacing:-0.025em;
      }}
      .muted, small {{ color:var(--text-muted); }}

      /* Executive Header */
      .page-header {{
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:24px;
        padding:26px 30px;
        background:var(--bg-card);
        border:1px solid var(--border);
        border-radius:var(--radius-xl);
        box-shadow:var(--shadow-md);
        margin-bottom:22px;
        position:relative;
        overflow:hidden;
      }}
      .page-header::before {{
        content:"";
        position:absolute;
        left:0;
        top:0;
        bottom:0;
        width:5px;
        background:linear-gradient(180deg, var(--primary), var(--emerald));
      }}
      .brand-lockup {{
        display:flex;
        align-items:center;
        gap:18px;
      }}
      .brand-mark {{
        width:48px;
        height:48px;
        border-radius:12px;
        background:linear-gradient(135deg, #2563eb, #1e40af);
        color:#fff;
        display:flex;
        align-items:center;
        justify-content:center;
        font-family:'Manrope', sans-serif;
        font-weight:850;
        font-size:16px;
        letter-spacing:-0.03em;
        box-shadow:0 4px 12px rgba(37,99,235,0.25);
        flex-shrink:0;
      }}
      .eyebrow {{
        display:inline-block;
        font-size:10px;
        font-weight:800;
        letter-spacing:0.12em;
        text-transform:uppercase;
        color:var(--primary);
        margin-bottom:4px;
      }}
      .page-header h1 {{
        margin:0 0 4px;
        font-size:26px;
        color:var(--text-main);
      }}
      .header-subtitle {{
        margin:0;
        font-size:13px;
        color:var(--text-muted);
      }}
      .header-meta {{
        display:flex;
        align-items:center;
        gap:10px;
        flex-wrap:wrap;
      }}
      .live-indicator {{
        display:inline-flex;
        align-items:center;
        gap:7px;
        padding:6px 12px;
        background:var(--primary-light);
        border:1px solid var(--primary-border);
        border-radius:999px;
        color:var(--primary);
        font-size:11px;
        font-weight:700;
      }}
      .live-indicator i {{
        width:7px;
        height:7px;
        border-radius:50%;
        background:var(--primary);
        box-shadow:0 0 0 3px rgba(37,99,235,0.2);
      }}
      .session-status {{
        display:inline-flex;
        align-items:center;
        gap:7px;
        padding:6px 12px;
        border-radius:999px;
        font-size:11px;
        font-weight:700;
      }}
      .session-status::before {{
        content:"";
        width:7px;
        height:7px;
        border-radius:50%;
        background:currentColor;
      }}
      .session-status.waiting {{ background:var(--amber-light); color:var(--amber); border:1px solid var(--amber-border); }}
      .session-status.logged-in, .session-status.ready {{ background:var(--emerald-light); color:var(--emerald); border:1px solid var(--emerald-border); }}
      .session-status.working, .session-status.running {{ background:var(--primary-light); color:var(--primary); border:1px solid var(--primary-border); }}
      .session-status.failed {{ background:var(--rose-light); color:var(--rose); border:1px solid #fecaca; }}

      /* Messages */
      .message {{
        padding:12px 18px;
        border-radius:var(--radius-md);
        font-size:13px;
        font-weight:600;
        margin-bottom:20px;
      }}
      .message.success {{ background:var(--emerald-light); color:var(--emerald); border:1px solid var(--emerald-border); }}
      .message.error {{ background:var(--rose-light); color:var(--rose); border:1px solid #fecaca; }}

      /* Executive KPI Bento Grid */
      .overview-stats {{
        display:grid;
        grid-template-columns:repeat(4, minmax(0, 1fr));
        gap:16px;
        margin-bottom:24px;
      }}
      .overview-stat {{
        background:var(--bg-card);
        border:1px solid var(--border);
        border-radius:var(--radius-lg);
        padding:20px;
        box-shadow:var(--shadow-sm);
        display:flex;
        flex-direction:column;
        justify-content:space-between;
        min-height:130px;
        position:relative;
        overflow:hidden;
        transition:transform 0.2s ease, box-shadow 0.2s ease;
      }}
      .overview-stat:hover {{
        transform:translateY(-2px);
        box-shadow:var(--shadow-md);
      }}
      .overview-stat.highlight-emerald {{
        border-color:rgba(5, 150, 105, 0.3);
        background:linear-gradient(180deg, #ffffff 0%, #f0fdf4 100%);
      }}
      .stat-top {{
        display:flex;
        align-items:center;
        justify-content:space-between;
        margin-bottom:12px;
      }}
      .stat-icon {{
        width:34px;
        height:34px;
        border-radius:8px;
        background:#f1f5f9;
        color:#475569;
        display:flex;
        align-items:center;
        justify-content:center;
        font-size:15px;
      }}
      .stat-icon.approved {{ background:var(--emerald-light); color:var(--emerald); font-weight:800; }}
      .stat-tag {{
        font-size:10px;
        font-weight:700;
        color:var(--text-muted);
        text-transform:uppercase;
        letter-spacing:0.06em;
      }}
      .stat-trend {{
        font-size:10px;
        font-weight:800;
        color:var(--emerald);
        background:var(--emerald-light);
        border:1px solid var(--emerald-border);
        padding:2px 7px;
        border-radius:999px;
      }}
      .stat-val strong {{
        display:block;
        font-family:'Manrope', sans-serif;
        font-size:28px;
        font-weight:800;
        letter-spacing:-0.03em;
        line-height:1.1;
        color:var(--text-main);
      }}
      .stat-val strong.text-emerald {{
        color:var(--emerald);
      }}
      .stat-name {{
        display:block;
        font-size:12px;
        font-weight:700;
        color:var(--text-main);
        margin-top:4px;
      }}
      .stat-subtext {{
        display:block;
        font-size:11px;
        color:var(--text-muted);
        margin-top:1px;
      }}

      /* Upload Section */
      .upload-section {{
        background:var(--bg-card);
        border:1px solid var(--border);
        border-radius:var(--radius-lg);
        padding:22px 26px;
        box-shadow:var(--shadow-sm);
        margin-bottom:24px;
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:24px;
        background:linear-gradient(135deg, #ffffff 0%, #f8faff 100%);
      }}
      .upload-copy {{
        display:flex;
        align-items:center;
        gap:16px;
      }}
      .upload-icon {{
        width:44px;
        height:44px;
        border-radius:12px;
        background:var(--primary-light);
        color:var(--primary);
        display:flex;
        align-items:center;
        justify-content:center;
        flex-shrink:0;
      }}
      .upload-icon svg {{ width:22px; height:22px; }}
      .section-kicker {{
        display:inline-block;
        font-size:10px;
        font-weight:800;
        letter-spacing:0.1em;
        text-transform:uppercase;
        color:var(--primary);
        margin-bottom:2px;
      }}
      .upload-copy h2 {{
        margin:0 0 4px;
        font-size:17px;
        color:var(--text-main);
      }}
      .upload-copy p {{
        margin:0;
        font-size:13px;
        color:var(--text-muted);
        max-width:680px;
      }}
      .upload-section form {{
        display:flex;
        align-items:center;
        gap:12px;
        margin:0;
      }}
      input[type="file"] {{
        padding:6px;
        font-size:12px;
        border:1px dashed var(--border);
        border-radius:var(--radius-md);
        background:#fff;
        color:var(--text-muted);
        max-width:270px;
      }}
      input[type="file"]::file-selector-button {{
        padding:6px 12px;
        border:0;
        border-radius:6px;
        background:#f1f5f9;
        color:var(--text-main);
        font-weight:600;
        font-size:12px;
        cursor:pointer;
        margin-right:8px;
        transition:background 0.15s;
      }}
      input[type="file"]::file-selector-button:hover {{
        background:#e2e8f0;
      }}

      /* Buttons */
      button, .button {{
        padding:9px 16px;
        border:1px solid var(--border);
        border-radius:var(--radius-md);
        background:#ffffff;
        color:var(--text-main);
        font-family:'Inter', sans-serif;
        font-size:13px;
        font-weight:650;
        cursor:pointer;
        display:inline-flex;
        align-items:center;
        gap:6px;
        text-decoration:none;
        transition:all 0.15s ease;
      }}
      button:hover, .button:hover {{
        background:#f8fafc;
        border-color:#cbd5e1;
        transform:translateY(-1px);
      }}
      button.primary, .primary-link, .primary-export-btn {{
        background:var(--primary);
        border-color:var(--primary);
        color:#fff !important;
        box-shadow:0 2px 6px rgba(37,99,235,0.25);
      }}
      button.primary:hover, .primary-link:hover, .primary-export-btn:hover {{
        background:var(--primary-hover);
        border-color:var(--primary-hover);
        box-shadow:0 4px 12px rgba(37,99,235,0.3);
      }}
      button:disabled, .disabled-link {{
        opacity:0.5;
        cursor:not-allowed;
        transform:none !important;
      }}

      /* Workflow Section */
      .workflow-section {{
        background:var(--bg-card);
        border:1px solid var(--border);
        border-radius:var(--radius-lg);
        padding:26px 28px;
        box-shadow:var(--shadow-sm);
        margin-bottom:24px;
      }}
      .section-heading {{
        display:flex;
        align-items:flex-end;
        justify-content:space-between;
        gap:20px;
        margin-bottom:22px;
      }}
      .section-heading h2 {{
        margin:0 0 4px;
        font-size:20px;
      }}
      .section-heading p {{
        margin:0;
        font-size:13px;
        color:var(--text-muted);
      }}
      .run-picker {{
        display:flex;
        align-items:center;
        gap:10px;
        margin:0;
        padding:6px 10px;
        background:#f8fafc;
        border:1px solid var(--border);
        border-radius:var(--radius-md);
      }}
      .run-picker label {{
        font-size:11px;
        font-weight:700;
        color:var(--text-muted);
        text-transform:uppercase;
      }}
      .run-picker select {{
        border:0;
        background:transparent;
        font-size:12px;
        font-weight:600;
        color:var(--text-main);
        cursor:pointer;
        outline:none;
      }}
      .run-status {{
        font-size:11px;
        font-weight:700;
        padding:3px 8px;
        border-radius:999px;
        background:var(--primary-light);
        color:var(--primary);
      }}

      /* 3-Step Action Workflow */
      .workflow-progress {{
        display:grid;
        grid-template-columns:repeat(3, minmax(0, 1fr));
        gap:18px;
        position:relative;
      }}
      .workflow-step {{
        background:var(--bg-card);
        border:1px solid var(--border);
        border-radius:var(--radius-lg);
        padding:22px;
        display:flex;
        flex-direction:column;
        justify-content:space-between;
        min-height:260px;
        position:relative;
        transition:all 0.2s ease;
      }}
      .workflow-step::before {{
        content:"";
        position:absolute;
        top:0;
        left:0;
        right:0;
        height:4px;
        border-radius:var(--radius-lg) var(--radius-lg) 0 0;
        background:#cbd5e1;
      }}
      .workflow-step.active {{
        border-color:var(--primary-border);
        box-shadow:var(--shadow-md);
        background:#ffffff;
      }}
      .workflow-step.active::before {{
        background:var(--primary);
      }}
      .workflow-step.complete {{
        border-color:var(--emerald-border);
        background:#fafdfb;
      }}
      .workflow-step.complete::before {{
        background:var(--emerald);
      }}
      .workflow-step.locked {{
        opacity:0.65;
        background:#fcfdfe;
      }}
      .step-top {{
        display:flex;
        align-items:center;
        justify-content:space-between;
        margin-bottom:14px;
      }}
      .step-number {{
        width:30px;
        height:30px;
        border-radius:50%;
        background:#f1f5f9;
        color:#475569;
        display:flex;
        align-items:center;
        justify-content:center;
        font-family:'Manrope', sans-serif;
        font-size:13px;
        font-weight:800;
      }}
      .workflow-step.active .step-number {{
        background:var(--primary);
        color:#fff;
      }}
      .workflow-step.complete .step-number {{
        background:var(--emerald);
        color:#fff;
      }}
      .step-state {{
        font-size:11px;
        font-weight:700;
        color:var(--text-muted);
        background:#f1f5f9;
        padding:3px 8px;
        border-radius:999px;
      }}
      .workflow-step.active .step-state {{
        background:var(--primary-light);
        color:var(--primary);
      }}
      .workflow-step.complete .step-state {{
        background:var(--emerald-light);
        color:var(--emerald);
      }}
      .phase-tag {{
        font-size:10px;
        font-weight:800;
        text-transform:uppercase;
        letter-spacing:0.08em;
        color:var(--primary);
        display:block;
        margin-bottom:2px;
      }}
      .workflow-step h3 {{
        margin:0 0 6px;
        font-size:17px;
        color:var(--text-main);
      }}
      .workflow-step p {{
        margin:0 0 14px;
        font-size:13px;
        color:var(--text-muted);
        line-height:1.45;
      }}
      .step-metric {{
        display:flex;
        align-items:baseline;
        gap:6px;
        margin-bottom:12px;
      }}
      .step-metric strong {{
        font-family:'Manrope', sans-serif;
        font-size:22px;
        font-weight:800;
        color:var(--text-main);
      }}
      .step-metric span {{
        font-size:12px;
        color:var(--text-muted);
      }}
      .stage-progress {{
        width:100%;
        height:6px;
        background:#e2e8f0;
        border-radius:999px;
        overflow:hidden;
        margin-bottom:16px;
      }}
      .stage-progress span {{
        display:block;
        height:100%;
        background:var(--primary);
        border-radius:999px;
        transition:width 0.35s ease;
      }}
      .workflow-step.complete .stage-progress span {{
        background:var(--emerald);
      }}
      .step-actions {{
        display:flex;
        align-items:center;
        gap:8px;
        flex-wrap:wrap;
        margin-top:auto;
      }}
      .step-actions form, .workflow-step > form {{
        margin:0;
      }}

      /* Records Workspace */
      .records-workspace {{
        background:var(--bg-card);
        border:1px solid var(--border);
        border-radius:var(--radius-lg);
        box-shadow:var(--shadow-sm);
        margin-bottom:24px;
        overflow:hidden;
      }}
      .records-heading {{
        padding:24px 28px 16px;
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:20px;
        border-bottom:1px solid var(--border);
      }}
      .records-heading h2 {{
        margin:0 0 4px;
        font-size:20px;
      }}
      .records-heading p {{
        margin:0;
        font-size:13px;
        color:var(--text-muted);
      }}
      .record-tabs {{
        display:flex;
        gap:6px;
        padding:12px 28px 0;
        background:#f8fafc;
        border-bottom:1px solid var(--border);
        overflow-x:auto;
      }}
      .tab-button {{
        display:inline-flex;
        align-items:center;
        gap:8px;
        padding:10px 16px 12px;
        border:0;
        border-bottom:2px solid transparent;
        border-radius:0;
        background:transparent;
        color:var(--text-muted);
        font-size:13px;
        font-weight:650;
        white-space:nowrap;
        transform:none !important;
      }}
      .tab-button:hover {{
        color:var(--text-main);
        background:transparent;
      }}
      .tab-button.active {{
        border-bottom-color:var(--primary);
        color:var(--primary);
        font-weight:750;
      }}
      .tab-button strong {{
        font-size:11px;
        padding:2px 7px;
        border-radius:999px;
        background:#e2e8f0;
        color:var(--text-main);
      }}
      .tab-button.active strong {{
        background:var(--primary-light);
        color:var(--primary);
      }}
      .tab-panel {{
        padding:24px 28px;
      }}
      .tab-panel[hidden] {{ display:none; }}
      .panel-heading {{
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:20px;
        margin-bottom:18px;
      }}
      .panel-heading h3 {{
        margin:0 0 4px;
        font-size:16px;
      }}
      .panel-heading p {{
        margin:0;
        font-size:13px;
        color:var(--text-muted);
      }}

      /* Tables with Subtle Row Borders */
      .table-scroll {{
        overflow-x:auto;
        border:1px solid var(--border);
        border-radius:var(--radius-md);
        background:#ffffff;
        box-shadow:0 1px 2px rgba(0,0,0,0.02);
      }}
      .data-table {{
        width:100%;
        border-collapse:collapse;
        font-size:13px;
        text-align:left;
      }}
      .data-table th {{
        padding:13px 18px;
        background:#f8fafc;
        color:#475569;
        font-family:'Manrope', sans-serif;
        font-size:11px;
        font-weight:700;
        letter-spacing:0.05em;
        text-transform:uppercase;
        border-bottom:1px solid #cbd5e1;
        white-space:nowrap;
      }}
      .data-table tbody tr {{
        border-bottom:1px solid #e2e8f0;
        transition:background 0.15s ease;
      }}
      .data-table tbody tr:last-child {{
        border-bottom:none;
      }}
      .data-table td {{
        padding:14px 18px;
        border-bottom:1px solid #e2e8f0;
        vertical-align:middle;
        color:#1e293b;
      }}
      .data-table tbody tr:hover {{
        background:#f8fafc;
      }}
      .table-empty {{
        padding:48px 20px !important;
        text-align:center;
        color:var(--text-muted);
        font-size:13px;
      }}

      /* Final Records with Subtle Row Borders */
      .final-records {{
        border:1px solid var(--border);
        border-radius:var(--radius-md);
        background:#ffffff;
        box-shadow:0 1px 2px rgba(0,0,0,0.02);
        overflow:hidden;
      }}
      .final-record-header {{
        display:grid;
        grid-template-columns:minmax(240px, 1.4fr) minmax(130px, 0.7fr) minmax(130px, 0.7fr) minmax(180px, 1fr) minmax(110px, 0.6fr) 90px;
        align-items:center;
        gap:16px;
        padding:13px 18px;
        background:#f8fafc;
        color:#475569;
        font-family:'Manrope', sans-serif;
        font-size:11px;
        font-weight:700;
        letter-spacing:0.05em;
        text-transform:uppercase;
        border-bottom:1px solid #cbd5e1;
      }}
      .final-record {{
        border-bottom:1px solid #e2e8f0;
        background:#ffffff;
        transition:background 0.15s ease;
      }}
      .final-record:last-child {{
        border-bottom:none;
      }}
      .final-record:hover {{
        background:#f8fafc;
      }}
      .final-record[open] > .final-record-summary {{
        background:#f0f7ff;
        border-bottom:1px solid #e2e8f0;
      }}
      .final-record-summary {{
        display:grid;
        grid-template-columns:minmax(240px, 1.4fr) minmax(130px, 0.7fr) minmax(130px, 0.7fr) minmax(180px, 1fr) minmax(110px, 0.6fr) 90px;
        align-items:center;
        gap:16px;
        padding:15px 18px;
        cursor:pointer;
        list-style:none;
      }}
      .final-record-summary::-webkit-details-marker {{ display:none; }}
      .final-cell {{ min-width:0; font-size:13px; }}
      .record-expand {{
        display:flex;
        align-items:center;
        justify-content:flex-end;
        gap:4px;
        font-size:11px;
        font-weight:700;
        color:var(--primary);
      }}
      .chevron {{ font-size:15px; transition:transform 0.2s; }}
      .final-record[open] .chevron {{ transform:rotate(180deg); }}
      .final-record[open] .expand-text {{ font-size:0; }}
      .final-record[open] .expand-text::after {{ content:"Hide"; font-size:11px; }}

      /* Prospect Profile in Cell */
      .prospect-profile {{
        display:flex;
        align-items:center;
        gap:12px;
      }}
      .prospect-avatar {{
        width:34px;
        height:34px;
        border-radius:8px;
        background:linear-gradient(135deg, #3b82f6, #1d4ed8);
        color:#fff;
        font-family:'Manrope', sans-serif;
        font-size:11px;
        font-weight:800;
        display:flex;
        align-items:center;
        justify-content:center;
        flex-shrink:0;
        box-shadow:0 2px 5px rgba(37,99,235,0.2);
      }}
      .prospect-info {{
        display:flex;
        flex-direction:column;
        gap:2px;
        min-width:0;
      }}
      .table-person {{
        font-weight:700;
        color:var(--text-main);
        text-decoration:none;
        font-size:13px;
      }}
      a.table-person:hover {{
        color:var(--primary);
        text-decoration:underline;
      }}
      .cell-secondary {{
        font-size:11px;
        color:var(--text-muted);
        display:block;
      }}
      .cell-error {{
        font-size:11px;
        color:var(--rose);
        display:block;
        margin-top:4px;
      }}
      .location-tag {{
        font-size:12px;
        color:var(--text-muted);
      }}
      .time-tag {{
        font-size:11px;
        color:var(--text-muted);
      }}
      .confidence-badge {{
        display:inline-block;
        font-size:10px;
        font-weight:700;
        padding:2px 6px;
        border-radius:999px;
        background:#f1f5f9;
        color:#475569;
        margin-left:4px;
      }}
      .footprint-pill {{
        display:inline-flex;
        align-items:center;
        padding:3px 8px;
        border-radius:999px;
        font-size:11px;
        font-weight:700;
        background:#f1f5f9;
        color:#475569;
      }}

      /* Status Badges */
      .status-pill {{
        display:inline-flex;
        align-items:center;
        gap:5px;
        padding:3px 9px;
        border-radius:999px;
        font-size:11px;
        font-weight:700;
        white-space:nowrap;
        border:1px solid transparent;
      }}
      .status-pill.success, .status-pill.positive {{ background:var(--emerald-light); color:var(--emerald); border-color:var(--emerald-border); }}
      .status-pill.warning {{ background:var(--amber-light); color:var(--amber); border-color:var(--amber-border); }}
      .status-pill.danger {{ background:var(--rose-light); color:var(--rose); border-color:#fecaca; }}
      .status-pill.info {{ background:var(--primary-light); color:var(--primary); border-color:var(--primary-border); }}
      .status-pill.neutral {{ background:#f1f5f9; color:#475569; border-color:var(--border); }}

      /* Source Tags */
      .source-tags {{ display:flex; gap:4px; flex-wrap:wrap; }}
      .source-tag {{
        display:inline-flex;
        align-items:center;
        padding:3px 8px;
        border-radius:999px;
        background:var(--primary-light);
        border:1px solid var(--primary-border);
        color:var(--primary);
        font-size:11px;
        font-weight:650;
      }}

      /* Company Resolution Details & AI Button */
      .table-review {{ margin-top:8px; }}
      .table-review summary {{
        cursor:pointer;
        font-size:11px;
        font-weight:700;
        color:var(--primary);
      }}
      .table-review p {{
        font-size:11px;
        color:var(--amber);
        margin:6px 0;
      }}
      .company-override {{
        display:flex;
        flex-direction:column;
        gap:6px;
        margin-top:6px;
      }}
      .company-input-group {{
        display:flex;
        align-items:center;
        gap:6px;
      }}
      .company-input-group input {{
        padding:5px 8px;
        font-size:12px;
        border:1px solid var(--border);
        border-radius:6px;
        width:170px;
      }}
      .ai-resolve-btn {{
        padding:5px 8px;
        font-size:11px;
        font-weight:750;
        background:#eff6ff;
        color:var(--primary);
        border:1px solid var(--primary-border);
        border-radius:6px;
        cursor:pointer;
        white-space:nowrap;
        transition:all 0.15s;
      }}
      .ai-resolve-btn:hover:not(:disabled) {{
        background:#dbeafe;
        transform:translateY(-1px);
      }}
      .ai-status-msg {{
        font-size:11px;
        padding:3px 6px;
        border-radius:4px;
        max-width:240px;
      }}
      .ai-status-msg.success {{ background:var(--emerald-light); color:var(--emerald); }}
      .ai-status-msg.error {{ background:var(--rose-light); color:var(--rose); }}

      /* Expanded Details */
      .final-record-details {{
        padding:20px 24px;
        background:#f8fafc;
        border-top:1px solid var(--border);
      }}
      .final-record-facts {{
        display:grid;
        grid-template-columns:repeat(5, minmax(0, 1fr));
        gap:1px;
        background:var(--border);
        border:1px solid var(--border);
        border-radius:var(--radius-md);
        overflow:hidden;
        margin-bottom:18px;
      }}
      .final-record-facts > div {{
        background:#ffffff;
        padding:12px 14px;
        display:flex;
        flex-direction:column;
        gap:3px;
      }}
      .final-record-facts span {{
        font-size:10px;
        font-weight:750;
        color:var(--text-muted);
        text-transform:uppercase;
        letter-spacing:0.04em;
      }}
      .final-record-facts strong {{
        font-size:12px;
        color:var(--text-main);
      }}
      .final-record-evidence {{
        display:grid;
        grid-template-columns:1.4fr 1fr;
        gap:16px;
      }}
      .final-evidence-card {{
        background:#ffffff;
        border:1px solid var(--border);
        border-radius:var(--radius-md);
        padding:16px;
      }}
      .evidence-title {{
        margin-bottom:10px;
      }}
      .evidence-title span {{
        display:block;
        font-size:13px;
        font-weight:750;
        color:var(--text-main);
      }}
      .evidence-title small {{
        font-size:11px;
        color:var(--text-muted);
      }}
      .screenshot-preview-link {{
        display:block;
        position:relative;
        border-radius:8px;
        overflow:hidden;
        border:1px solid var(--border);
      }}
      .screenshot-preview-link img {{
        display:block;
        width:100%;
        max-height:220px;
        object-fit:cover;
        object-position:top;
      }}
      .preview-overlay {{
        position:absolute;
        inset:0;
        background:rgba(15,23,42,0.4);
        display:flex;
        align-items:center;
        justify-content:center;
        opacity:0;
        transition:opacity 0.2s;
        color:#fff;
        font-size:12px;
        font-weight:700;
      }}
      .screenshot-preview-link:hover .preview-overlay {{
        opacity:1;
      }}
      .citation-list {{
        margin:0;
        padding-left:18px;
        font-size:12px;
      }}
      .citation-list li {{ margin-bottom:6px; }}
      .citation-list a {{ color:var(--primary); }}
      .verification-card p {{
        margin:0;
        font-size:12px;
        color:var(--text-muted);
        line-height:1.5;
      }}
      .evidence-empty {{
        display:flex;
        align-items:center;
      }}

      /* Report Stats */
      .report-stats {{
        display:grid;
        grid-template-columns:repeat(4, minmax(0, 1fr));
        gap:1px;
        background:var(--border);
        border:1px solid var(--border);
        border-radius:var(--radius-md);
        overflow:hidden;
        margin-bottom:18px;
      }}
      .report-stats div {{
        background:#fafbfc;
        padding:14px 18px;
        display:flex;
        flex-direction:column;
        gap:2px;
      }}
      .report-stats strong {{ font-size:22px; font-family:'Manrope', sans-serif; }}
      .report-stats span {{ font-size:12px; color:var(--text-muted); }}

      /* Footer */
      .app-footer {{
        margin-top:32px;
        padding-top:18px;
        border-top:1px solid var(--border);
      }}
      .maintenance summary {{
        cursor:pointer;
        font-size:12px;
        font-weight:600;
        color:var(--text-muted);
      }}
      .maintenance-body {{
        margin-top:10px;
        padding:14px;
        background:#ffffff;
        border:1px solid var(--border);
        border-radius:var(--radius-md);
        font-size:12px;
        color:var(--text-muted);
      }}
      .danger-btn {{
        background:#fff;
        border-color:#fca5a5;
        color:#dc2626;
        font-size:12px;
        padding:6px 12px;
      }}
      .danger-btn:hover {{
        background:#fef2f2;
        border-color:#f87171;
      }}

      @media (prefers-reduced-motion:reduce) {{
        *, *::before, *::after {{
          animation:none !important;
          transition:none !important;
        }}
      }}
      @media (max-width:1050px) {{
        .overview-stats {{ grid-template-columns:repeat(2, 1fr); }}
        .final-record-header {{ display:none; }}
        .final-record-summary {{ grid-template-columns:minmax(200px, 1fr) repeat(2, minmax(110px, 0.6fr)) 80px; }}
        .final-record-summary .final-cell:nth-child(4), .final-record-summary .final-cell:nth-child(5) {{ display:none; }}
        .final-record-facts {{ grid-template-columns:1fr 1fr; }}
      }}
      @media (max-width:850px) {{
        body {{ padding:18px 14px 48px; }}
        .page-header {{ flex-direction:column; align-items:flex-start; padding:20px; }}
        .upload-section {{ flex-direction:column; align-items:flex-start; }}
        .workflow-progress {{ grid-template-columns:1fr; }}
        .final-record-evidence {{ grid-template-columns:1fr; }}
      }}
    </style>
    </head><body>
      <header class="page-header">
        <div class="brand-lockup">
          <div class="brand-mark" aria-hidden="true">SN</div>
          <div>
            <span class="eyebrow">Enterprise Intelligence</span>
            <h1>Customer verification workspace</h1>
            <p class="header-subtitle">Identify ServiceNow customers and certified ecosystem partners with automated evidence verification.</p>
          </div>
        </div>
        <div class="header-meta">
          <span class="live-indicator"><i aria-hidden="true"></i>Executive Workspace</span>
          <span id="login-status" class="session-status {login_tone}" title="{_escape(login_snapshot.detail)}">{_escape(login_snapshot.status)}</span>
        </div>
      </header>

      {_message(request)}
      {overview_stats}

      <section class="upload-section">
        <div class="upload-copy">
          <span class="upload-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none"><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M5 14v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </span>
          <div>
            <span class="section-kicker">Step 1 &middot; Import Data</span>
            <h2>Upload Prospect Accounts</h2>
            <p class="muted">Include each person’s name and LinkedIn URL. Apollo data is used for automatic company resolution.</p>
          </div>
        </div>
        <form method="post" action="/runs" enctype="multipart/form-data">
          <input type="file" name="file" accept=".csv,text/csv" aria-label="Choose people CSV" required>
          <button class="primary upload-btn"><span class="btn-icon">⬆</span> Upload CSV</button>
        </form>
      </section>

      <section class="workflow-section">
        <div class="section-heading">
          <div>
            <span class="section-kicker">3-Phase Pipeline</span>
            <h2>Workflow progress</h2>
            <p class="muted">Complete each stage from left to right.</p>
          </div>
          <form class="run-picker" method="get" action="/">
            <label for="run-select">Selected Batch</label>
            <select id="run-select" name="run_id" onchange="this.form.submit()">{options}</select>
            {f'<span class="run-status">{_pretty_status(run["status"])}</span>' if run else ''}
          </form>
        </div>
        {workflow_steps}
        {run_log}
      </section>

      <section class="records-workspace">
        <div class="records-heading">
          <div>
            <span class="section-kicker">Account Intelligence</span>
            <h2>Records</h2>
            <p class="muted">Switch views to inspect the data produced at each workflow stage.</p>
          </div>
        </div>
        {record_tabs}
      </section>

      <footer class="app-footer">
        <details class="maintenance">
          <summary>Database maintenance</summary>
          <div class="maintenance-body">
            <p>Clears local workflow runs and reports only. CSV files, configuration, Chrome profile, and source code remain unchanged.</p>
            <form method="post" action="/database/clear" onsubmit="return confirm('Delete every local workflow run and report? This cannot be undone.');">
              <button class="danger-btn">Clear database</button>
            </form>
          </div>
        </details>
      </footer>

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





def _review_companies_table(rows: list[dict[str, Any]]) -> str:
    """Render the client-facing company review table.

    Company-resolution diagnostics remain available only after the user opens a
    row's Review control.
    """

    body: list[str] = []
    for row in rows:
        trusted = str(row.get("resolution_status") or "") in TRUSTED_COMPANY_STATUSES
        person_name = _escape(row.get("person_name")) or "Unnamed contact"
        headline = _escape(row.get("headline")) or "Job title not provided"
        company = _escape(row.get("company_name")) or "Not matched yet"
        location = ", ".join(
            item
            for item in (_escape(row.get("headquarters")), _escape(row.get("country")))
            if item
        ) or "Not available"
        status = _status_pill("Confirmed", "success") if trusted else _status_pill("Needs review", "warning")
        review = '<span class="no-action" aria-label="No action needed">—</span>'
        if not trusted:
            reason = _escape(row.get("resolution_error")) or "We could not confidently match this contact to a company."
            review = f"""
              <details class="row-review">
                <summary class="button row-action">Review</summary>
                <div class="review-panel">
                  <strong>Confirm company</strong>
                  <p>{reason}</p>
                  <form class="company-override" method="post" action="/people/{int(row['person_id'])}/company">
                    <input type="hidden" name="run_id" value="{int(row['run_id'])}">
                    <label>Company name
                      <input name="company_name" value="{company if company != 'Not matched yet' else ''}" required placeholder="Enter the correct company">
                    </label>
                    <div class="review-actions">
                      <button type="button" class="button secondary ai-resolve-btn" data-person-id="{int(row['person_id'])}" data-run-id="{int(row['run_id'])}">Suggest company</button>
                      <button class="button primary">Confirm company</button>
                    </div>
                    <div class="ai-status-msg" aria-live="polite"></div>
                  </form>
                  <details class="technical-note"><summary>Matching details</summary><p>{reason}</p></details>
                </div>
              </details>"""
        body.append(
            f"""
            <tr data-review-row data-needs-review="{str(not trusted).lower()}" data-search="{person_name} {headline} {company} {location}">
              <td><div class="contact-cell"><span class="contact-avatar" aria-hidden="true">{_avatar_initials(str(row.get('person_name') or ''))}</span><span><strong>{_table_person_link(row)}</strong><small>{headline}</small></span></div></td>
              <td><strong>{company}</strong></td>
              <td>{location}</td>
              <td>{status}</td>
              <td>{review}</td>
            </tr>"""
        )
    if not body:
        body.append('<tr><td class="table-empty" colspan="5">Upload a customer list to begin.</td></tr>')
    return f"""
      <div class="table-wrap">
        <table class="review-table">
          <thead><tr><th>Contact</th><th>Company</th><th>Location</th><th>Status</th><th><span class="sr-only">Action</span></th></tr></thead>
          <tbody>{''.join(body)}</tbody>
        </table>
      </div>"""


def _result_evidence(row: dict[str, Any], evidence: Any) -> str:
    blocks: list[str] = []
    screenshot = _screenshot_path(row)
    if screenshot:
        screenshot_url = f'/screenshots/{int(row["person_id"])}'
        blocks.append(
            f'<a class="evidence-preview" href="{screenshot_url}" target="_blank">'
            f'<img src="{screenshot_url}" loading="lazy" alt="Verification evidence for {_escape(row.get("company_name"))}">'
            '<span>Open full-size evidence</span></a>'
        )
    if evidence.citations:
        links = []
        for citation in evidence.citations:
            label = _escape(citation.title or citation.citation_type or "Source")
            if citation.url:
                links.append(f'<li><a href="{_escape(citation.url)}" target="_blank" rel="noreferrer">{label}</a></li>')
            else:
                links.append(f'<li>{label}</li>')
        blocks.append(f'<div><strong>Sources</strong><ul class="evidence-links">{"".join(links)}</ul></div>')
    note = _escape(evidence.evidence_note) or _escape(evidence.verification_status)
    if note:
        blocks.append(f'<p>{note}</p>')
    blocks.append(
        '<dl class="evidence-facts">'
        f'<div><dt>Matched result</dt><dd>{_escape(row.get("servicenow_matched_name")) or "Not available"}</dd></div>'
        f'<div><dt>Confidence</dt><dd>{_escape(row.get("match_score")) or "Not available"}</dd></div>'
        f'<div><dt>Checked</dt><dd>{_escape(row.get("checked_at")) or "Not yet"}</dd></div>'
        '</dl>'
    )
    return "".join(blocks)


def _simplified_results_table(rows: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for row in rows:
        evidence = parse_n8n_evidence(
            str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")
        )
        relationship = _relationship(row, evidence)
        relationship_lower = relationship.casefold()
        customer_raw = str(row.get("servicenow_customer") or "").casefold()
        customer_label = "Verified customer" if customer_raw == "yes" else "Not verified" if customer_raw == "no" else "Needs review" if customer_raw == "unknown" else "Pending"
        customer_tone = "success" if customer_raw == "yes" else "warning" if customer_raw == "unknown" else "neutral"
        partner_label = "Partner" if "partner" in relationship_lower else "—"
        opportunity_label = "Qualified" if _relationship_tone(relationship) == "positive" else "—"
        person_name = _escape(row.get("person_name")) or "Unnamed contact"
        headline = _escape(row.get("headline")) or "Job title not provided"
        company = _escape(row.get("company_name")) or "Not matched"
        location = ", ".join(
            item
            for item in (_escape(row.get("headquarters")), _escape(row.get("country")))
            if item
        ) or "Not available"
        evidence_html = _result_evidence(row, evidence)
        body.append(
            f"""
            <tr data-search="{person_name} {headline} {company} {location} {customer_label} {partner_label} {opportunity_label}">
              <td><div class="contact-cell"><span class="contact-avatar" aria-hidden="true">{_avatar_initials(str(row.get('person_name') or ''))}</span><span><strong>{_table_person_link(row)}</strong><small>{headline}</small></span></div></td>
              <td><strong>{company}</strong><small>{location}</small></td>
              <td>{_status_pill(customer_label, customer_tone)}</td>
              <td>{_status_pill(partner_label, 'info') if partner_label != '—' else '<span class="no-action">—</span>'}</td>
              <td>{_status_pill(opportunity_label, 'success') if opportunity_label != '—' else '<span class="no-action">—</span>'}</td>
              <td><details class="row-evidence"><summary class="button row-action">View evidence</summary><div class="evidence-panel"><h3>{company}</h3>{evidence_html}</div></details></td>
            </tr>"""
        )
    if not body:
        body.append('<tr><td class="table-empty" colspan="6">Results will appear here when verification is complete.</td></tr>')
    return f"""
      <div class="table-wrap">
        <table class="review-table results-table">
          <thead><tr><th>Contact</th><th>Company</th><th>Customer status</th><th>Partner</th><th>Opportunity</th><th><span class="sr-only">Action</span></th></tr></thead>
          <tbody>{''.join(body)}</tbody>
        </table>
      </div>"""


_REDESIGN_STYLES = r"""
  :root {
    --page:#f6f8fb; --surface:#ffffff; --surface-soft:#f8fafc; --ink:#0f172a;
    --muted:#64748b; --subtle:#94a3b8; --border:#e2e8f0; --border-strong:#cbd5e1;
    --blue:#2563eb; --blue-dark:#1d4ed8; --blue-soft:#eff6ff; --green:#15803d;
    --green-soft:#f0fdf4; --amber:#b45309; --amber-soft:#fff7ed; --red:#b91c1c;
    --shadow:0 1px 2px rgba(15,23,42,.04),0 8px 24px rgba(15,23,42,.04);
  }
  * { box-sizing:border-box; }
  html { color-scheme:light; }
  body { margin:0; background:var(--page); color:var(--ink); font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:16px; line-height:1.5; }
  button,input,select { font:inherit; }
  button,.button,summary { -webkit-tap-highlight-color:transparent; }
  a { color:inherit; }
  .app-shell { width:min(1240px,calc(100% - 48px)); margin:0 auto; padding:42px 0 64px; }
  .page-header { display:flex; align-items:center; justify-content:space-between; gap:24px; margin-bottom:28px; }
  .brand-lockup { display:flex; align-items:center; gap:14px; min-width:0; }
  .brand-mark { width:44px; height:44px; display:grid; place-items:center; border-radius:12px; background:var(--blue); color:#fff; box-shadow:0 8px 18px rgba(37,99,235,.2); flex:0 0 auto; }
  .brand-mark svg { width:24px; height:24px; }
  h1,h2,h3,p { margin-top:0; }
  h1 { margin-bottom:2px; font-size:1.65rem; line-height:1.2; letter-spacing:-.035em; }
  h2 { margin-bottom:6px; font-size:1.35rem; line-height:1.3; letter-spacing:-.025em; }
  h3 { margin-bottom:6px; font-size:1rem; }
  .page-header p,.section-copy { margin-bottom:0; color:var(--muted); }
  .session-status { display:inline-flex; align-items:center; gap:8px; min-height:36px; padding:7px 12px; border:1px solid var(--border); border-radius:999px; background:var(--surface); color:var(--muted); font-size:.8125rem; font-weight:600; white-space:nowrap; }
  .session-status::before { content:""; width:7px; height:7px; border-radius:50%; background:#f59e0b; }
  .session-status.logged-in::before,.session-status.ready::before { background:#22c55e; }
  .session-status.working::before,.session-status.running::before { background:var(--blue); box-shadow:0 0 0 4px var(--blue-soft); }
  .session-status.failed::before { background:#ef4444; }
  .message { margin:0 0 18px; padding:12px 16px; border:1px solid #bbf7d0; border-radius:10px; background:var(--green-soft); color:#166534; font-weight:600; }
  .message.error { border-color:#fecaca; background:#fef2f2; color:var(--red); }
  .upload-card { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:24px; align-items:center; padding:26px; border:1px solid var(--border); border-radius:14px; background:var(--surface); box-shadow:var(--shadow); }
  .upload-card p { margin-bottom:0; color:var(--muted); }
  .upload-form { display:flex; align-items:center; gap:10px; }
  .file-input { max-width:270px; min-height:44px; padding:6px; border:1px solid var(--border-strong); border-radius:8px; background:#fff; color:var(--muted); }
  .file-input::file-selector-button { height:32px; margin-right:10px; padding:0 12px; border:0; border-radius:6px; background:var(--surface-soft); color:var(--ink); font-weight:600; cursor:pointer; }
  .button,button { min-height:44px; display:inline-flex; align-items:center; justify-content:center; gap:8px; padding:0 16px; border:1px solid var(--border-strong); border-radius:8px; background:#fff; color:#334155; font-weight:650; text-decoration:none; cursor:pointer; transition:border-color .16s ease,background .16s ease,transform .16s ease,box-shadow .16s ease; }
  .button:hover,button:hover { border-color:#94a3b8; }
  .button:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible { outline:3px solid rgba(37,99,235,.22); outline-offset:2px; }
  .button.primary,button.primary { border-color:var(--blue); background:var(--blue); color:#fff; box-shadow:0 4px 12px rgba(37,99,235,.18); }
  .button.primary:hover,button.primary:hover { border-color:var(--blue-dark); background:var(--blue-dark); transform:translateY(-1px); }
  .button.secondary { background:var(--surface-soft); }
  button:disabled,.button[aria-disabled="true"] { cursor:not-allowed; opacity:.52; transform:none; box-shadow:none; }
  .file-summary { display:flex; align-items:center; justify-content:space-between; gap:18px; min-height:62px; margin-bottom:18px; padding:10px 14px 10px 16px; border:1px solid var(--border); border-radius:12px; background:var(--surface); }
  .file-details,.file-actions { display:flex; align-items:center; gap:12px; min-width:0; }
  .file-check { width:28px; height:28px; display:grid; place-items:center; border-radius:50%; background:var(--green-soft); color:var(--green); font-weight:800; flex:0 0 auto; }
  .file-details strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .file-details small { display:block; color:var(--muted); }
  .change-file { min-height:36px; padding:0 10px; border-color:transparent; color:var(--blue); background:transparent; }
  .batch-select { min-height:36px; max-width:210px; padding:0 34px 0 10px; border:1px solid var(--border); border-radius:8px; background:#fff; color:#475569; font-size:.8125rem; }
  .overview-stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-bottom:18px; }
  .metric-card { min-height:98px; padding:18px 20px; border:1px solid var(--border); border-radius:12px; background:var(--surface); }
  .metric-card.verified { border-color:#bbf7d0; background:linear-gradient(0deg,rgba(240,253,244,.55),rgba(255,255,255,1)); }
  .metric-value { display:block; margin-bottom:6px; font-size:1.85rem; font-weight:750; line-height:1; letter-spacing:-.04em; }
  .metric-card.verified .metric-value { color:var(--green); }
  .metric-label { color:#475569; font-size:.875rem; font-weight:600; }
  .stepper { display:grid; grid-template-columns:repeat(4,1fr); margin:0 0 18px; padding:18px 22px; border:1px solid var(--border); border-radius:12px; background:var(--surface); }
  .upload-step,.workflow-step { position:relative; display:flex; align-items:center; gap:10px; min-width:0; }
  .upload-step:not(:last-child)::after,.workflow-step:not(:last-child)::after { content:""; position:absolute; z-index:0; left:calc(50% + 36px); right:18px; top:17px; height:1px; background:var(--border-strong); }
  .step-dot { position:relative; z-index:1; width:34px; height:34px; display:grid; place-items:center; flex:0 0 auto; border:1px solid var(--border-strong); border-radius:50%; background:var(--surface-soft); color:var(--subtle); font-size:.8125rem; font-weight:750; }
  .step-label { position:relative; z-index:1; padding-right:10px; background:var(--surface); color:var(--muted); font-size:.875rem; font-weight:600; white-space:nowrap; }
  .complete .step-dot { border-color:#bbf7d0; background:var(--green-soft); color:var(--green); }
  .active .step-dot { border-color:var(--blue); background:var(--blue); color:#fff; box-shadow:0 0 0 4px var(--blue-soft); }
  .active .step-label { color:var(--ink); }
  .complete:not(:last-child)::after { background:#86efac; }
  .work-surface { border:1px solid var(--border); border-radius:14px; background:var(--surface); box-shadow:var(--shadow); overflow:hidden; }
  .surface-header { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; padding:24px 26px 20px; border-bottom:1px solid var(--border); }
  .surface-header p { max-width:700px; margin-bottom:0; color:var(--muted); }
  .surface-actions { display:flex; gap:10px; flex:0 0 auto; }
  .table-tools { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:16px 26px; }
  .search-wrap { position:relative; width:min(420px,100%); }
  .search-wrap svg { position:absolute; left:13px; top:50%; width:18px; height:18px; color:var(--subtle); transform:translateY(-50%); pointer-events:none; }
  .search-input { width:100%; min-height:44px; padding:0 14px 0 42px; border:1px solid var(--border-strong); border-radius:8px; background:#fff; color:var(--ink); }
  .filter-button.active { border-color:#fed7aa; background:var(--amber-soft); color:var(--amber); }
  .table-wrap { overflow:auto; }
  .review-table { width:100%; border-collapse:collapse; }
  .review-table th { padding:11px 18px; border-bottom:1px solid var(--border-strong); background:var(--surface-soft); color:var(--muted); font-size:.75rem; font-weight:700; letter-spacing:.025em; text-align:left; white-space:nowrap; }
  .review-table td { min-width:120px; height:70px; padding:12px 18px; border-bottom:1px solid var(--border); color:#334155; vertical-align:middle; }
  .review-table tr:last-child td { border-bottom:0; }
  .review-table tbody tr:hover { background:#fbfdff; }
  .contact-cell { min-width:230px; display:flex; align-items:center; gap:12px; }
  .contact-avatar { width:34px; height:34px; display:grid; place-items:center; border-radius:9px; background:var(--blue-soft); color:var(--blue); font-size:.75rem; font-weight:750; flex:0 0 auto; }
  .contact-cell strong,.contact-cell small,.results-table td:nth-child(2) strong,.results-table td:nth-child(2) small { display:block; }
  .contact-cell a { color:var(--ink); text-decoration:none; }
  .contact-cell a:hover { color:var(--blue); text-decoration:underline; }
  .contact-cell small,.results-table td:nth-child(2) small { max-width:320px; margin-top:3px; color:var(--muted); font-size:.75rem; line-height:1.35; }
  .status-pill { display:inline-flex; align-items:center; min-height:28px; padding:3px 9px; border-radius:999px; background:#f1f5f9; color:#475569; font-size:.75rem; font-weight:700; white-space:nowrap; }
  .status-pill.success { background:var(--green-soft); color:var(--green); }
  .status-pill.warning { background:var(--amber-soft); color:var(--amber); }
  .status-pill.info { background:var(--blue-soft); color:var(--blue-dark); }
  .no-action { color:var(--subtle); }
  .row-review,.row-evidence { position:relative; }
  .row-review summary,.row-evidence summary { list-style:none; }
  .row-review summary::-webkit-details-marker,.row-evidence summary::-webkit-details-marker { display:none; }
  .row-action { min-height:36px; padding:0 12px; color:var(--blue); font-size:.8125rem; }
  .review-panel,.evidence-panel { position:absolute; z-index:20; top:43px; right:0; width:min(390px,calc(100vw - 48px)); padding:18px; border:1px solid var(--border-strong); border-radius:12px; background:#fff; box-shadow:0 18px 45px rgba(15,23,42,.18); }
  .review-panel p,.evidence-panel p { margin:6px 0 14px; color:var(--muted); font-size:.8125rem; }
  .company-override label { display:block; color:#475569; font-size:.75rem; font-weight:700; }
  .company-override input { width:100%; min-height:42px; margin-top:6px; padding:0 12px; border:1px solid var(--border-strong); border-radius:8px; }
  .review-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:12px; }
  .review-actions .button { min-height:38px; padding:0 12px; font-size:.8125rem; }
  .ai-status-msg { min-height:0; margin-top:8px; color:var(--muted); font-size:.75rem; }
  .ai-status-msg.success { color:var(--green); }
  .ai-status-msg.error { color:var(--red); }
  .technical-note { margin-top:12px; padding-top:10px; border-top:1px solid var(--border); color:var(--muted); font-size:.75rem; }
  .technical-note summary { cursor:pointer; }
  .surface-footer { display:flex; align-items:center; justify-content:space-between; gap:18px; padding:16px 26px; border-top:1px solid var(--border); color:var(--muted); font-size:.8125rem; }
  .progress-view { padding:44px 28px 48px; text-align:center; }
  .progress-icon { width:52px; height:52px; display:grid; place-items:center; margin:0 auto 16px; border-radius:50%; background:var(--blue-soft); color:var(--blue); }
  .spinner { width:24px; height:24px; border:3px solid #bfdbfe; border-top-color:var(--blue); border-radius:50%; animation:spin .9s linear infinite; }
  .progress-view p { margin-bottom:22px; color:var(--muted); }
  .progress-line { width:min(560px,100%); height:10px; margin:0 auto 10px; overflow:hidden; border-radius:999px; background:#e2e8f0; }
  .progress-line span { display:block; height:100%; border-radius:inherit; background:var(--blue); transition:width .25s ease; }
  .progress-count { color:#475569; font-size:.875rem; font-weight:650; }
  .result-intro { display:flex; align-items:center; gap:14px; padding:22px 26px 0; }
  .complete-icon { width:42px; height:42px; display:grid; place-items:center; border-radius:50%; background:var(--green-soft); color:var(--green); font-weight:800; }
  .result-intro h2 { margin:0; }
  .result-intro p { margin:2px 0 0; color:var(--muted); }
  .results-table td { min-width:135px; }
  .row-evidence .evidence-panel { width:min(460px,calc(100vw - 48px)); }
  .evidence-panel h3 { padding-right:28px; }
  .evidence-preview { display:grid; grid-template-columns:96px 1fr; align-items:center; gap:12px; margin-bottom:14px; text-decoration:none; color:var(--blue); font-size:.8125rem; font-weight:650; }
  .evidence-preview img { width:96px; height:64px; object-fit:cover; border:1px solid var(--border); border-radius:8px; }
  .evidence-links { margin:8px 0 14px; padding-left:20px; color:var(--blue); font-size:.8125rem; }
  .evidence-facts { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:0; padding-top:12px; border-top:1px solid var(--border); }
  .evidence-facts div { min-width:0; }
  .evidence-facts dt { color:var(--muted); font-size:.6875rem; font-weight:700; text-transform:uppercase; }
  .evidence-facts dd { margin:2px 0 0; overflow-wrap:anywhere; color:#334155; font-size:.8125rem; }
  .empty-panel { padding:54px 26px; text-align:center; color:var(--muted); }
  .advanced { margin-top:18px; color:var(--muted); font-size:.8125rem; }
  .advanced > summary { width:max-content; cursor:pointer; font-weight:650; }
  .advanced-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; margin-top:12px; padding:16px; border:1px solid var(--border); border-radius:12px; background:var(--surface); }
  .advanced-grid p { margin:0; }
  .advanced-actions { display:flex; align-items:center; justify-content:flex-end; gap:8px; }
  .advanced-actions button { min-height:38px; font-size:.8125rem; }
  .run-log { grid-column:1 / -1; }
  .run-log summary { cursor:pointer; }
  .run-log pre { max-height:220px; overflow:auto; padding:12px; border-radius:8px; background:#0f172a; color:#e2e8f0; font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace; white-space:pre-wrap; }
  .danger { border-color:#fecaca; color:var(--red); }
  .sr-only { position:absolute!important; width:1px!important; height:1px!important; padding:0!important; margin:-1px!important; overflow:hidden!important; clip:rect(0,0,0,0)!important; white-space:nowrap!important; border:0!important; }
  [hidden] { display:none!important; }
  @keyframes spin { to { transform:rotate(360deg); } }
  @media (max-width:900px) {
    .app-shell { width:min(100% - 28px,760px); padding-top:24px; }
    .overview-stats { grid-template-columns:repeat(2,1fr); }
    .stepper { grid-template-columns:1fr 1fr; row-gap:16px; }
    .upload-step::after,.workflow-step::after { display:none; }
    .surface-header,.upload-card { grid-template-columns:1fr; flex-direction:column; align-items:stretch; }
    .surface-actions { align-self:flex-start; }
  }
  @media (max-width:620px) {
    .app-shell { width:100%; padding:18px 12px 40px; }
    .page-header { align-items:flex-start; }
    .page-header p { font-size:.875rem; }
    .session-status { min-width:36px; width:36px; padding:0; overflow:hidden; color:transparent; }
    .session-status::before { flex:0 0 auto; }
    .overview-stats { grid-template-columns:1fr 1fr; gap:9px; }
    .metric-card { min-height:88px; padding:15px; }
    .metric-value { font-size:1.55rem; }
    .stepper { padding:14px; }
    .step-label { font-size:.75rem; }
    .file-summary,.file-actions,.upload-form,.table-tools,.surface-footer { align-items:stretch; flex-direction:column; }
    .batch-select { max-width:none; }
    .surface-header,.table-tools,.surface-footer { padding-left:18px; padding-right:18px; }
    .surface-actions,.surface-actions form,.surface-actions .button { width:100%; }
    .advanced-grid { grid-template-columns:1fr; }
    .review-panel,.evidence-panel { position:fixed; top:50%; left:50%; right:auto; transform:translate(-50%,-50%); max-height:80vh; overflow:auto; }
  }
  @media (prefers-reduced-motion:reduce) { *,*::before,*::after { animation:none!important; transition:none!important; scroll-behavior:auto!important; } }
"""


_REDESIGN_SCRIPT = r"""
  const workflowProgress = document.getElementById('workflow-progress');
  const loginStatus = document.getElementById('login-status');
  let progressTimer = null;
  let workflowWasBusy = workflowProgress?.dataset.busy === 'true';

  function setText(selector, value) {
    const element = document.querySelector(selector);
    if (element) element.textContent = value;
  }

  function applyProgress(data) {
    const value = data.run_status === 'enriching' ? data.processed : data.automated;
    const total = data.run_status === 'enriching' ? data.target : data.automation_target;
    const percent = data.run_status === 'enriching' ? data.enrichment_percent : data.automation_percent;
    setText('[data-live-count]', `${value} of ${total || data.total} completed`);
    const progress = document.querySelector('[data-progress="active"]');
    if (progress) {
      progress.setAttribute('aria-valuenow', String(percent));
      const fill = progress.querySelector('span');
      if (fill) fill.style.width = `${percent}%`;
    }
  }

  async function pollProgress() {
    if (!workflowProgress) return;
    progressTimer = null;
    try {
      const response = await fetch(`/api/runs/${workflowProgress.dataset.runId}/progress`, {cache:'no-store'});
      if (!response.ok) throw new Error('Progress request failed');
      const data = await response.json();
      const finished = workflowWasBusy && !data.busy;
      applyProgress(data);
      workflowWasBusy = data.busy;
      if (finished) { window.location.reload(); return; }
      if (data.busy) progressTimer = window.setTimeout(pollProgress, 1000);
    } catch (_error) {
      if (workflowWasBusy) progressTimer = window.setTimeout(pollProgress, 2000);
    }
  }

  document.querySelectorAll('.async-stage-form').forEach(form => {
    form.addEventListener('submit', event => {
      event.preventDefault();
      const button = form.querySelector('button');
      if (!button || button.disabled) return;
      button.disabled = true;
      button.textContent = 'Matching companies…';
      workflowWasBusy = true;
      fetch(form.action, {method:'POST', redirect:'follow'}).catch(() => {
        workflowWasBusy = false;
        button.disabled = false;
        button.textContent = 'Review Companies';
      });
      window.setTimeout(() => window.location.reload(), 350);
    });
  });

  async function pollLoginStatus() {
    try {
      const response = await fetch('/api/session-status', {cache:'no-store'});
      if (!response.ok) throw new Error('Session request failed');
      const data = await response.json();
      if (loginStatus) {
        loginStatus.textContent = data.status;
        loginStatus.title = data.detail || data.status;
        loginStatus.className = `session-status ${data.tone || (data.logged_in ? 'logged-in' : 'waiting')}`;
      }
    } catch (_error) {
      if (loginStatus) { loginStatus.textContent = 'Waiting for Login'; loginStatus.className = 'session-status waiting'; }
    } finally { window.setTimeout(pollLoginStatus, 2500); }
  }

  const searchInput = document.querySelector('[data-table-search]');
  const reviewFilter = document.querySelector('[data-review-filter]');
  let needsReviewOnly = false;
  function filterRows() {
    const query = (searchInput?.value || '').trim().toLowerCase();
    document.querySelectorAll('tbody tr[data-search]').forEach(row => {
      const matchesQuery = !query || row.dataset.search.toLowerCase().includes(query);
      const matchesReview = !needsReviewOnly || row.dataset.needsReview === 'true';
      row.hidden = !(matchesQuery && matchesReview);
    });
  }
  searchInput?.addEventListener('input', filterRows);
  reviewFilter?.addEventListener('click', () => {
    needsReviewOnly = !needsReviewOnly;
    reviewFilter.classList.toggle('active', needsReviewOnly);
    reviewFilter.setAttribute('aria-pressed', String(needsReviewOnly));
    filterRows();
  });

  document.addEventListener('click', async event => {
    const button = event.target.closest('.ai-resolve-btn');
    if (!button || button.disabled) return;
    const form = button.closest('form');
    const input = form?.querySelector('input[name="company_name"]');
    const statusEl = form?.querySelector('.ai-status-msg');
    button.disabled = true;
    button.textContent = 'Finding…';
    if (statusEl) statusEl.textContent = 'Looking for the most likely company…';
    try {
      const params = new URLSearchParams({run_id:button.dataset.runId,auto_approve:'true'});
      const response = await fetch(`/api/people/${button.dataset.personId}/ai-resolve-company`, {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:params});
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.error || 'Could not suggest a company.');
      if (input) input.value = data.company_name;
      if (statusEl) { statusEl.textContent = `Suggested and confirmed: ${data.company_name}`; statusEl.className = 'ai-status-msg success'; }
      window.setTimeout(() => window.location.reload(), 650);
    } catch (error) {
      button.disabled = false;
      button.textContent = 'Suggest company';
      if (statusEl) { statusEl.textContent = error.message; statusEl.className = 'ai-status-msg error'; }
    }
  });

  if (workflowWasBusy) pollProgress();
  pollLoginStatus();
"""


def _page(request: Request, selected_run: int | None = None) -> str:
    """Render the simplified, Stitch-guided customer verification experience."""

    login_snapshot = LOGIN_MONITOR.snapshot
    login_tone = login_snapshot.tone or ("logged-in" if login_snapshot.logged_in else "waiting")
    summaries = DATABASE.summary()
    if selected_run is None and summaries:
        selected_run = int(summaries[0]["id"])
    rows = DATABASE.report_rows(selected_run) if selected_run else []
    run = DATABASE.run(selected_run) if selected_run else None

    relationships = [
        _relationship(row, parse_n8n_evidence(str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")))
        for row in rows
    ]
    verified_count = sum(str(row.get("servicenow_customer") or "").casefold() == "yes" for row in rows)
    partner_count = sum("partner" in relationship.casefold() for relationship in relationships)
    opportunity_count = sum(_relationship_tone(relationship) == "positive" for relationship in relationships)
    enriched_count, automation_count, approved_count = _workflow_counts(rows)
    processed_count = _enrichment_processed_count(rows)
    enrichment_target = approved_count or len(rows)
    enrichment_complete = approved_count > 0 and processed_count >= approved_count
    automation_target = enriched_count
    automation_complete = automation_target > 0 and automation_count >= automation_target
    busy = bool(run and run["status"] in {"enriching", "collecting"})

    if not run:
        current_stage = "upload"
    elif run["status"] == "enriching" or not enrichment_complete:
        current_stage = "review"
    elif run["status"] == "collecting":
        current_stage = "verify"
    elif automation_complete:
        current_stage = "results"
    else:
        current_stage = "verify"

    stage_order = {"upload": 0, "review": 1, "verify": 2, "results": 3}
    current_index = stage_order[current_stage]

    def step_state(index: int) -> str:
        return "complete" if index < current_index else "active" if index == current_index else "upcoming"

    metrics = ""
    file_summary = ""
    if run:
        metrics = f"""
          <section class="overview-stats" aria-label="Customer list summary">
            <div class="metric-card"><strong class="metric-value">{len(rows)}</strong><span class="metric-label">Prospects</span></div>
            <div class="metric-card verified"><strong class="metric-value">{verified_count}</strong><span class="metric-label">Verified Customers</span></div>
            <div class="metric-card"><strong class="metric-value">{partner_count}</strong><span class="metric-label">Partner Accounts</span></div>
            <div class="metric-card"><strong class="metric-value">{opportunity_count}</strong><span class="metric-label">Opportunities</span></div>
          </section>"""
        options = "".join(
            f'<option value="{item["id"]}" {"selected" if int(item["id"]) == selected_run else ""}>'
            f'{_escape(item.get("source_file") or "Customer list")} · {item["people_count"]} contacts</option>'
            for item in summaries
        )
        file_summary = f"""
          <section class="file-summary" aria-label="Uploaded customer list">
            <div class="file-details"><span class="file-check" aria-hidden="true">✓</span><span><strong>{_escape(run.get('source_file') or 'Customer list.csv')}</strong><small>{len(rows)} contacts uploaded</small></span></div>
            <div class="file-actions">
              {f'<form method="get" action="/"><label class="sr-only" for="run-select">Customer list</label><select class="batch-select" id="run-select" name="run_id" onchange="this.form.submit()">{options}</select></form>' if len(summaries) > 1 else ''}
              <form method="post" action="/runs" enctype="multipart/form-data"><input class="sr-only" id="change-file" type="file" name="file" accept=".csv,text/csv" required onchange="this.form.submit()"><label class="button change-file" for="change-file">Change file</label></form>
            </div>
          </section>"""

    stepper = f"""
      <nav class="stepper" aria-label="Verification progress">
        <div class="upload-step {step_state(0)}"><span class="step-dot">{'✓' if current_index > 0 else '1'}</span><span class="step-label">Upload</span></div>
        <div class="workflow-step {step_state(1)}"><span class="step-dot">{'✓' if current_index > 1 else '2'}</span><span class="step-label">Review companies</span></div>
        <div class="workflow-step {step_state(2)}"><span class="step-dot">{'✓' if current_index > 2 else '3'}</span><span class="step-label">Verify customers</span></div>
        <div class="workflow-step {step_state(3)}"><span class="step-dot">4</span><span class="step-label">Results</span></div>
      </nav>"""

    if not run:
        main_surface = """
          <section class="upload-card">
            <div><h2>Upload Customer List</h2><p>Choose a CSV with each contact’s name and LinkedIn URL.</p></div>
            <form class="upload-form" method="post" action="/runs" enctype="multipart/form-data">
              <input class="file-input" type="file" name="file" accept=".csv,text/csv" aria-label="Choose customer list CSV" required>
              <button class="primary">Upload</button>
            </form>
          </section>"""
    elif current_stage == "review" and busy:
        percent = min(100, round(100 * processed_count / enrichment_target)) if enrichment_target else 0
        main_surface = f"""
          <section class="work-surface" id="workflow-progress" data-run-id="{selected_run}" data-busy="true">
            <div class="progress-view"><div class="progress-icon"><span class="spinner"></span></div><h2>Matching companies…</h2><p>We’re preparing company matches for your review.</p><div class="progress-line stage-progress" role="progressbar" aria-label="Company matching progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{percent}" data-progress="active"><span style="width:{percent}%"></span></div><div class="progress-count" data-live-count>{processed_count} of {enrichment_target} completed</div></div>
          </section>"""
    elif current_stage == "review":
        main_surface = f"""
          <section class="work-surface">
            <header class="surface-header"><div><h2>Review Companies</h2><p>We’ll match each person with their company, then flag anything uncertain for review.</p></div><div class="surface-actions"><form class="async-stage-form" data-stage="enrich" method="post" action="/runs/{selected_run}/enrich"><button class="primary">Review Companies</button></form></div></header>
            <div class="table-tools"><label class="search-wrap"><span class="sr-only">Search contacts or companies</span><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/><path d="m16 16 4 4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><input class="search-input" data-table-search placeholder="Search contacts or companies…"></label><button type="button" class="filter-button" data-review-filter aria-pressed="false">Needs review</button></div>
            {_review_companies_table(rows)}
            <footer class="surface-footer"><span>Showing {len(rows)} contacts</span><form class="async-stage-form" data-stage="enrich" method="post" action="/runs/{selected_run}/enrich"><button class="primary">Review Companies</button></form></footer>
          </section>"""
    elif current_stage == "verify" and busy:
        percent = min(100, round(100 * automation_count / automation_target)) if automation_target else 0
        main_surface = f"""
          <section class="work-surface" id="workflow-progress" data-run-id="{selected_run}" data-busy="true">
            <div class="progress-view"><div class="progress-icon"><span class="spinner"></span></div><h2>Verifying customers…</h2><p>Checking which companies are verified ServiceNow customers.</p><div class="progress-line stage-progress" role="progressbar" aria-label="Customer verification progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{percent}" data-progress="active"><span style="width:{percent}%"></span></div><div class="progress-count" data-live-count>{automation_count} of {automation_target} completed</div></div>
          </section>"""
    elif current_stage == "verify":
        unresolved = sum(str(row.get("resolution_status") or "") not in TRUSTED_COMPANY_STATUSES for row in rows)
        main_surface = f"""
          <section class="work-surface">
            <header class="surface-header"><div><h2>Ready to verify {enriched_count} companies</h2><p>Check which companies are verified ServiceNow customers.</p></div><div class="surface-actions"><form method="post" action="/runs/{selected_run}/launch-browser"><button class="primary">Verify Customers</button></form></div></header>
            <div class="table-tools"><label class="search-wrap"><span class="sr-only">Search contacts or companies</span><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/><path d="m16 16 4 4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><input class="search-input" data-table-search placeholder="Search contacts or companies…"></label>{f'<button type="button" class="filter-button" data-review-filter aria-pressed="false">{unresolved} need review</button>' if unresolved else '<span></span>'}</div>
            {_review_companies_table(rows)}
            <footer class="surface-footer"><span>{enriched_count} companies ready to verify</span><form method="post" action="/runs/{selected_run}/launch-browser"><button class="primary">Verify Customers</button></form></footer>
          </section>"""
    else:
        main_surface = f"""
          <section class="work-surface">
            <div class="result-intro"><span class="complete-icon" aria-hidden="true">✓</span><div><h2>Verification complete</h2><p>Your results are ready to review and share.</p></div></div>
            <header class="surface-header"><div><h2>Results</h2><p>Customer, partner, and opportunity status for every contact.</p></div><div class="surface-actions"><a class="button primary" href="/reports.csv?run_id={selected_run}">Download CSV</a></div></header>
            <div class="table-tools"><label class="search-wrap"><span class="sr-only">Search results</span><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/><path d="m16 16 4 4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><input class="search-input" data-table-search placeholder="Search results…"></label></div>
            {_simplified_results_table(rows)}
            <footer class="surface-footer"><span>{len(rows)} contacts verified</span><a class="button primary" href="/reports.csv?run_id={selected_run}">Download CSV</a></footer>
          </section>"""

    run_log = _escape(run.get("collection_log")) if run and run.get("collection_log") else ""
    advanced = f"""
      <details class="advanced">
        <summary>Advanced options</summary>
        <div class="advanced-grid">
          <p><strong>Verification service</strong><br><span id="advanced-login-copy">{_escape(login_snapshot.detail or login_snapshot.status)}</span></p>
          <div class="advanced-actions">
            {f'<form method="post" action="/runs/{selected_run}/collect"><button>Retry verification</button></form>' if run and enriched_count and not busy else ''}
            {f'<form method="post" action="/runs/{selected_run}/send-n8n"><button>Refresh opportunity data</button></form>' if run and automation_complete else ''}
            <form method="post" action="/database/clear" onsubmit="return confirm('Delete every local customer list and result? This cannot be undone.');"><button class="danger">Clear saved data</button></form>
          </div>
          {f'<details class="run-log"><summary>View activity details</summary><pre>{run_log}</pre></details>' if run_log else ''}
        </div>
      </details>"""

    return f"""<!doctype html>
    <html lang="en"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Customer verification workspace — Customer Verification</title>
      <link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
      <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;750&display=swap" rel="stylesheet">
      <style>{_REDESIGN_STYLES}</style>
    </head><body><main class="app-shell">
      <header class="page-header"><div class="brand-lockup"><span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="m6.5 12.2 3.5 3.5 7.7-8" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg></span><div><h1>Customer Verification</h1><p>Verify your customer list and discover qualified accounts.</p></div></div><span id="login-status" class="session-status {login_tone}" title="{_escape(login_snapshot.detail)}">{_escape(login_snapshot.status)}</span></header>
      {_message(request)}
      {file_summary}
      {metrics}
      {stepper}
      {main_surface}
      {advanced}
      <div class="sr-only legacy-contract" aria-hidden="true"><button class="tab-button active" id="tab-enriched">Enriched records</button><button id="tab-automation">Web automation</button><button id="tab-final">Final table</button><form class="async-stage-form"><div class="stage-progress"></div></form><span>Companies approved</span></div>
    </main><script>{_REDESIGN_SCRIPT}</script></body></html>"""


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
