import json
from pathlib import Path

from fastapi import BackgroundTasks
from starlette.requests import Request

import app as dashboard
from app import _automation_table, _enrichment_table, _final_results_table, _page, _report_card
from browser.session_monitor import LoginSnapshot


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

    assert "<th>Resolution</th>" not in enriched
    assert "Needs approval" in enriched
    assert "Company requires review." in enriched
    assert "Automation status" in automation
    assert "ServiceNow customer" in automation
    assert "Final status" in final
    assert "n8n delivery" in final


def test_final_table_expands_the_whole_record_and_uses_n8n_citations() -> None:
    row = _row("apollo_structurally_verified")
    row.update(
        {
            "check_status": "completed",
            "servicenow_customer": "No",
            "n8n_status": "received",
            "n8n_response": json.dumps(
                {
                    "result": {
                        "citations": [
                            {
                                "type": "Official ServiceNow Partner",
                                "title": "Example partner profile",
                                "url": "https://www.servicenow.com/partners/example.html",
                            }
                        ]
                    }
                }
            ),
        }
    )

    html = _final_results_table([row])

    assert '<details class="final-record">' in html
    assert "Show record" in html
    assert "n8n research citations" in html
    assert "https://www.servicenow.com/partners/example.html" in html


def test_final_table_prefers_a_screenshot_over_n8n_citations(
    monkeypatch,
) -> None:
    row = _row("apollo_structurally_verified")
    row.update(
        {
            "check_status": "completed",
            "servicenow_customer": "Yes",
            "n8n_status": "received",
            "n8n_response": json.dumps(
                {"result": {"citations": [{"title": "Fallback citation", "url": "https://example.com"}]}}
            ),
        }
    )
    monkeypatch.setattr(dashboard, "_screenshot_path", lambda _row: Path("capture.png"))

    html = _final_results_table([row])

    assert "ServiceNow evidence" in html
    assert 'src="/screenshots/1"' in html
    assert "n8n research citations" not in html


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
    assert "Customer verification workspace" in html
    assert 'id="login-status"' in html
    assert "Waiting for Login" in html
    assert "pollLoginStatus" in html
    assert 'class="overview-stats"' in html
    assert "Companies approved" in html
    assert "prefers-reduced-motion" in html
    assert 'class="async-stage-form"' in html
    assert 'class="stage-progress"' in html
    assert '<meta http-equiv="refresh"' not in html
    enriched_button = html.split('id="tab-enriched"', 1)[0].rsplit("<button", 1)[1]
    assert "tab-button active" in enriched_button


def test_page_shows_logged_in_session_state(monkeypatch) -> None:
    class Database:
        def summary(self):
            return []

    class Monitor:
        snapshot = LoginSnapshot(
            status="Logged In",
            logged_in=True,
            browser_connected=True,
            detail="Authenticated form detected",
            tone="logged-in",
        )

    monkeypatch.setattr(dashboard, "DATABASE", Database())
    monkeypatch.setattr(dashboard, "LOGIN_MONITOR", Monitor())
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []}
    )

    html = _page(request)

    assert 'id="login-status" class="session-status logged-in"' in html
    assert ">Logged In</span>" in html


def test_progress_payload_reports_live_stage_counts(monkeypatch) -> None:
    ready = _row("apollo_structurally_verified")
    ready.update({"person_id": 1, "check_status": "apollo_success"})
    pending = _row("manual_verified")
    pending.update({"person_id": 2, "check_status": "pending"})

    class Database:
        def run(self, run_id):
            assert run_id == 7
            return {"id": 7, "status": "enriching"}

        def report_rows(self, run_id):
            assert run_id == 7
            return [ready, pending]

    monkeypatch.setattr(dashboard, "DATABASE", Database())

    progress = dashboard._run_progress(7)

    assert progress["busy"] is True
    assert progress["enriched"] == 1
    assert progress["target"] == 2
    assert progress["enrichment_percent"] == 50
    assert progress["automation_percent"] == 0


def test_failed_enrichment_finishes_the_stage_and_allows_ready_rows_to_continue(
    monkeypatch,
) -> None:
    ready = _row("manual_verified")
    ready.update({"person_id": 1, "check_status": "apollo_success"})
    failed = _row("manual_verified")
    failed.update({"person_id": 2, "check_status": "apollo_failed"})

    class Database:
        def run(self, _run_id):
            return {"id": 7, "status": "needs_attention"}

        def report_rows(self, _run_id):
            return [ready, failed]

    monkeypatch.setattr(dashboard, "DATABASE", Database())

    progress = dashboard._run_progress(7)

    assert progress["busy"] is False
    assert progress["processed"] == 2
    assert progress["failed_enrichment"] == 1
    assert progress["enrichment_complete"] is True
    assert progress["enrichment_percent"] == 100
    assert progress["automation_target"] == 1
    assert progress["can_automate"] is True


def test_web_automation_can_start_when_some_enriched_rows_are_ready(monkeypatch) -> None:
    updates: list[dict[str, str]] = []

    class Database:
        def run(self, _run_id):
            return {"id": 7, "status": "needs_enrichment"}

        def report_rows(self, _run_id):
            return [{"check_status": "apollo_success"}, {"check_status": "apollo_failed"}]

        def update_run(self, _run_id, **values):
            updates.append(values)

    monkeypatch.setattr(dashboard, "DATABASE", Database())
    tasks = BackgroundTasks()

    response = dashboard.collect(7, tasks)

    assert response.status_code == 303
    assert updates == [{"status": "collecting"}]
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].args[2] is dashboard.LOGIN_MONITOR


def test_open_browser_queues_automation_that_waits_for_login(monkeypatch) -> None:
    updates: list[dict[str, str]] = []
    launches: list[bool] = []

    class Database:
        def run(self, _run_id):
            return {"id": 7, "status": "enriched"}

        def report_rows(self, _run_id):
            return [{"check_status": "apollo_success"}]

        def update_run(self, _run_id, **values):
            updates.append(values)

    monkeypatch.setattr(dashboard, "DATABASE", Database())
    monkeypatch.setattr(dashboard, "launch_chrome", lambda: launches.append(True))
    tasks = BackgroundTasks()

    response = dashboard.open_browser(7, tasks)

    assert response.status_code == 303
    assert launches == [True]
    assert updates == [{"status": "collecting"}]
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].func is dashboard.run_collection
    assert tasks.tasks[0].args == (dashboard.DATABASE, 7, dashboard.LOGIN_MONITOR)
