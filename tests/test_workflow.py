from pathlib import Path

from clients.openai_websearch import CompanyResearch, OpenAIResearchNoMatchError
from workflow.database import WorkflowDatabase
from workflow.person_company import PersonCompanyResolver
from workflow.service import build_pipeline_csv, parse_people_csv, sync_pipeline_results


def test_uploaded_linkedin_export_is_normalized() -> None:
    people = parse_people_csv(
        b"S.No,Name,Headline,Profile URL\n1,Ada Lovelace,Engineer at Example Corp,https://linkedin.com/in/ada\n"
    )
    assert people == [
        {
            "person_name": "Ada Lovelace",
            "linkedin_url": "https://linkedin.com/in/ada",
            "company_name": "",
            "headline": "Engineer at Example Corp",
            "raw_input": {
                "S.No": "1",
                "Name": "Ada Lovelace",
                "Headline": "Engineer at Example Corp",
                "Profile URL": "https://linkedin.com/in/ada",
            },
        }
    ]


class FakeResearch:
    def person_company(
        self, *, person_name: str, linkedin_url: str, headline: str, supplied_company_name: str
    ) -> CompanyResearch:
        assert linkedin_url == "https://linkedin.com/in/ada"
        assert person_name == "Ada"
        return CompanyResearch(
            company_name="OpenAI Organization",
            country="United States",
            country_code="US",
            headquarters="San Francisco, California, United States",
            domain="openai.com",
            linkedin_url="https://linkedin.com/company/openai",
            confidence="high",
            evidence="Verified from web sources.",
        )


class NoMatchResearch:
    def person_company(
        self, *, person_name: str, linkedin_url: str, headline: str, supplied_company_name: str
    ) -> CompanyResearch:
        raise OpenAIResearchNoMatchError("No current employer could be verified")


def test_company_resolver_uses_verified_openai_web_search_result() -> None:
    resolver = PersonCompanyResolver(FakeResearch())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ada", linkedin_url="https://linkedin.com/in/ada",
        supplied_company_name="Provided Ltd", headline="Engineer at OpenAI Organization",
    )
    assert result.company_name == "OpenAI Organization"
    assert result.status == "openai_verified"
    assert result.domain == "openai.com"
    assert result.company_linkedin_url == "https://linkedin.com/company/openai"
    assert result.country_code == "US"


def test_pipeline_csv_sync_and_negative_filter(tmp_path: Path) -> None:
    database = WorkflowDatabase(tmp_path / "workflow.db")
    database.initialize()
    run_id = database.create_run(
        "people.csv", [{"person_name": "Ada", "linkedin_url": "https://linkedin.com/in/ada"}]
    )
    person = database.people_for_run(run_id)[0]
    output = tmp_path / "checked.csv"
    output.write_text(
        "source_person_id,company_name,servicenow_customer,servicenow_screenshot,check_status,country\n"
        f"{person['id']},Example Corp,No,C:/screenshots/example.png,completed,United Kingdom\n",
        encoding="utf-8",
    )
    assert sync_pipeline_results(database, run_id, output) == 1
    negative = database.unsent_negative_checks(run_id)
    assert len(negative) == 1
    assert negative[0]["company_name"] == "Example Corp"
    assert database.report_rows(run_id)[0]["screenshot_path"] == "C:/screenshots/example.png"
    database.set_n8n_result(
        person["id"], status="not_configured", response="Webhook missing"
    )
    assert len(database.unsent_negative_checks(run_id)) == 1
    database.set_n8n_result(person["id"], status="failed", response="HTTP 500")
    assert len(database.unsent_negative_checks(run_id)) == 1
    database.set_n8n_result(person["id"], status="sent", response="ok", sent=True)
    assert database.unsent_negative_checks(run_id) == []


def test_clear_database_removes_reports_but_keeps_schema(tmp_path: Path) -> None:
    database = WorkflowDatabase(tmp_path / "workflow.db")
    database.initialize()
    database.create_run(
        "people.csv", [{"person_name": "Ada", "linkedin_url": "https://linkedin.com/in/ada"}]
    )
    database.clear_all()
    assert database.summary() == []
    # A new run can be created immediately: only data, not the schema, was removed.
    new_run_id = database.create_run(
        "next.csv", [{"person_name": "Grace", "linkedin_url": "https://linkedin.com/in/grace"}]
    )
    assert database.run(new_run_id)["source_file"] == "next.csv"


def test_build_pipeline_csv_replaces_stale_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    database = WorkflowDatabase(tmp_path / "workflow.db")
    database.initialize()
    run_id = database.create_run(
        "people.csv", [{"person_name": "Ada", "linkedin_url": "https://linkedin.com/in/ada"}]
    )
    person = database.people_for_run(run_id)[0]
    database.update_person_resolution(
        person["id"], company_name="Example Corp", status="openai_verified",
        domain="example.com", company_linkedin_url="https://linkedin.com/company/example",
        headquarters="London, England, United Kingdom", country="United Kingdom", country_code="GB",
    )
    monkeypatch.setattr("workflow.service.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(
        "workflow.service.resolve_people", lambda _database, _run_id, _settings: database.people_for_run(run_id)
    )
    stale_output = tmp_path / "runs" / str(run_id) / "companies_checked.csv"
    stale_output.parent.mkdir(parents=True)
    stale_output.write_text("stale", encoding="utf-8")
    input_path, output_path, count = build_pipeline_csv(database, run_id, object())  # type: ignore[arg-type]
    assert count == 1
    assert not output_path.exists()
    generated = input_path.read_text(encoding="utf-8")
    assert "example.com" in generated
    assert "linkedin.com/company/example" in generated
    assert "London, England, United Kingdom" in generated
    assert "GB" in generated


def test_verified_openai_result_can_disagree_with_headline_hint() -> None:
    resolver = PersonCompanyResolver(FakeResearch())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ada", linkedin_url="https://linkedin.com/in/ada",
        supplied_company_name="", headline="Engineer at Completely Different Ltd",
    )
    assert result.status == "openai_verified"
    assert result.company_name == "OpenAI Organization"
    assert "headline mentioned" in result.error


def test_headline_is_fallback_when_openai_cannot_verify_company() -> None:
    resolver = PersonCompanyResolver(NoMatchResearch())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ada", linkedin_url="https://linkedin.com/in/ada",
        supplied_company_name="", headline="Engineer @ Tech Mahindra | Python",
    )
    assert result.company_name == "Tech Mahindra"
    assert result.status == "linkedin_headline_verified"


def test_no_verified_company_without_headline_is_unresolved() -> None:
    resolver = PersonCompanyResolver(NoMatchResearch())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Different Person",
        linkedin_url="https://linkedin.com/in/different-person",
        supplied_company_name="",
        headline="",
    )
    assert result.status == "unresolved"
    assert result.company_name == ""
