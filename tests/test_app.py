from starlette.requests import Request

import app as dashboard
from app import _automation_table, _enrichment_table, _final_results_table, _page, _report_card


def _row(resolution_status: str, resolution_error: str = "") -> dict[str, object]:
    return {
        "person_id": 1,
        "run_id": 1,
        "person_name": "Example Person",
        "linkedin_url": "https://www.linkedin.com/in/example",
        "company_name": "Example Company",
        "resolution_status": resolution_status,
        "resolution_error": resolution_error,
        "servicenow_customer": "",
        "n8n_status": "",
        "n8n_response": "",
    }


def test_report_card_shows_needs_approval_on_collapsed_company_summary() -> None:
    html = _report_card(
        _row(
            "apollo_employment_history_fallback",
            "Apollo primary organization was unavailable.",
        )
    )

    summary = html.split("</summary>", 1)[0]
    assert '<span class="company-approval-status">Needs approval</span>' in summary
    assert "Apollo primary organization was unavailable." not in summary
    assert "Apollo primary organization was unavailable." in html


def test_report_card_does_not_show_approval_status_for_trusted_company() -> None:
    html = _report_card(_row("apollo_structurally_verified"))

    summary = html.split("</summary>", 1)[0]
    assert "company-approval-status" not in summary
    assert "Needs approval" not in summary


def test_report_card_links_person_name_to_linkedin_profile() -> None:
    html = _report_card(_row("apollo_structurally_verified"))

    summary = html.split("</summary>", 1)[0]
    assert (
        '<a class="person-name" href="https://www.linkedin.com/in/example" '
        'target="_blank" rel="noreferrer">Example Person</a>'
    ) in summary


def test_report_card_keeps_plain_person_name_without_linkedin_profile() -> None:
    row = _row("apollo_structurally_verified")
    row["linkedin_url"] = ""

    html = _report_card(row)
    summary = html.split("</summary>", 1)[0]

    assert '<span class="person-name">Example Person</span>' in summary
    assert '<a class="person-name"' not in summary


def test_stage_tables_show_distinct_workflow_data() -> None:
    row = _row("apollo_employment_history_fallback", "Company requires review.")
    row.update(
        {
            "check_status": "completed",
            "apollo_company_name": "Example Company",
            "headquarters": "Copenhagen",
            "country": "Denmark",
            "servicenow_customer": "No",
            "servicenow_matched_name": "No result",
            "match_score": "0",
            "checked_at": "2026-09-01T10:00:00Z",
        }
    )

    enriched = _enrichment_table([row])
    automation = _automation_table([row])
    final = _final_results_table([row])

    assert "Resolution" in enriched
    assert "Needs approval" in enriched
    assert "Company requires review." in enriched
    assert "Automation status" in automation
    assert "ServiceNow customer" in automation
    assert "Final status" in final
    assert "n8n delivery" in final


def test_page_renders_progress_steps_and_three_record_tabs(monkeypatch) -> None:
    row = _row("apollo_structurally_verified")
    row.update({"check_status": "apollo_success", "apollo_company_name": "Example Company"})

    class Database:
        def summary(self):
            return [{"id": 7, "status": "enriched", "people_count": 1}]

        def report_rows(self, run_id):
            assert run_id == 7
            return [row]

        def run(self, run_id):
            assert run_id == 7
            return {"id": 7, "status": "enriched", "collection_log": ""}

    monkeypatch.setattr(dashboard, "DATABASE", Database())
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []}
    )

    html = _page(request, 7)

    assert html.count('class="workflow-step ') == 3
    assert 'id="tab-enriched"' in html
    assert 'id="tab-automation"' in html
    assert 'id="tab-final"' in html
    assert "Enriched records" in html
    assert "Web automation" in html
    assert "Final table" in html
