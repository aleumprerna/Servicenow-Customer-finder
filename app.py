from __future__ import annotations



import csv

import html

import io

import json

import logging

import os

import secrets

from pathlib import Path

from typing import Any



from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from dotenv import load_dotenv

from starlette.middleware.sessions import SessionMiddleware



from utils.filenames import safe_filename


from config import PROJECT_ROOT, load_settings

from microsoft_auth import router as microsoft_auth_router

from services.ai_company_resolver import resolve_company_from_web, resolve_company_headquarters
from services.ai_metrics import pricing_from_settings

from services.servicenow_deep_research import (
    DeepResearchService,
    LLMResearchProvider,
    ServiceNowCustomerPageVerifier,
)

from services.servicenow_deep_research.crawler import (
    BoundedOfficialCrawler,
    UnsafeResearchTarget,
    normalize_domain,
    validate_public_domain,
)

from workflow.database import WorkflowDatabase

from workflow.presentation import parse_n8n_evidence

from workflow.service import (

    TRUSTED_COMPANY_STATUSES,


    parse_people_csv,


    run_enrichment,


)





DATABASE = WorkflowDatabase(PROJECT_ROOT / "data" / "workflow.db")

LOGGER = logging.getLogger(__name__)


load_dotenv(PROJECT_ROOT / ".env")

app = FastAPI(title="ServiceNow Partner Workflow", docs_url=None, redoc_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32),
    same_site="lax",
    https_only=os.getenv("SESSION_HTTPS_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"},
)

app.include_router(microsoft_auth_router)


def _metric_recorder(
    database: WorkflowDatabase, run_id: int, person_id: int | None
) -> Any:
    return lambda metric: database.record_ai_metric(
        run_id=run_id, person_id=person_id, **metric
    )


def _format_metric_duration(milliseconds: Any) -> str:
    seconds = float(milliseconds or 0) / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes}m {remainder:02d}s"


def _format_metric_cost(summary: dict[str, Any]) -> str:
    calls = int(summary.get("call_count") or 0)
    priced = int(summary.get("priced_calls") or 0)
    cost = summary.get("estimated_cost_usd")
    if not calls or not priced or cost is None:
        return "Not configured"
    suffix = "*" if priced < calls else ""
    return f"${float(cost):.6f}{suffix}"


def _format_action_usage(summary: dict[str, Any]) -> str:
    if not int(summary.get("call_count") or 0):
        return ""
    return (
        f'{int(summary.get("total_tokens") or 0):,} tokens · '
        f'{_format_metric_duration(summary.get("model_latency_ms"))} · '
        f'{_format_metric_cost(summary)}'
    )


def _format_call_cost(metric: dict[str, Any]) -> str:
    value = metric.get("estimated_cost_usd")
    return "Not configured" if value is None else f"${float(value):.6f}"


def _person_ai_summary(database: Any, person_id: int, **filters: Any) -> dict[str, Any]:
    loader = getattr(database, "ai_metrics_summary_for_person", None)
    return loader(person_id, **filters) if callable(loader) else {}


def _run_ai_summary(database: Any, run_id: int) -> dict[str, Any]:
    loader = getattr(database, "ai_metrics_summary", None)
    return loader(run_id) if callable(loader) else {}



ENRICHED_CHECK_STATUSES = {"apollo_success", "ai_success", "searching", "completed", "manual_review", "error"}

ENRICHMENT_TERMINAL_STATUSES = ENRICHED_CHECK_STATUSES | {"apollo_failed"}

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


def _workflow_counts(rows: list[dict[str, Any]]) -> tuple[int, int]:

    enriched = sum(

        str(row.get("check_status") or "").casefold() in ENRICHED_CHECK_STATUSES

        for row in rows

    )

    approved = sum(

        str(row.get("resolution_status") or "") in TRUSTED_COMPANY_STATUSES for row in rows

    )

    return enriched, approved





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

        note_parts.append('<p class="detail-alert">The stored research response could not be fully read.</p>')

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

                <div><dt>Research status</dt><dd>{_pretty_status(evidence.delivery_status, "Waiting")}</dd></div>

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


def _usage_info_button(row: dict[str, Any]) -> str:
    person_id = int(row.get("person_id") or 0)
    label = _escape(row.get("person_name") or row.get("company_name") or "record")
    return (
        f'<button type="button" class="usage-info-btn" data-person-id="{person_id}" '
        f'aria-label="View AI usage for {label}" title="View AI usage">i</button>'
    )


def _enrichment_table(rows: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for serial_number, row in enumerate(rows, start=1):
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
        body.append(
            f"""
            <tr class="table-row">
              <td>
                <div class="prospect-profile">
                  <div class="prospect-avatar" aria-label="Serial number {serial_number}">{serial_number}</div>
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


def _final_results_table(rows: list[dict[str, Any]]) -> str:
    records: list[str] = []
    for serial_number, row in enumerate(rows, start=1):
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
                <div class="evidence-title"><span>Research citations</span><small>Market intelligence sources</small></div>
                <ul class="citation-list">{''.join(citation_items)}</ul>
              </div>"""
        else:
            evidence_html = """
              <div class="final-evidence-card evidence-empty">
                <div class="evidence-title"><span>Verification in progress</span><small>The record has not returned a screenshot or research citation yet.</small></div>
              </div>"""

        verification_note = (
            _escape(evidence.evidence_note)
            or _escape(evidence.verification_status)
            or "Record verified with platform intelligence."
        )
        person_name = _escape(row.get("person_name")) or "Unnamed prospect"
        company = _escape(row.get("company_name")) or "Company unresolved"
        records.append(
            f"""
            <details class="final-record">
              <summary class="final-record-summary">
                <div class="final-cell record-person">
                  <div class="prospect-profile">
                    <div class="prospect-avatar" aria-label="Serial number {serial_number}">{serial_number}</div>
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
        <div class="final-record-header" aria-hidden="true"><span>Person &amp; company</span><span>Final status</span><span>ServiceNow app</span><span>Sources</span><span>Research status</span><span></span></div>
        {''.join(records)}
      </div>"""


def _run_progress(run_id: int) -> dict[str, Any]:

    run = DATABASE.run(run_id)

    if not run:

        raise HTTPException(status_code=404, detail="Run not found")

    rows = DATABASE.report_rows(run_id)

    total = len(rows)

    enriched, approved = _workflow_counts(rows)

    target = approved or total

    processed = _enrichment_processed_count(rows)

    failed_enrichment = sum(

        str(row.get("check_status") or "").casefold() == "apollo_failed" for row in rows

    )

    enrichment_complete = approved > 0 and processed >= approved

    busy = run["status"] in {"enriching"}

    enrich_state = "active" if run["status"] == "enriching" or not enrichment_complete else "complete"

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

        "confirmed": confirmed,

        "enrichment_percent": percent(processed, target),

        "enrichment_complete": enrichment_complete,

        "enrich_state": enrich_state,

        "can_enrich": not busy,

        "enrich_label": "Enriching records…" if run["status"] == "enriching" else "Enrich records",

    }





def _friendly_company_review_error(value: Any) -> str:
    detail = " ".join(str(value or "").split())
    lowered = detail.casefold()
    if "proxyerror" in lowered or "unable to connect to proxy" in lowered:
        return "The company data service could not be reached. Check the network or proxy settings, then try again."
    if "timed out" in lowered or "timeout" in lowered:
        return "The company lookup timed out. Try again, or confirm the company manually."
    if "rejected the api key" in lowered or "http 401" in lowered:
        return "The company data service rejected its API key. Check the Apollo configuration, then try again."
    if "lacks access" in lowered or "http 403" in lowered:
        return "The Apollo API key does not have access to this lookup."
    if detail:
        return detail
    return "We could not confidently match this contact to a company."


def _review_companies_table(rows: list[dict[str, Any]]) -> str:
    """Render the client-facing company review table.

    Company-resolution diagnostics remain available only after the user opens a
    row's Review control.
    """

    body: list[str] = []
    for serial_number, row in enumerate(rows, start=1):
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
            raw_reason = str(row.get("resolution_error") or "")
            reason = _escape(_friendly_company_review_error(raw_reason))
            technical_reason = _escape(raw_reason) or reason
            review = f"""
              <details class="row-review">
                <summary class="button row-action">Review</summary>
                <div class="review-panel">
                  <button type="button" class="review-close" aria-label="Close company review">×</button>
                  <strong>Confirm company</strong>
                  <p>{reason}</p>
                  <form class="company-override" method="post" action="/people/{int(row['person_id'])}/company">
                    <input type="hidden" name="run_id" value="{int(row['run_id'])}">
                    <input type="hidden" name="resolution_source" value="">
                    <input type="hidden" name="suggested_company_name" value="">
                    <input type="hidden" name="suggested_company_domain" value="">
                    <input type="hidden" name="suggested_company_linkedin_url" value="">
                    <input type="hidden" name="suggested_headquarters" value="">
                    <input type="hidden" name="suggested_country" value="">
                    <input type="hidden" name="suggested_country_code" value="">
                    <label>Company name
                      <input name="company_name" value="{company if company != 'Not matched yet' else ''}" required placeholder="Enter the correct company">
                    </label>
                    <div class="review-actions">
                      <button type="button" class="button secondary ai-resolve-btn" data-person-id="{int(row['person_id'])}" data-run-id="{int(row['run_id'])}">Suggest company</button>
                      <button type="submit" class="button primary">Confirm and review</button>
                    </div>
                    <div class="ai-status-msg" aria-live="polite"></div>
                  </form>
                  <details class="technical-note"><summary>Matching details</summary><p>{technical_reason}</p></details>
                </div>
              </details>"""
        body.append(
            f"""
            <tr data-review-row data-needs-review="{str(not trusted).lower()}" data-search="{person_name} {headline} {company} {location}">
              <td><div class="contact-cell"><span class="contact-avatar" aria-label="Serial number {serial_number}">{serial_number}</span><span><strong>{_table_person_link(row)}</strong><small>{headline}</small></span></div></td>
              <td><strong>{company}</strong></td>
              <td>{location}</td>
              <td>{status}</td>
              <td><div class="row-action-group">{review}{_usage_info_button(row)}</div></td>
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


def _deep_research_evidence_list(items: Any, empty_copy: str) -> str:
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            items = []
    if not isinstance(items, list) or not items:
        return f'<p class="deep-empty">{_escape(empty_copy)}</p>'
    cards: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        safe_url = url if url.startswith(("https://", "http://")) else ""
        citation_grounded = item.get("citation_grounded") is True
        title = _escape(item.get("page_title") or url or "Source")
        source_tag = str(item.get("source_type") or (
            "Official" if item.get("official_source") else "External"
        ))
        strength = str(item.get("strength") or "weak").capitalize()
        link = (
            f'<a href="{_escape(safe_url)}" target="_blank" rel="noopener noreferrer">{title}</a>'
            if safe_url and citation_grounded
            else f"<strong>{title}</strong>"
        )
        citation_note = (
            ""
            if citation_grounded
            else '<span class="muted">Citation not verified; run research again.</span>'
        )
        reason_html = (
            f'<p><strong>Reason:</strong> {_escape(item.get("reason"))}</p>'
            if item.get("reason")
            else ""
        )
        raw_modules = item.get("modules")
        module_names = (
            [str(module).strip() for module in raw_modules if str(module).strip()]
            if isinstance(raw_modules, list)
            else []
        )
        modules_html = (
            f'<p><strong>ServiceNow modules:</strong> {_escape(", ".join(module_names))}</p>'
            if module_names
            else ""
        )
        cards.append(
            '<li class="deep-source">'
            f'<div>{link}<span class="source-tags"><span>{source_tag}</span><span>{_escape(strength)}</span></span></div>'
            f'<p>{_escape(item.get("evidence") or "No excerpt available.")}</p>'
            f'{modules_html}'
            f'{reason_html}'
            f'{citation_note}'
            '</li>'
        )
    return f'<ul class="deep-source-list">{"".join(cards)}</ul>' if cards else f'<p class="deep-empty">{_escape(empty_copy)}</p>'


def _deep_research_visit_logs(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    urls = list(
        dict.fromkeys(
            str(item).strip()
            for item in value
            if str(item).strip().startswith(("https://", "http://"))
        )
    ) if isinstance(value, list) else []
    entries = "".join(
        f'<li><a href="{_escape(url)}" target="_blank" rel="noopener noreferrer">{_escape(url)}</a></li>'
        for url in urls
    )
    content = (
        f'<p>{len(urls)} website{"s" if len(urls) != 1 else ""} visited</p><ol class="deep-log-list">{entries}</ol>'
        if urls
        else '<p class="deep-empty">No visited websites were recorded for this run.</p>'
    )
    return (
        '<details class="deep-log-details">'
        '<summary class="button deep-log-toggle">View logs</summary>'
        f'<div class="deep-log-content">{content}</div>'
        '</details>'
    )


def _deep_research_cell(
    row: dict[str, Any], action_usage: dict[str, Any] | None = None
) -> str:
    person_id = int(row.get("person_id") or 0)
    request_status = str(row.get("dr_request_status") or "idle")
    classification = str(row.get("dr_classification_status") or "")
    confidence = int(row.get("dr_confidence") or 0)
    domain = str(row.get("company_domain") or "").strip()
    company_name = str(row.get("company_name") or "").strip()
    labels = {
        "CONFIRMED_CUSTOMER": ("Confirmed customer", "success"),
        "LIKELY_CUSTOMER": ("Likely customer", "success"),
        "INCONCLUSIVE": ("Inconclusive", "warning"),
        "NO_OFFICIAL_EVIDENCE": ("Not verified", "neutral"),
        "PARTNER_ONLY": ("Partner evidence only", "info"),
    }
    label, tone = labels.get(classification, ("Not researched", "neutral"))
    is_running = request_status == "running"
    has_result = bool(classification and row.get("dr_researched_at"))
    button_label = "Researching..." if is_running else "Run again" if has_result else "Deep Research"
    disabled = " disabled" if is_running or not company_name else ""
    title = "" if domain else ' title="Official domain will be found with AI before research"'
    force = "true" if has_result else "false"
    button = (
        f'<button type="button" class="deep-research-btn" data-person-id="{person_id}" '
        f'data-force="{force}"{disabled}{title}>{button_label}</button>'
    )
    progress = (
        '<span class="deep-progress" aria-live="polite"><span class="mini-spinner"></span>'
        '<span data-research-message>Searching official sources...</span></span>'
        if is_running
        else '<span class="deep-progress" aria-live="polite" hidden><span class="mini-spinner"></span><span data-research-message></span></span>'
    )
    error = str(row.get("dr_last_error") or "")
    error_html = f'<small class="deep-error">{_escape(error)}</small>' if request_status == "failed" and error else ""
    usage_html = (
        f'<small class="ai-action-usage">AI usage: {_escape(_format_action_usage(action_usage or {}))}</small>'
        if action_usage and int(action_usage.get("call_count") or 0)
        else ""
    )
    if not has_result:
        return f'<div class="deep-research-cell">{button}{progress}{usage_html}{error_html}</div>'

    researched_at = str(row.get("dr_researched_at") or "").replace("T", " ").replace("+00:00", " UTC")
    customer_page_status = row.get("dr_servicenow_customer_page_found")
    customer_page_found = customer_page_status == 1
    customer_page_url = str(row.get("dr_servicenow_customer_page_url") or "")
    customer_page_value = "Yes" if customer_page_found else "No" if customer_page_status == 0 else "Check unavailable"
    customer_page_html = (
        f'<a href="{_escape(customer_page_url)}" target="_blank" rel="noopener noreferrer">'
        'Yes — open official story</a>'
        if customer_page_found and customer_page_url.startswith("https://")
        else customer_page_value
    )
    customer_sources = _deep_research_evidence_list(
        row.get("dr_customer_evidence"), "No end-customer evidence was retained."
    )
    partner_sources = _deep_research_evidence_list(
        row.get("dr_partner_evidence"), "No partner-only evidence was found."
    )
    ambiguous_sources = _deep_research_evidence_list(
        row.get("dr_ambiguous_evidence"), "No ambiguous references were retained."
    )
    visit_logs = _deep_research_visit_logs(row.get("dr_visited_urls"))
    details = f"""
      <details class="row-evidence deep-details">
        <summary class="deep-view">View research</summary>
        <div class="evidence-panel deep-panel">
          <button type="button" class="deep-close" aria-label="Close research evidence">×</button>
          <h3>{_escape(row.get('company_name') or 'Company')} research</h3>
          <div class="deep-panel-summary">{_status_pill(label, tone)} <strong>{confidence}% confidence</strong></div>
          <p>{_escape(row.get('dr_summary') or '')}</p>
          <dl class="evidence-facts">
            <div><dt>Sources checked</dt><dd>{int(row.get('dr_sources_checked') or 0)}</dd></div>
            <div><dt>Relevant sources</dt><dd>{int(row.get('dr_relevant_sources') or 0)}</dd></div>
            <div><dt>Official domain</dt><dd>{_escape(row.get('company_domain') or 'Not available')}</dd></div>
            <div><dt>ServiceNow customer page</dt><dd>{customer_page_html}</dd></div>
            <div><dt>Last researched</dt><dd>{_escape(researched_at)}</dd></div>
            <div class="deep-log-field"><dt>Research logs</dt><dd>{visit_logs}</dd></div>
          </dl>
          <section><h4>Customer evidence</h4>{customer_sources}</section>
          <section><h4>Partner evidence</h4>{partner_sources}</section>
          <section><h4>Ambiguous references</h4>{ambiguous_sources}</section>
        </div>
      </details>"""
    return (
        '<div class="deep-research-cell">'
        f'{_status_pill(label, tone)}'
        f'<small>{confidence}% confidence · {int(row.get("dr_relevant_sources") or 0)} sources</small>'
        f'<small>Last researched: {_escape(researched_at.split(" ", 1)[0])}</small>'
        f'{details}{button}{progress}{usage_html}{error_html}'
        '</div>'
    )


def _simplified_results_table(rows: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for serial_number, row in enumerate(rows, start=1):
        evidence = parse_n8n_evidence(
            str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")
        )
        relationship = _relationship(row, evidence)
        relationship_lower = relationship.casefold()
        portal_customer_raw = str(row.get("servicenow_customer") or "").casefold()
        story_check = row.get("dr_servicenow_customer_page_found")
        customer_raw = (
            "yes"
            if portal_customer_raw == "yes" or story_check == 1
            else "no"
            if portal_customer_raw == "no"
            else portal_customer_raw
        )
        customer_label = "Yes" if customer_raw == "yes" else "No" if customer_raw == "no" else "Needs review" if customer_raw == "unknown" else "Pending"
        customer_tone = "success" if customer_raw == "yes" else "warning" if customer_raw == "unknown" else "neutral"
        story_url = str(row.get("dr_servicenow_customer_page_url") or "")
        customer_story_link = (
            f'<small><a href="{_escape(story_url)}" target="_blank" rel="noopener noreferrer">View official page</a></small>'
            if story_check == 1 and story_url.startswith("https://")
            else ""
        )
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
            <tr data-search="{person_name} {headline} {company} {location} {customer_label} {partner_label} {opportunity_label}" data-customer-status="{customer_raw}">
              <td class="bulk-select-cell">{f'<input type="checkbox" class="bulk-research-checkbox" data-person-id="{int(row.get("person_id") or 0)}" aria-label="Select {company} for Deep Research">' if customer_raw == 'no' else ''}</td>
              <td><div class="contact-cell"><span class="contact-avatar" aria-label="Serial number {serial_number}">{serial_number}</span><span><strong>{_table_person_link(row)}</strong><small>{headline}</small></span></div></td>
              <td><strong>{company}</strong><small>{location}</small></td>
              <td>{_status_pill(customer_label, customer_tone)}{customer_story_link}</td>
              <td>{_status_pill(partner_label, 'info') if partner_label != '—' else '<span class="no-action">—</span>'}</td>
              <td>{_status_pill(opportunity_label, 'success') if opportunity_label != '—' else '<span class="no-action">—</span>'}</td>
              <td>{_deep_research_cell(row, row.get('_deep_research_usage'))}</td>
              <td><div class="row-action-group"><details class="row-evidence"><summary class="button row-action">View evidence</summary><div class="evidence-panel"><h3>{company}</h3>{evidence_html}</div></details>{_usage_info_button(row)}</div></td>
            </tr>"""
        )
    if not body:
        body.append('<tr><td class="table-empty" colspan="8">Results will appear here when verification is complete.</td></tr>')
    return f"""
      <div class="table-wrap">
        <table class="review-table results-table">
          <thead><tr><th><span class="sr-only">Bulk selection</span></th><th>Contact</th><th>Company</th><th>ServiceNow customer</th><th>Partner</th><th>Opportunity</th><th>Deep Research</th><th><span class="sr-only">Action</span></th></tr></thead>
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
  .result-filter-actions,.bulk-research-controls { display:flex; align-items:center; gap:8px; }
  .bulk-research-controls[hidden] { display:none; }
  .bulk-select-label { display:flex; align-items:center; gap:7px; color:var(--muted); font-size:.875rem; font-weight:600; white-space:nowrap; }
  .bulk-research-checkbox,[data-bulk-select-all] { width:18px; height:18px; accent-color:var(--blue); cursor:pointer; }
  .bulk-select-cell { min-width:48px !important; width:48px; padding-right:0 !important; }
  .workflow-notice { margin:16px 26px 0; padding:11px 13px; border:1px solid #fed7aa; border-radius:8px; background:var(--amber-soft); color:#92400e; font-size:.875rem; }
  .workflow-notice.error { border-color:#fecaca; background:#fef2f2; color:var(--red); }
  .table-wrap { overflow:auto; }
  .review-table { width:100%; border-collapse:collapse; }
  .review-table th { padding:11px 18px; border-bottom:1px solid var(--border-strong); background:var(--surface-soft); color:var(--muted); font-size:.75rem; font-weight:700; letter-spacing:.025em; text-align:left; white-space:nowrap; }
  .review-table td { min-width:120px; height:70px; padding:12px 18px; border-bottom:1px solid var(--border); color:#334155; vertical-align:middle; }
  .review-table tr:last-child td { border-bottom:0; }
  .review-table tbody tr:hover { background:#fbfdff; }
  .contact-cell { min-width:230px; display:flex; align-items:center; gap:12px; }
  .contact-avatar { width:34px; height:34px; display:grid; place-items:center; border-radius:9px; background:var(--blue-soft); color:var(--blue); font-size:.75rem; font-weight:750; flex:0 0 auto; }
  .contact-cell strong,.contact-cell small,.results-table td:nth-child(3) strong,.results-table td:nth-child(3) small { display:block; }
  .contact-cell a { color:var(--ink); text-decoration:none; }
  .contact-cell a:hover { color:var(--blue); text-decoration:underline; }
  .contact-cell small,.results-table td:nth-child(3) small { max-width:320px; margin-top:3px; color:var(--muted); font-size:.75rem; line-height:1.35; }
  .status-pill { display:inline-flex; align-items:center; min-height:28px; padding:3px 9px; border-radius:999px; background:#f1f5f9; color:#475569; font-size:.75rem; font-weight:700; white-space:nowrap; }
  .status-pill.success { background:var(--green-soft); color:var(--green); }
  .status-pill.warning { background:var(--amber-soft); color:var(--amber); }
  .status-pill.info { background:var(--blue-soft); color:var(--blue-dark); }
  .no-action { color:var(--subtle); }
  .row-review,.row-evidence { position:relative; }
  .row-review summary,.row-evidence summary { list-style:none; }
  .row-review summary::-webkit-details-marker,.row-evidence summary::-webkit-details-marker { display:none; }
  .row-action { min-height:36px; padding:0 12px; color:var(--blue); font-size:.8125rem; }
  .row-action-group { display:flex; align-items:center; gap:8px; }
  .usage-info-btn { width:32px; height:32px; min-height:32px; padding:0; border:1px solid #bfdbfe; border-radius:50%; background:var(--blue-soft); color:var(--blue-dark); font-family:Georgia,serif; font-size:.9rem; font-weight:700; }
  .usage-info-btn:hover { border-color:#60a5fa; background:#dbeafe; }
  .review-panel,.evidence-panel { position:absolute; z-index:20; top:43px; right:0; width:min(390px,calc(100vw - 48px)); padding:18px; border:1px solid var(--border-strong); border-radius:12px; background:#fff; box-shadow:0 18px 45px rgba(15,23,42,.18); }
  .row-review .review-panel { position:fixed; z-index:100; top:50%; left:50%; right:auto; width:min(460px,calc(100vw - 48px)); max-height:80vh; overflow:auto; transform:translate(-50%,-50%); }
  .review-close { position:absolute; top:10px; right:10px; min-width:34px; min-height:34px; padding:0; border-color:transparent; background:transparent; color:var(--muted); font-size:1.35rem; }
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
  .deep-research-cell { min-width:174px; display:flex; flex-direction:column; align-items:flex-start; gap:6px; }
  .deep-research-cell > small { color:var(--muted); font-size:.7rem; }
  .deep-research-btn { min-height:34px; padding:0 11px; border-color:#bfdbfe; background:var(--blue-soft); color:var(--blue-dark); font-size:.75rem; }
  .deep-research-btn:hover { border-color:#60a5fa; background:#dbeafe; }
  .deep-progress { display:flex; align-items:center; gap:6px; max-width:190px; color:var(--muted); font-size:.7rem; line-height:1.3; }
  .mini-spinner { width:13px; height:13px; flex:0 0 auto; border:2px solid #bfdbfe; border-top-color:var(--blue); border-radius:50%; animation:spin .9s linear infinite; }
  .deep-error { max-width:210px; color:var(--red)!important; line-height:1.35; }
  .deep-view { color:var(--blue); cursor:pointer; font-size:.75rem; font-weight:700; }
  .deep-view:hover { text-decoration:underline; }
  .deep-panel { position:fixed!important; z-index:100; top:50%!important; left:50%!important; right:auto!important; width:min(620px,calc(100vw - 48px))!important; max-height:min(680px,78vh); overflow:auto; transform:translate(-50%,-50%); }
  .deep-close { position:absolute; top:10px; right:10px; min-width:34px; min-height:34px; padding:0; border-color:transparent; background:transparent; color:var(--muted); font-size:1.35rem; }
  .deep-panel-summary { display:flex; align-items:center; gap:10px; margin:8px 0; color:#334155; font-size:.8125rem; }
  .deep-panel section { margin-top:16px; padding-top:14px; border-top:1px solid var(--border); }
  .deep-panel h4 { margin:0 0 8px; color:#334155; font-size:.8125rem; }
  .evidence-facts .deep-log-field { grid-column:1 / -1; }
  .deep-log-details { margin-top:6px; }
  .deep-log-details summary { list-style:none; }
  .deep-log-details summary::-webkit-details-marker { display:none; }
  .deep-log-toggle { min-height:34px; padding:0 11px; color:var(--blue); font-size:.75rem; }
  .deep-log-content { max-height:230px; margin-top:8px; overflow:auto; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:var(--surface-soft); }
  .deep-log-content p { margin:0 0 7px; }
  .deep-log-list { display:grid; gap:6px; margin:0; padding-left:20px; }
  .deep-log-list a { color:var(--blue-dark); font-size:.75rem; overflow-wrap:anywhere; }
  .deep-source-list { display:grid; gap:8px; margin:0; padding:0; list-style:none; }
  .deep-source { padding:10px; border:1px solid var(--border); border-radius:8px; background:var(--surface-soft); }
  .deep-source > div { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
  .deep-source a,.deep-source strong { color:var(--blue-dark); font-size:.75rem; font-weight:700; overflow-wrap:anywhere; }
  .deep-source p { margin:7px 0 0; color:#475569; font-size:.75rem; line-height:1.45; }
  .source-tags { display:flex; gap:4px; flex:0 0 auto; }
  .source-tags span { padding:2px 5px; border-radius:999px; background:#e2e8f0; color:#475569; font-size:.625rem; font-weight:700; }
  .deep-empty { margin:0!important; color:var(--muted); font-size:.75rem!important; }
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
  .ai-run-monitor { grid-column:1 / -1; padding-top:14px; border-top:1px solid var(--border); }
  .ai-run-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:12px; }
  .ai-run-heading h3 { margin-bottom:2px; color:var(--ink); }
  .ai-run-heading p { color:var(--muted); }
  .ai-run-heading .button { min-height:34px; padding:0 10px; font-size:.75rem; }
  .ai-metric-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:8px; margin-bottom:14px; }
  .ai-metric-grid > div { min-width:0; padding:11px; border:1px solid var(--border); border-radius:8px; background:var(--surface-soft); }
  .ai-metric-grid span,.ai-metric-grid strong { display:block; }
  .ai-metric-grid span { margin-bottom:4px; color:var(--muted); font-size:.68rem; font-weight:700; text-transform:uppercase; }
  .ai-metric-grid strong { overflow:hidden; color:var(--ink); font-size:.95rem; text-overflow:ellipsis; }
  .ai-log-table { width:100%; border-collapse:collapse; margin-top:10px; }
  .ai-log-table th,.ai-log-table td { padding:8px 10px; border-bottom:1px solid var(--border); text-align:left; white-space:nowrap; }
  .usage-dialog { width:min(720px,calc(100vw - 32px)); max-height:80vh; padding:0; border:1px solid var(--border-strong); border-radius:14px; background:#fff; color:var(--ink); box-shadow:0 24px 70px rgba(15,23,42,.25); }
  .usage-dialog::backdrop { background:rgba(15,23,42,.45); }
  .usage-dialog-header { display:flex; justify-content:space-between; gap:18px; padding:18px 20px 14px; border-bottom:1px solid var(--border); }
  .usage-dialog-header h3 { margin-bottom:2px; }
  .usage-dialog-header p { margin:0; color:var(--muted); font-size:.8125rem; }
  .usage-dialog-close { width:34px; height:34px; min-height:34px; padding:0; border:0; background:transparent; color:var(--muted); font-size:1.35rem; }
  [data-usage-content] { padding:18px 20px 22px; overflow:auto; }
  .record-usage-summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-bottom:16px; }
  .record-usage-summary > div { padding:10px; border:1px solid var(--border); border-radius:8px; background:var(--surface-soft); }
  .record-usage-summary span,.record-usage-summary strong { display:block; }
  .record-usage-summary span { color:var(--muted); font-size:.68rem; font-weight:700; text-transform:uppercase; }
  .record-usage-summary strong { margin-top:3px; }
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
    .ai-metric-grid,.record-usage-summary { grid-template-columns:repeat(2,1fr); }
    .review-panel,.evidence-panel { position:fixed; top:50%; left:50%; right:auto; transform:translate(-50%,-50%); max-height:80vh; overflow:auto; }
  }
  @media (prefers-reduced-motion:reduce) { *,*::before,*::after { animation:none!important; transition:none!important; scroll-behavior:auto!important; } }
"""


_REDESIGN_SCRIPT = r"""
  const workflowProgress = document.getElementById('workflow-progress');
  let progressTimer = null;
  let workflowWasBusy = workflowProgress?.dataset.busy === 'true';

  function setText(selector, value) {
    const element = document.querySelector(selector);
    if (element) element.textContent = value;
  }

  const numberFormat = new Intl.NumberFormat();
  function formatDuration(milliseconds) {
    const seconds = Number(milliseconds || 0) / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const rounded = Math.round(seconds);
    return `${Math.floor(rounded / 60)}m ${String(rounded % 60).padStart(2, '0')}s`;
  }
  function formatCost(summary) {
    const calls = Number(summary?.call_count || 0);
    const priced = Number(summary?.priced_calls || 0);
    if (!calls || !priced || summary?.estimated_cost_usd == null) return 'Not configured';
    return `$${Number(summary.estimated_cost_usd).toFixed(6)}${priced < calls ? '*' : ''}`;
  }
  function formatActionUsage(summary) {
    if (!Number(summary?.call_count || 0)) return '';
    return `${numberFormat.format(Number(summary.total_tokens || 0))} tokens · ${formatDuration(summary.model_latency_ms)} · ${formatCost(summary)}`;
  }
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[character]));
  }
  function applyRunUsage(summary, calls) {
    if (!summary) return;
    const values = {
      calls:numberFormat.format(Number(summary.call_count || 0)),
      input:numberFormat.format(Number(summary.input_tokens || 0)),
      output:numberFormat.format(Number(summary.output_tokens || 0)),
      tokens:numberFormat.format(Number(summary.total_tokens || 0)),
      latency:formatDuration(summary.model_latency_ms),
      cost:formatCost(summary),
    };
    Object.entries(values).forEach(([key, value]) => {
      document.querySelectorAll(`[data-ai-total="${key}"], [data-ai-top="${key}"]`).forEach(element => { element.textContent = value; });
    });
    if (Array.isArray(calls)) {
      const body = document.querySelector('[data-ai-call-log]');
      if (body) body.innerHTML = calls.map(call => `<tr><td>${escapeHtml(call.person_name || call.company_name || 'Run-level')}</td><td>${escapeHtml(call.operation || 'AI call')}</td><td>${escapeHtml(`${call.provider || ''}:${call.model || ''}`)}</td><td>${numberFormat.format(Number(call.total_tokens || 0))}</td><td>${formatDuration(call.latency_ms)}</td><td>${call.estimated_cost_usd == null ? 'Not configured' : `$${Number(call.estimated_cost_usd).toFixed(6)}`}</td><td>${escapeHtml(call.status || '')}</td></tr>`).join('');
    }
  }
  async function refreshRunUsage(runId) {
    if (!runId) return;
    try {
      const response = await fetch(`/api/runs/${runId}/ai-metrics`, {cache:'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      applyRunUsage(data.summary, data.calls);
    } catch (_error) {}
  }

  document.querySelector('[data-refresh-ai-usage]')?.addEventListener('click', event => {
    refreshRunUsage(event.currentTarget.closest('[data-ai-run-monitor]')?.dataset.runId);
  });

  const usageDialog = document.querySelector('[data-usage-dialog]');
  document.querySelector('[data-usage-close]')?.addEventListener('click', () => usageDialog?.close());
  usageDialog?.addEventListener('click', event => { if (event.target === usageDialog) usageDialog.close(); });
  document.addEventListener('click', async event => {
    const button = event.target.closest('.usage-info-btn');
    if (!button || !usageDialog) return;
    const content = usageDialog.querySelector('[data-usage-content]');
    const title = usageDialog.querySelector('[data-usage-title]');
    if (content) content.innerHTML = '<p class="muted">Loading usage…</p>';
    usageDialog.showModal();
    try {
      const response = await fetch(`/api/people/${button.dataset.personId}/ai-metrics`, {cache:'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not load usage.');
      if (title) title.textContent = `${data.person.person_name || data.person.company_name || 'Record'} · AI usage`;
      const summary = data.summary || {};
      const rows = (data.calls || []).map(call => `<tr><td>${escapeHtml(call.operation)}</td><td>${escapeHtml(`${call.provider}:${call.model}`)}</td><td>${numberFormat.format(Number(call.input_tokens || 0))}</td><td>${numberFormat.format(Number(call.output_tokens || 0))}</td><td>${numberFormat.format(Number(call.total_tokens || 0))}</td><td>${formatDuration(call.latency_ms)}</td><td>${call.estimated_cost_usd == null ? 'Not configured' : `$${Number(call.estimated_cost_usd).toFixed(6)}`}</td></tr>`).join('');
      if (content) content.innerHTML = `<div class="record-usage-summary"><div><span>Calls</span><strong>${numberFormat.format(Number(summary.call_count || 0))}</strong></div><div><span>Total tokens</span><strong>${numberFormat.format(Number(summary.total_tokens || 0))}</strong></div><div><span>Model latency</span><strong>${formatDuration(summary.model_latency_ms)}</strong></div><div><span>Estimated cost</span><strong>${formatCost(summary)}</strong></div></div>${rows ? `<div class="table-scroll"><table class="ai-log-table"><thead><tr><th>Operation</th><th>Model</th><th>Input</th><th>Output</th><th>Total</th><th>Latency</th><th>Cost</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="muted">No AI usage has been recorded for this record yet.</p>'}`;
      applyRunUsage(data.run_summary);
    } catch (error) {
      if (content) content.innerHTML = `<p class="deep-error">${escapeHtml(error.message)}</p>`;
    }
  });

  function applyProgress(data) {
    const value = data.processed;
    const total = data.target;
    const percent = data.enrichment_percent;
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
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button');
      if (!button || button.disabled) return;
      const originalLabel = button.textContent;
      button.disabled = true;
      button.textContent = 'Matching companies…';
      workflowWasBusy = true;
      try {
        const response = await fetch(form.action, {method:'POST', redirect:'follow'});
        if (!response.ok) throw new Error('Company review could not be started.');
        window.location.reload();
      } catch (_error) {
        workflowWasBusy = false;
        button.disabled = false;
        button.textContent = originalLabel || 'Review Companies';
        window.alert('Company review could not be started. Please try again.');
      }
    });
  });

  const searchInput = document.querySelector('[data-table-search]');
  const reviewFilter = document.querySelector('[data-review-filter]');
  const customerFilters = [...document.querySelectorAll('[data-customer-filter]')];
  const bulkControls = document.querySelector('[data-bulk-research-controls]');
  const bulkSelectAll = document.querySelector('[data-bulk-select-all]');
  const bulkResearchButton = document.querySelector('[data-bulk-deep-research]');
  let needsReviewOnly = false;
  let customerStatusFilter = '';
  function updateBulkSelection() {
    const visibleNoBoxes = [...document.querySelectorAll('tr[data-customer-status="no"]:not([hidden]) .bulk-research-checkbox')];
    const selected = visibleNoBoxes.filter(box => box.checked).length;
    if (bulkSelectAll) {
      bulkSelectAll.checked = visibleNoBoxes.length > 0 && selected === visibleNoBoxes.length;
      bulkSelectAll.indeterminate = selected > 0 && selected < visibleNoBoxes.length;
    }
    if (bulkResearchButton) {
      bulkResearchButton.disabled = selected === 0;
      bulkResearchButton.textContent = `Deep Research selected (${selected})`;
    }
  }
  function filterRows() {
    const query = (searchInput?.value || '').trim().toLowerCase();
    document.querySelectorAll('tbody tr[data-search]').forEach(row => {
      const matchesQuery = !query || row.dataset.search.toLowerCase().includes(query);
      const matchesReview = !needsReviewOnly || row.dataset.needsReview === 'true';
      const matchesCustomer = !customerStatusFilter || row.dataset.customerStatus === customerStatusFilter;
      row.hidden = !(matchesQuery && matchesReview && matchesCustomer);
    });
    if (bulkControls) bulkControls.hidden = customerStatusFilter !== 'no';
    updateBulkSelection();
  }
  searchInput?.addEventListener('input', filterRows);
  reviewFilter?.addEventListener('click', () => {
    needsReviewOnly = !needsReviewOnly;
    reviewFilter.classList.toggle('active', needsReviewOnly);
    reviewFilter.setAttribute('aria-pressed', String(needsReviewOnly));
    filterRows();
  });
  customerFilters.forEach(filter => filter.addEventListener('click', () => {
    const requested = filter.dataset.customerFilter;
    customerStatusFilter = customerStatusFilter === requested ? '' : requested;
    customerFilters.forEach(item => {
      const active = item.dataset.customerFilter === customerStatusFilter;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    filterRows();
  }));
  bulkSelectAll?.addEventListener('change', () => {
    document.querySelectorAll('tr[data-customer-status="no"]:not([hidden]) .bulk-research-checkbox').forEach(box => {
      box.checked = bulkSelectAll.checked;
    });
    updateBulkSelection();
  });
  document.addEventListener('change', event => {
    if (event.target.matches('.bulk-research-checkbox')) updateBulkSelection();
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
      const params = new URLSearchParams({run_id:button.dataset.runId,auto_approve:'false'});
      const response = await fetch(`/api/people/${button.dataset.personId}/ai-resolve-company`, {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:params});
      const data = await response.json();
      if (!response.ok || !data.success) {
        const usageText = formatActionUsage(data.usage);
        await refreshRunUsage(button.dataset.runId);
        throw new Error(`${data.error || 'Could not suggest a company.'}${usageText ? ` AI usage: ${usageText}.` : ''}`);
      }
      if (input) {
        input.value = data.company_name;
        input.dataset.suggestedCompany = data.company_name;
      }
      const suggestionFields = {
        resolution_source:'ai_suggestion',
        suggested_company_name:data.company_name || '',
        suggested_company_domain:data.company_domain || '',
        suggested_company_linkedin_url:data.company_linkedin_url || '',
        suggested_headquarters:data.headquarters || '',
        suggested_country:data.country || '',
        suggested_country_code:data.country_code || '',
      };
      Object.entries(suggestionFields).forEach(([name, value]) => {
        const field = form?.querySelector(`[name="${name}"]`);
        if (field) field.value = value;
      });
      const submitButton = form?.querySelector('button[type="submit"]');
      if (submitButton) submitButton.textContent = 'Confirm AI company';
      if (statusEl) {
        const locationText = data.location ? ` · Headquarters: ${data.location}` : '';
        const usageText = formatActionUsage(data.usage);
        statusEl.textContent = `Suggested: ${data.company_name}${locationText}. ${usageText ? `AI usage: ${usageText}. ` : ''}Press Confirm AI company to submit.`;
        statusEl.className = 'ai-status-msg success';
      }
      applyRunUsage(data.run_usage);
      await refreshRunUsage(button.dataset.runId);
      button.disabled = false;
      button.textContent = 'Suggest again';
    } catch (error) {
      button.disabled = false;
      button.textContent = 'Suggest company';
      if (statusEl) { statusEl.textContent = error.message; statusEl.className = 'ai-status-msg error'; }
    }
  });

  document.querySelectorAll('.company-override input[name="company_name"]').forEach(input => {
    input.addEventListener('input', () => {
      if (!input.dataset.suggestedCompany || input.value.trim() === input.dataset.suggestedCompany) return;
      const form = input.closest('form');
      form?.querySelectorAll('input[name^="suggested_"], input[name="resolution_source"]').forEach(field => {
        field.value = '';
      });
      const submitButton = form?.querySelector('button[type="submit"]');
      if (submitButton) submitButton.textContent = 'Confirm and review';
      delete input.dataset.suggestedCompany;
    });
  });

  const researchMessages = [
    'Searching official sources...',
    'Checking job postings and company pages...',
    'Separating customer evidence from partner evidence...',
    'Cross-checking external sources...',
    'Preparing the evidence summary...'
  ];

  async function pollDeepResearch(personId, cell, button, messageTimer) {
    try {
      const response = await fetch(`/api/people/${personId}/deep-research`, {cache:'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not read research progress.');
      const status = data.research?.request_status || 'idle';
      applyRunUsage(data.run_usage);
      if (status === 'completed') {
        window.clearInterval(messageTimer);
        if (data.cell_html) cell.outerHTML = data.cell_html;
        applyRunUsage(data.run_usage);
        await refreshRunUsage(data.run_usage?.run_id);
        updateBulkSelection();
        return;
      }
      if (status === 'failed') {
        applyRunUsage(data.run_usage);
        await refreshRunUsage(data.run_usage?.run_id);
        const usageText = formatActionUsage(data.usage);
        throw new Error(`${data.research?.last_error || 'Deep Research could not be completed.'}${usageText ? ` AI usage: ${usageText}.` : ''}`);
      }
      window.setTimeout(() => pollDeepResearch(personId, cell, button, messageTimer), 1500);
    } catch (error) {
      window.clearInterval(messageTimer);
      button.disabled = false;
      button.textContent = button.dataset.force === 'true' ? 'Run again' : 'Deep Research';
      const progress = cell.querySelector('.deep-progress');
      if (progress) { progress.hidden = false; progress.textContent = error.message; progress.classList.add('deep-error'); }
    }
  }

  document.addEventListener('click', async event => {
    const bulkButton = event.target.closest('[data-bulk-deep-research]');
    if (bulkButton && !bulkButton.disabled) {
      const buttons = [...document.querySelectorAll('tr[data-customer-status="no"] .bulk-research-checkbox:checked')]
        .map(checkbox => checkbox.closest('tr')?.querySelector('.deep-research-btn:not(:disabled)'))
        .filter(Boolean);
      if (!buttons.length) return;
      const noun = buttons.length === 1 ? 'record' : 'records';
      if (!window.confirm(`Run Deep Research for ${buttons.length} ServiceNow non-customer ${noun}?`)) return;
      bulkButton.disabled = true;
      for (let index = 0; index < buttons.length; index += 1) {
        bulkButton.textContent = `Starting ${index + 1} of ${buttons.length}...`;
        buttons[index].click();
        await new Promise(resolve => window.setTimeout(resolve, 120));
      }
      bulkButton.textContent = 'Deep Research started';
      return;
    }
    const button = event.target.closest('.deep-research-btn');
    if (!button || button.disabled) return;
    const cell = button.closest('.deep-research-cell');
    const progress = cell?.querySelector('.deep-progress');
    const message = cell?.querySelector('[data-research-message]');
    button.disabled = true;
    button.textContent = 'Researching...';
    if (progress) { progress.hidden = false; progress.classList.remove('deep-error'); }
    let messageIndex = 0;
    if (message) message.textContent = researchMessages[0];
    const messageTimer = window.setInterval(() => {
      messageIndex = (messageIndex + 1) % researchMessages.length;
      if (message) message.textContent = researchMessages[messageIndex];
    }, 2600);
    try {
      const body = new URLSearchParams({force:button.dataset.force || 'false', research_depth:'deep'});
      const response = await fetch(`/api/people/${button.dataset.personId}/deep-research`, {
        method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body
      });
      const data = await response.json();
      if (!response.ok && response.status !== 409) throw new Error(data.error || 'Deep Research could not start.');
      pollDeepResearch(button.dataset.personId, cell, button, messageTimer);
    } catch (error) {
      window.clearInterval(messageTimer);
      button.disabled = false;
      button.textContent = button.dataset.force === 'true' ? 'Run again' : 'Deep Research';
      if (progress) { progress.hidden = false; progress.textContent = error.message; progress.classList.add('deep-error'); }
    }
  });

  document.addEventListener('click', event => {
    const close = event.target.closest('.deep-close, .review-close');
    if (close) close.closest('details')?.removeAttribute('open');
  });

  if (workflowWasBusy) pollProgress();
"""


def _page(request: Request, selected_run: int | None = None) -> str:
    """Render the simplified, Stitch-guided customer verification experience."""

    summaries = DATABASE.summary()
    if selected_run is None and summaries:
        selected_run = int(summaries[0]["id"])
    rows = DATABASE.report_rows(selected_run) if selected_run else []
    run = DATABASE.run(selected_run) if selected_run else None
    metrics_loader = getattr(DATABASE, "ai_metrics_summary", None)
    ai_summary = metrics_loader(selected_run) if selected_run and callable(metrics_loader) else {}
    calls_loader = getattr(DATABASE, "ai_metrics", None)
    ai_calls = calls_loader(selected_run) if selected_run and callable(calls_loader) else []
    person_metrics_loader = getattr(DATABASE, "ai_metrics_summary_for_person", None)
    if callable(person_metrics_loader):
        for row in rows:
            if row.get("dr_started_at"):
                row["_deep_research_usage"] = person_metrics_loader(
                    int(row.get("person_id") or 0),
                    operation_prefix="deep_research.",
                    since=str(row.get("dr_started_at") or ""),
                )

    relationships = [
        _relationship(row, parse_n8n_evidence(str(row.get("n8n_status") or ""), str(row.get("n8n_response") or "")))
        for row in rows
    ]
    verified_count = sum(str(row.get("servicenow_customer") or "").casefold() == "yes" for row in rows)
    non_customer_count = sum(str(row.get("servicenow_customer") or "").casefold() == "no" for row in rows)
    partner_count = sum("partner" in relationship.casefold() for relationship in relationships)
    opportunity_count = sum(_relationship_tone(relationship) == "positive" for relationship in relationships)
    enriched_count, approved_count = _workflow_counts(rows)
    processed_count = _enrichment_processed_count(rows)
    enrichment_target = approved_count or len(rows)
    enrichment_complete = approved_count > 0 and processed_count >= approved_count
    busy = bool(run and run["status"] in {"enriching"})

    if not run:
        current_stage = "upload"
    elif run["status"] == "enriching" or not enrichment_complete or request.query_params.get("view") == "review":
        current_stage = "review"
    else:
        current_stage = "results"

    stage_order = {"upload": 0, "review": 1, "results": 2}
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
            <div class="metric-card"><strong class="metric-value" data-ai-top="calls">{int(ai_summary.get('call_count') or 0):,}</strong><span class="metric-label">AI Calls</span></div>
            <div class="metric-card"><strong class="metric-value" data-ai-top="tokens">{int(ai_summary.get('total_tokens') or 0):,}</strong><span class="metric-label">AI Tokens</span></div>
            <div class="metric-card"><strong class="metric-value" data-ai-top="latency">{_format_metric_duration(ai_summary.get('model_latency_ms'))}</strong><span class="metric-label">Total Model Latency</span></div>
            <div class="metric-card"><strong class="metric-value" data-ai-top="cost">{_format_metric_cost(ai_summary)}</strong><span class="metric-label">Estimated AI Cost</span></div>
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
        <div class="workflow-step {step_state(2)}"><span class="step-dot">3</span><span class="step-label">Results</span></div>
      </nav>"""

    review_notice = ""
    if run and run.get("status") in {"needs_attention", "failed"}:
        errors = [str(row.get("resolution_error") or "") for row in rows]
        network_error = any(
            "proxyerror" in error.casefold() or "unable to connect to proxy" in error.casefold()
            for error in errors
        )
        if network_error:
            notice_copy = "Company matching could not reach Apollo. Check the network or proxy settings, then try again."
        elif run.get("status") == "failed":
            notice_copy = "Company matching stopped before it finished. Try again or open Advanced options for details."
        else:
            notice_copy = "Company matching finished, but some records still need attention. Open Review to confirm them."
        notice_tone = " error" if run.get("status") == "failed" or network_error else ""
        review_notice = f'<div class="workflow-notice{notice_tone}" role="status">{_escape(notice_copy)}</div>'

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
            {review_notice}
            <div class="table-tools"><label class="search-wrap"><span class="sr-only">Search contacts or companies</span><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/><path d="m16 16 4 4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><input class="search-input" data-table-search placeholder="Search contacts or companies…"></label><button type="button" class="filter-button" data-review-filter aria-pressed="false">Needs review</button></div>
            {_review_companies_table(rows)}
            <footer class="surface-footer"><span>Showing {len(rows)} contacts</span><form class="async-stage-form" data-stage="enrich" method="post" action="/runs/{selected_run}/enrich"><button class="primary">Review Companies</button></form></footer>
          </section>"""
    else:
        main_surface = f"""
          <section class="work-surface">
            <div class="result-intro"><span class="complete-icon" aria-hidden="true">✓</span><div><h2>Company enrichment complete</h2><p>Your results are ready to review and share.</p></div></div>
            <header class="surface-header"><div><h2>Results</h2><p>Customer, partner, and opportunity status for every contact.</p></div><div class="surface-actions"><a class="button primary" href="/reports.csv?run_id={selected_run}">Download CSV</a><a class="button" href="/?run_id={selected_run}&amp;view=review">Review companies</a></div></header>
            <div class="table-tools"><label class="search-wrap"><span class="sr-only">Search results</span><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/><path d="m16 16 4 4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><input class="search-input" data-table-search placeholder="Search results…"></label><div class="result-filter-actions"><button type="button" class="filter-button" data-customer-filter="yes" aria-pressed="false">Customer: Yes ({verified_count})</button><button type="button" class="filter-button" data-customer-filter="no" aria-pressed="false">Customer: No ({non_customer_count})</button><div class="bulk-research-controls" data-bulk-research-controls hidden><label class="bulk-select-label"><input type="checkbox" data-bulk-select-all> Select all</label><button type="button" data-bulk-deep-research disabled>Deep Research selected (0)</button></div></div></div>
            {_simplified_results_table(rows)}
            <footer class="surface-footer"><span>{len(rows)} contacts</span><a class="button primary" href="/reports.csv?run_id={selected_run}">Download CSV</a></footer>
          </section>"""

    run_log = _escape(run.get("collection_log")) if run and run.get("collection_log") else ""
    ai_call_rows = "".join(
        "<tr>"
        f"<td>{_escape(item.get('person_name') or item.get('company_name') or 'Run-level')}</td>"
        f"<td>{_escape(item.get('operation') or 'AI call')}</td>"
        f"<td>{_escape(item.get('provider'))}:{_escape(item.get('model'))}</td>"
        f"<td>{int(item.get('total_tokens') or 0):,}</td>"
        f"<td>{_format_metric_duration(item.get('latency_ms'))}</td>"
        f"<td>{_format_call_cost(item)}</td>"
        f"<td>{_escape(item.get('status') or '')}</td>"
        "</tr>"
        for item in ai_calls
    )
    ai_details = f"""
      <section class="ai-run-monitor" data-ai-run-monitor data-run-id="{selected_run or ''}">
        <div class="ai-run-heading"><div><h3>AI usage for this dataset</h3><p>Totals include company suggestions, headquarters lookup, and Deep Research.</p></div><button type="button" class="button" data-refresh-ai-usage>Refresh</button></div>
        <div class="ai-metric-grid">
          <div><span>Calls</span><strong data-ai-total="calls">{int(ai_summary.get('call_count') or 0):,}</strong></div>
          <div><span>Input tokens</span><strong data-ai-total="input">{int(ai_summary.get('input_tokens') or 0):,}</strong></div>
          <div><span>Output tokens</span><strong data-ai-total="output">{int(ai_summary.get('output_tokens') or 0):,}</strong></div>
          <div><span>Total tokens</span><strong data-ai-total="tokens">{int(ai_summary.get('total_tokens') or 0):,}</strong></div>
          <div><span>Model latency</span><strong data-ai-total="latency">{_format_metric_duration(ai_summary.get('model_latency_ms'))}</strong></div>
          <div><span>Estimated cost</span><strong data-ai-total="cost">{_format_metric_cost(ai_summary)}</strong></div>
        </div>
        <details class="run-log"><summary>View AI call logs</summary>
          <div class="table-scroll"><table class="ai-log-table"><thead><tr><th>Record</th><th>Operation</th><th>Model</th><th>Tokens</th><th>Latency</th><th>Cost</th><th>Status</th></tr></thead><tbody data-ai-call-log>{ai_call_rows}</tbody></table></div>
          <p class="muted">Cost uses the configured token and web-search rates. “Not configured” means pricing rates are missing.</p>
        </details>
      </section>"""
    advanced = f"""
      <details class="advanced">
        <summary>Advanced options and logs</summary>
        <div class="advanced-grid">
          <div class="advanced-actions">
            <form method="post" action="/database/clear" onsubmit="return confirm('Delete every local customer list and result? This cannot be undone.');"><button class="danger">Clear saved data</button></form>
          </div>
          {f'<details class="run-log"><summary>View activity details</summary><pre>{run_log}</pre></details>' if run_log else ''}
          {ai_details}
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
      <header class="page-header"><div class="brand-lockup"><span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="m6.5 12.2 3.5 3.5 7.7-8" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg></span><div><h1>Customer Verification</h1><p>Verify your customer list and discover qualified accounts.</p></div></div></header>
      {_message(request)}
      {file_summary}
      {metrics}
      {stepper}
      {main_surface}
      {advanced}
      <dialog class="usage-dialog" data-usage-dialog>
        <div class="usage-dialog-header"><div><h3 data-usage-title>Record AI usage</h3><p>Token, latency, and cost details for this record.</p></div><button type="button" class="usage-dialog-close" data-usage-close aria-label="Close">×</button></div>
        <div data-usage-content><p class="muted">Loading usage…</p></div>
      </dialog>
    </main><script>{_REDESIGN_SCRIPT}</script></body></html>"""


@app.get("/", response_class=HTMLResponse)

def home(request: Request, run_id: int | None = None) -> HTMLResponse:

    return HTMLResponse(_page(request, run_id))





@app.post("/runs")

async def upload_csv(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
) -> RedirectResponse:

    if not (file.filename or "").lower().endswith(".csv"):

        return RedirectResponse(url="/?kind=error&message=Please+upload+a+CSV+file", status_code=303)

    try:

        people = parse_people_csv(await file.read())

        run_id = DATABASE.create_run(file.filename or "uploaded.csv", people)

        DATABASE.update_run(run_id, status="enriching")

        background_tasks.add_task(run_enrichment, DATABASE, run_id)

    except ValueError as exc:

        return RedirectResponse(url=f"/?kind=error&message={str(exc).replace(' ', '+')}", status_code=303)

    return RedirectResponse(
        url=f"/?run_id={run_id}&message=CSV+uploaded.+Company+review+started",
        status_code=303,
    )





@app.post("/database/clear")

def clear_database() -> RedirectResponse:

    DATABASE.clear_all()

    return RedirectResponse(url="/?message=Local+workflow+database+cleared", status_code=303)





@app.post("/people/{person_id}/company")

def set_company_override(

    person_id: int,

    background_tasks: BackgroundTasks,

    company_name: str = Form(...),

    run_id: int = Form(...),

    resolution_source: str = Form(""),

    suggested_company_name: str = Form(""),

    suggested_company_domain: str = Form(""),

    suggested_company_linkedin_url: str = Form(""),

    suggested_headquarters: str = Form(""),

    suggested_country: str = Form(""),

    suggested_country_code: str = Form(""),

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

    is_ai_suggestion = (
        resolution_source == "ai_suggestion"
        and " ".join(suggested_company_name.split()).casefold() == company.casefold()
        and bool(suggested_headquarters.strip())
        and bool(suggested_country.strip())
        and bool(suggested_country_code.strip())
    )

    DATABASE.update_person_resolution(

        person_id,

        company_name=company,

        status="manual_verified",

        error="",

        domain=suggested_company_domain.strip() if is_ai_suggestion else "",

        company_linkedin_url=suggested_company_linkedin_url.strip() if is_ai_suggestion else "",

    )

    DATABASE.reset_check_for_company_change(
        person_id,
        run_id,
        company,
        headquarters=suggested_headquarters.strip() if is_ai_suggestion else "",
        country=suggested_country.strip() if is_ai_suggestion else "",
        country_code=suggested_country_code.strip().upper() if is_ai_suggestion else "",
        check_status="ai_success" if is_ai_suggestion else "pending",
    )

    if is_ai_suggestion:

        DATABASE.update_run(run_id, status="enriched")

        return RedirectResponse(

            url=f"/?run_id={run_id}&message=AI+company+saved+without+Apollo+re-enrichment",

            status_code=303,

        )

    DATABASE.update_run(run_id, status="enriching")

    background_tasks.add_task(run_enrichment, DATABASE, run_id)

    return RedirectResponse(

        url=f"/?run_id={run_id}&message=Company+saved.+Review+started",

        status_code=303,

    )





@app.post("/api/people/{person_id}/ai-resolve-company")

def ai_resolve_company(

    person_id: int,

    run_id: int | None = Form(None),

    auto_approve: bool = Form(False),

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

        require_headquarters=True,

        require_grounding=True,
        metrics_callback=_metric_recorder(DATABASE, actual_run_id, person_id),

    )
    action_usage = _person_ai_summary(
        DATABASE,
        person_id, operation_prefix="company_resolution", latest_only=True
    )
    run_usage = _run_ai_summary(DATABASE, actual_run_id)



    if not result.get("success") or not result.get("company_name"):

        return JSONResponse(

            status_code=422,

            content={

                "success": False,

                "error": result.get("error") or "Could not determine company name via web search.",
                "usage": action_usage,
                "run_usage": run_usage,

            },

        )



    company = " ".join(str(result["company_name"]).split())
    headquarters = " ".join(str(result.get("headquarters") or "").split())
    country = " ".join(str(result.get("country") or "").split())
    country_code = str(result.get("country_code") or "").strip().upper()
    company_domain = str(result.get("company_domain") or "").strip()
    company_linkedin_url = str(result.get("company_linkedin_url") or "").strip()



    if auto_approve and actual_run_id:

        DATABASE.update_person_resolution(

            person_id,

            company_name=company,

            status="manual_verified",

            error="",

            domain=company_domain,

            company_linkedin_url=company_linkedin_url,

        )

        DATABASE.reset_check_for_company_change(
            person_id,
            actual_run_id,
            company,
            headquarters=headquarters,
            country=country,
            country_code=country_code,
        )

        DATABASE.update_run(actual_run_id, status="needs_enrichment")

        return JSONResponse(

            content={

                "success": True,

                "company_name": company,

                "company_domain": company_domain,

                "company_linkedin_url": company_linkedin_url,

                "headquarters": headquarters,

                "country": country,

                "country_code": country_code,

                "location": ", ".join(item for item in (headquarters, country) if item),

                "approved": True,

                "confidence": result.get("confidence", "medium"),

                "reason": result.get("reason", ""),

                "source_urls": result.get("source_urls", []),

                "message": f"Resolved and approved company: {company}",
                "usage": action_usage,
                "run_usage": run_usage,

            }

        )



    return JSONResponse(

        content={

            "success": True,

            "company_name": company,

            "company_domain": company_domain,

            "company_linkedin_url": company_linkedin_url,

            "headquarters": headquarters,

            "country": country,

            "country_code": country_code,

            "location": ", ".join(item for item in (headquarters, country) if item),

            "approved": False,

            "confidence": result.get("confidence", "medium"),

            "reason": result.get("reason", ""),

            "source_urls": result.get("source_urls", []),
            "usage": action_usage,
            "run_usage": run_usage,

        }

    )





def _run_deep_research_task(database: WorkflowDatabase, person_id: int, settings: Any) -> None:
    person = database.person(person_id)
    if not person:
        return
    rows = database.report_rows(int(person["run_id"]))
    row = next((item for item in rows if int(item["person_id"]) == person_id), person)
    context = {
        "servicenow_customer": row.get("servicenow_customer"),
        "servicenow_matched_name": row.get("servicenow_matched_name"),
        "match_score": row.get("match_score"),
        "check_status": row.get("check_status"),
        "headline": row.get("headline"),
        "headquarters": row.get("headquarters"),
        "country": row.get("country"),
        "apollo_company_name": row.get("apollo_company_name"),
        "company_linkedin_url": row.get("company_linkedin_url"),
    }
    try:
        raw_input = json.loads(str(person.get("raw_input") or "{}"))
    except (TypeError, json.JSONDecodeError):
        raw_input = {}
    if isinstance(raw_input, dict) and "technographic_servicenow" in raw_input:
        context["technographic_servicenow"] = raw_input.get("technographic_servicenow")
    try:
        provider = LLMResearchProvider(
            str(settings.llm_api_key or ""),
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            provider_name=settings.llm_provider,
            supports_hosted_web_search=settings.llm_supports_hosted_web_search,
            reasoning_effort=settings.llm_reasoning_effort,
            timeout_seconds=settings.deep_research_request_timeout_seconds,
            max_retries=settings.gemini_max_retries,
            retry_base_seconds=settings.gemini_retry_base_seconds,
            metrics_callback=_metric_recorder(
                database, int(person["run_id"]), person_id
            ),
            pricing=pricing_from_settings(settings),
        )
        crawler = BoundedOfficialCrawler(
            max_pages=settings.deep_research_max_pages,
            page_timeout_seconds=settings.deep_research_page_timeout_seconds,
            max_content_chars=settings.deep_research_max_content_chars,
            max_elapsed_seconds=min(60.0, settings.deep_research_request_timeout_seconds),
        )
        customer_page_verifier = ServiceNowCustomerPageVerifier(
            timeout_seconds=min(20.0, settings.deep_research_page_timeout_seconds)
        )
        result = DeepResearchService(
            provider=provider,
            crawler=crawler,
            customer_page_verifier=customer_page_verifier,
        ).research(
            company_name=str(person.get("company_name") or ""),
            official_domain=str(person.get("company_domain") or ""),
            existing_context=context,
            research_depth="deep",
        )
        database.complete_deep_research(person_id, result.as_storage_values())
    except Exception as exc:
        LOGGER.exception("Deep research failed for person_id=%s", person_id)
        database.fail_deep_research(person_id, str(exc) or "Deep Research could not be completed.")


@app.get("/api/people/{person_id}/deep-research")
def get_deep_research(person_id: int) -> JSONResponse:
    person = DATABASE.person(person_id)
    if not person:
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": "Person record not found."},
        )
    research = DATABASE.deep_research(person_id) or {"request_status": "idle"}
    action_usage = _person_ai_summary(
        DATABASE,
        person_id,
        operation_prefix="deep_research.",
        since=str(research.get("started_at") or ""),
    )
    record_usage = _person_ai_summary(DATABASE, person_id)
    run_usage = _run_ai_summary(DATABASE, int(person["run_id"]))
    cell_html = ""
    if research.get("request_status") == "completed":
        row = next(
            (
                item
                for item in DATABASE.report_rows(int(person["run_id"]))
                if int(item.get("person_id") or 0) == person_id
            ),
            None,
        )
        if row:
            cell_html = _deep_research_cell(row, action_usage)
    return JSONResponse(
        content={
            "success": True,
            "research": research,
            "cell_html": cell_html,
            "usage": action_usage,
            "record_usage": record_usage,
            "run_usage": run_usage,
        }
    )


@app.post("/api/people/{person_id}/deep-research")
def start_deep_research(
    person_id: int,
    background_tasks: BackgroundTasks,
    force: bool = Form(False),
    research_depth: str = Form("deep"),
) -> JSONResponse:
    person = DATABASE.person(person_id)
    if not person:
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": "Person record not found."},
        )
    if research_depth != "deep":
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Only deep research is supported."},
        )
    company_name = str(person.get("company_name") or "").strip()
    official_domain = str(person.get("company_domain") or "").strip()
    if not company_name:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Resolve the company before running Deep Research.",
            },
        )
    existing = DATABASE.deep_research(person_id)
    if existing and existing.get("request_status") == "running":
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": "Deep Research is already running.", "research": existing},
        )

    settings = load_settings()
    if not settings.llm_api_key:
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": (
                    "Add KIE_API_KEY to enable Deep Research."
                    if settings.llm_provider == "kie"
                    else "Add GEMINI_API_KEY to enable Deep Research."
                    if settings.llm_provider == "gemini"
                    else "Add GLM_KEY to enable Deep Research."
                    if settings.llm_provider == "glm"
                    else "Add OPENAI_API_KEY to enable Deep Research."
                ),
            },
        )
    domain_error = ""
    if official_domain:
        try:
            official_domain = normalize_domain(official_domain)
            validate_public_domain(official_domain)
        except UnsafeResearchTarget as exc:
            domain_error = str(exc)
            official_domain = ""
    if not official_domain:
        domain_result = resolve_company_headquarters(
            company_name,
            settings.llm_api_key,
            company_domain=str(person.get("company_domain") or ""),
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            provider=settings.llm_provider,
            metrics_callback=_metric_recorder(
                DATABASE, int(person["run_id"]), person_id
            ),
        )
        candidate_domain = str(domain_result.get("company_domain") or "").strip()
        try:
            official_domain = normalize_domain(candidate_domain)
            validate_public_domain(official_domain)
        except UnsafeResearchTarget as exc:
            reason = str(domain_result.get("error") or exc or domain_error)
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "error": f"AI could not verify a usable official company domain: {reason}",
                },
            )
        DATABASE.update_person_resolution(
            person_id,
            company_name=company_name,
            status=str(person.get("resolution_status") or "manual_verified"),
            error=str(person.get("resolution_error") or ""),
            domain=official_domain,
            company_linkedin_url=str(person.get("company_linkedin_url") or ""),
        )
        location_values = {
            "company_name": company_name,
            "headquarters": str(domain_result.get("headquarters") or "").strip(),
            "country": str(domain_result.get("country") or "").strip(),
            "country_code": str(domain_result.get("country_code") or "").strip(),
        }
        if any(location_values[key] for key in ("headquarters", "country", "country_code")):
            DATABASE.upsert_check(
                person_id, int(person["run_id"]), location_values
            )
    if not force and DATABASE.deep_research_is_fresh(person_id, settings.deep_research_cache_days):
        return JSONResponse(
            content={"success": True, "cached": True, "research": DATABASE.deep_research(person_id)},
        )

    claimed = DATABASE.claim_deep_research(
        person_id=person_id,
        run_id=int(person["run_id"]),
        company_name=company_name,
        official_domain=official_domain,
        research_depth=research_depth,
        model_provider=f"{settings.llm_provider}:{settings.llm_model}+deterministic-rules",
    )
    if not claimed:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "error": "Deep Research is already running.",
                "research": DATABASE.deep_research(person_id),
            },
        )
    background_tasks.add_task(_run_deep_research_task, DATABASE, person_id, settings)
    return JSONResponse(
        status_code=202,
        content={"success": True, "cached": False, "research": DATABASE.deep_research(person_id)},
    )


@app.post("/runs/{run_id}/enrich")

def enrich(run_id: int, background_tasks: BackgroundTasks) -> RedirectResponse:

    run = DATABASE.run(run_id)

    if not run:

        raise HTTPException(status_code=404, detail="Run not found")

    if run["status"] in {"enriching"}:

        return RedirectResponse(

            url=f"/?run_id={run_id}&message=This+run+is+already+busy", status_code=303

        )

    DATABASE.update_run(run_id, status="enriching")

    background_tasks.add_task(run_enrichment, DATABASE, run_id)

    return RedirectResponse(url=f"/?run_id={run_id}&message=Enrichment+started", status_code=303)





@app.get("/api/reports")

def reports_api(run_id: int | None = None) -> list[dict[str, Any]]:

    return DATABASE.report_rows(run_id)





@app.get("/api/runs/{run_id}/progress")

def run_progress(run_id: int) -> dict[str, Any]:

    """Small polling payload used by the dashboard without reloading the page."""



    return _run_progress(run_id)


@app.get("/api/runs/{run_id}/ai-metrics")
def run_ai_metrics(run_id: int) -> dict[str, Any]:
    if not DATABASE.run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "summary": DATABASE.ai_metrics_summary(run_id),
        "calls": DATABASE.ai_metrics(run_id),
    }


@app.get("/api/people/{person_id}/ai-metrics")
def person_ai_metrics(person_id: int) -> dict[str, Any]:
    person = DATABASE.person(person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person record not found")
    return {
        "person": {
            "id": person_id,
            "person_name": person.get("person_name") or "",
            "company_name": person.get("company_name") or "",
        },
        "summary": DATABASE.ai_metrics_summary_for_person(person_id),
        "calls": DATABASE.ai_metrics_for_person(person_id),
        "run_summary": DATABASE.ai_metrics_summary(int(person["run_id"])),
    }





@app.get("/api/runs/{run_id}/workspace")

def run_workspace(run_id: int) -> dict[str, str]:

    if not DATABASE.run(run_id):

        raise HTTPException(status_code=404, detail="Run not found")

    rows = DATABASE.report_rows(run_id)

    return {

        "enriched": _enrichment_table(rows),


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





