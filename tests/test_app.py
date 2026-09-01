from app import _report_card


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
