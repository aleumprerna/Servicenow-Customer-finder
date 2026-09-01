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
    TRUSTED_COMPANY_STATUSES,
    launch_chrome,
    parse_people_csv,
    run_collection,
    run_enrichment,
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
    if str(row.get("resolution_status") or "") not in TRUSTED_COMPANY_STATUSES:
        parts.append(
            f'<form class="company-override" method="post" '
            f'action="/people/{int(row["person_id"])}/company">'
            f'<input type="hidden" name="run_id" value="{int(row["run_id"])}">'
            '<input name="company_name" required placeholder="Correct company name">'
            '<button>Use company</button></form>'
        )
    return "".join(parts)


def _pretty_status(value: Any, fallback: str = "Not available") -> str:
    text = str(value or "").strip()
    return text.replace("_", " ").replace("-", " ").title() if text else fallback


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
          <span class="expand-label"><span class="show-more">View details</span><span class="show-less">Close</span><span class="chevron">⌄</span></span>
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
                  <input name="company_name" required placeholder="Correct company name">
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
              <td>{_pretty_status(row.get('resolution_status'), 'Waiting')}</td>
              <td>{location}</td>
            </tr>"""
        )
    if not body:
        body.append('<tr><td class="table-empty" colspan="5">Upload a CSV to see enriched records.</td></tr>')
    return f"""
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Person</th><th>Resolved company</th><th>Approval</th><th>Resolution</th><th>Company location</th></tr></thead>
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
    body: list[str] = []
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
        body.append(
            f"""
            <tr>
              <td>{_table_person_link(row)}<span class="cell-secondary">{_escape(row.get('company_name')) or 'Company unresolved'}</span>{approval}</td>
              <td>{_status_pill(relationship, relationship_tone)}</td>
              <td>{_pretty_status(row.get('servicenow_customer'), 'Not checked')}</td>
              <td><div class="source-tags">{sources}</div></td>
              <td>{_status_pill(evidence.delivery_status or 'Waiting', 'info' if evidence.delivery_status else 'neutral')}</td>
              <td><details class="table-evidence"><summary>View evidence</summary>{_n8n_cell(row)}</details></td>
            </tr>"""
        )
    if not body:
        body.append('<tr><td class="table-empty" colspan="6">Final results will appear after processing.</td></tr>')
    return f"""
      <div class="table-scroll"><table class="data-table final-table">
        <thead><tr><th>Person &amp; company</th><th>Final status</th><th>ServiceNow app</th><th>Sources</th><th>n8n delivery</th><th>Details</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table></div>"""


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
        str(row.get("check_status") or "").casefold() in {"error", "manual_review"}
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
    enriched_count = sum(row.get("check_status") is not None for row in rows)
    automation_count = sum(
        str(row.get("check_status") or "").casefold() in {"completed", "manual_review", "error"}
        for row in rows
    )
    approved_count = sum(
        str(row.get("resolution_status") or "") in TRUSTED_COMPANY_STATUSES for row in rows
    )

    workflow_steps = '<div class="empty-state"><strong>No active run</strong><span>Upload a CSV to start the workflow.</span></div>'
    run_log = ""
    if run:
        busy = run["status"] in {"enriching", "collecting"}
        enrichment_complete = enriched_count > 0
        automation_complete = automation_count > 0
        final_complete = run["status"] == "completed"
        step_one_state = "active" if run["status"] == "enriching" or not enrichment_complete else "complete"
        step_two_state = (
            "active" if run["status"] == "collecting" or (enrichment_complete and not automation_complete)
            else "complete" if automation_complete else "locked"
        )
        step_three_state = "complete" if final_complete else "active" if automation_complete else "locked"
        enrich_disabled = "disabled" if busy else ""
        automation_disabled = "disabled" if busy or not enrichment_complete else ""
        final_disabled = "disabled" if busy or not automation_complete else ""
        enrich_label = "Enriching records…" if run["status"] == "enriching" else "Enrich records"
        automation_label = "Automation running…" if run["status"] == "collecting" else "Start web automation"
        download_action = (
            f'<a class="button primary-link" href="/reports.csv?run_id={selected_run}">Download CSV</a>'
            if automation_complete
            else '<span class="button disabled-link" aria-disabled="true">Download CSV</span>'
        )
        workflow_steps = f"""
          <div class="workflow-progress">
            <article class="workflow-step {step_one_state}">
              <div class="step-top"><span class="step-number">{'✓' if enrichment_complete else '1'}</span><span class="step-state">{approved_count}/{len(rows)} approved</span></div>
              <h3>Enrich records</h3>
              <p>Resolve each LinkedIn profile and enrich its company with Apollo.</p>
              <div class="step-metric"><strong>{enriched_count}</strong><span>of {len(rows)} records enriched</span></div>
              <form method="post" action="/runs/{selected_run}/enrich"><button class="primary step-action" {enrich_disabled}>{enrich_label}</button></form>
            </article>
            <article class="workflow-step {step_two_state}">
              <div class="step-top"><span class="step-number">{'✓' if automation_complete else '2'}</span><span class="step-state">{automation_count}/{len(rows)} checked</span></div>
              <h3>Run web automation</h3>
              <p>Open ServiceNow, sign in, then start the browser checks using saved enrichment.</p>
              <div class="step-actions">
                <form method="post" action="/runs/{selected_run}/launch-browser"><button {automation_disabled}>Open ServiceNow</button></form>
                <form method="post" action="/runs/{selected_run}/collect"><button class="primary" {automation_disabled}>{automation_label}</button></form>
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

    default_tab = "final" if automation_count else "automation" if enriched_count else "enriched"
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
          <span>Enriched records</span><strong>{enriched_count}/{len(rows)}</strong>
        </button>
        <button type="button" class="tab-button {'active' if automation_active else ''}" role="tab" aria-selected="{str(automation_active).lower()}" aria-controls="panel-automation" id="tab-automation" data-tab="automation">
          <span>Web automation</span><strong>{automation_count}/{len(rows)}</strong>
        </button>
        <button type="button" class="tab-button {'active' if final_active else ''}" role="tab" aria-selected="{str(final_active).lower()}" aria-controls="panel-final" id="tab-final" data-tab="final">
          <span>Final table</span><strong>{confirmed_count}/{len(rows)}</strong>
        </button>
      </div>
      <div class="tab-panel" id="panel-enriched" role="tabpanel" aria-labelledby="tab-enriched" {'hidden' if not enriched_active else ''}>
        <div class="panel-heading"><div><h3>Enriched records</h3><p>Identity, Apollo company resolution, approval status, and organization data.</p></div></div>
        {_enrichment_table(rows)}
      </div>
      <div class="tab-panel" id="panel-automation" role="tabpanel" aria-labelledby="tab-automation" {'hidden' if not automation_active else ''}>
        <div class="panel-heading"><div><h3>Web automation</h3><p>ServiceNow browser-check progress, matches, confidence, and screenshots.</p></div></div>
        {_automation_table(rows)}
      </div>
      <div class="tab-panel" id="panel-final" role="tabpanel" aria-labelledby="tab-final" {'hidden' if not final_active else ''}>
        <div class="panel-heading"><div><h3>Final results</h3><p>Combined ServiceNow result, verification sources, and n8n delivery status.</p></div>{final_export}</div>
        {report_stats}
        {_final_results_table(rows)}
      </div>"""

    refresh = (
        '<meta http-equiv="refresh" content="12">'
        if run and run["status"] in {"enriching", "collecting"}
        else ""
    )
    return f"""<!doctype html>
    <html><head><meta charset="utf-8">{refresh}<title>ServiceNow Partner Workflow</title>
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
      pre {{ white-space:pre-wrap; max-height:300px; overflow:auto; background:#111827; color:#e5e7eb; padding:12px; border-radius:6px; }}
      .page-header {{ display:flex; align-items:flex-end; justify-content:space-between; gap:24px; margin-bottom:20px; }} .page-header p {{ margin:0; }}
      .upload-section {{ display:flex; align-items:center; justify-content:space-between; gap:24px; padding:18px 22px; }} .upload-copy h2 {{ font-size:17px; margin-bottom:4px; }} .upload-copy p {{ margin:0; max-width:760px; font-size:13px; }} .upload-section form {{ display:flex; align-items:center; margin:0; }}
      .section-heading {{ display:flex; align-items:center; justify-content:space-between; gap:18px; margin-bottom:20px; }} .section-heading p {{ margin:4px 0 0; }} .run-picker {{ display:flex; align-items:center; gap:9px; margin:0; }} .run-picker label {{ color:#68758a; font-size:12px; font-weight:700; }}
      .run-status {{ display:inline-flex; border-radius:999px; background:#edf3fb; color:#315b8e; padding:6px 10px; font-size:12px; font-weight:750; }}
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
      .table-review, .table-evidence {{ margin-top:7px; }} .table-review > summary, .table-evidence > summary {{ width:max-content; cursor:pointer; color:#1769c2; font-size:11px; font-weight:700; }} .table-review p {{ max-width:280px; margin:8px 0; color:#8a5a0d; font-size:11px; }} .table-review .company-override {{ flex-direction:column; align-items:flex-start; }} .table-review .company-override input {{ width:220px; min-width:0; }} .table-empty {{ padding:36px !important; color:#758196; text-align:center; }} .final-table .source-tags {{ min-width:180px; }}
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
    </style></head><body>
      <header class="page-header"><div><h1>ServiceNow Partner Workflow</h1><p class="muted">Move from LinkedIn profiles to verified ServiceNow results in three clear stages.</p></div></header>
      {_message(request)}
      <section class="upload-section"><div class="upload-copy"><h2>Upload people CSV</h2>
        <p class="muted">Required: person name and LinkedIn URL. Apollo data alone is used for automatic company resolution.</p></div>
        <form method="post" action="/runs" enctype="multipart/form-data"><input type="file" name="file" accept=".csv,text/csv" required><button class="primary">Upload CSV</button></form>
      </section>
      <section class="workflow-section">
        <div class="section-heading"><div><h2>Workflow progress</h2><p class="muted">Complete each stage from left to right.</p></div>
          <form class="run-picker" method="get" action="/"><label for="run-select">Current run</label><select id="run-select" name="run_id" onchange="this.form.submit()">{options}</select>{f'<span class="run-status">{_pretty_status(run["status"])}</span>' if run else ''}</form>
        </div>
        {workflow_steps}
        {run_log}
      </section>
      <section class="records-workspace">
        <div class="records-heading"><h2>Records</h2><p class="muted">Switch views to inspect the data produced at each workflow stage.</p></div>
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
        error="Company supplied through dashboard override",
    )
    DATABASE.update_run(run_id, status="needs_enrichment")
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
    if run["status"] in {"uploaded", "needs_enrichment"}:
        return RedirectResponse(
            url=f"/?run_id={run_id}&kind=error&message=Click+Enrich+records+first",
            status_code=303,
        )
    DATABASE.update_run(run_id, status="collecting")
    background_tasks.add_task(run_collection, DATABASE, run_id)
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
