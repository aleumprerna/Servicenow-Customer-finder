from pathlib import Path

from clients.apollo import (
    ApolloCurrentCompanyUnavailableError,
    CurrentEmploymentEvidence,
    PersonOrganization,
)
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


def test_headline_current_role_header_is_recognized() -> None:
    people = parse_people_csv(
        b"Person Name,LinkedIn Profile URL,Headline / Current Role\n"
        b"Regitze Reeh,https://linkedin.com/in/regitze-reeh,Head of Corporate Affairs at Harbour Energy\n"
    )
    assert people[0]["headline"] == "Head of Corporate Affairs at Harbour Energy"


class FakeApollo:
    def person_company(self, linkedin_url: str, person_name: str) -> PersonOrganization:
        assert linkedin_url == "https://linkedin.com/in/ada"
        assert person_name == "Ada"
        return PersonOrganization(
            name="Apollo Organization", domain="apollo.io",
            linkedin_url="https://linkedin.com/company/apolloio",
            organization_id="org-1",
            current_employments=(
                CurrentEmploymentEvidence(
                    organization_id="org-1",
                    organization_name="Apollo Organization",
                    start_date="2024-01-01",
                ),
            ),
        )


class ChangedProfileApollo:
    def person_company(self, linkedin_url: str, person_name: str) -> PersonOrganization:
        return PersonOrganization(
            name="Tech Mahindra",
            domain="techmahindra.com",
            person_name="Prakhar Singh",
            person_linkedin_url="https://linkedin.com/in/prakhar-singh",
            profile_matched=False,
            organization_id="org-1",
            current_employments=(
                CurrentEmploymentEvidence(
                    organization_id="org-1",
                    organization_name="Tech Mahindra",
                    start_date="2024-01-01",
                ),
            ),
        )


def test_company_resolver_uses_structurally_verified_apollo_profile() -> None:
    resolver = PersonCompanyResolver(FakeApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ada", linkedin_url="https://linkedin.com/in/ada",
        supplied_company_name="Provided Ltd", headline="Engineer at Apollo Organization",
    )
    assert result.company_name == "Apollo Organization"
    assert result.status == "apollo_structurally_verified"
    assert result.domain == "apollo.io"
    assert result.company_linkedin_url == "https://linkedin.com/company/apolloio"


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
        person["id"], company_name="Example Corp", status="apollo_structurally_verified",
        domain="example.com", company_linkedin_url="https://linkedin.com/company/example",
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


def test_csv_headline_does_not_override_structurally_verified_apollo_company() -> None:
    resolver = PersonCompanyResolver(FakeApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ada", linkedin_url="https://linkedin.com/in/ada",
        supplied_company_name="", headline="Engineer at Completely Different Ltd",
    )
    assert result.status == "apollo_structurally_verified"
    assert result.company_name == "Apollo Organization"
    assert result.error == ""


def test_changed_profile_is_not_rescued_by_csv_headline() -> None:
    resolver = PersonCompanyResolver(ChangedProfileApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Prakhar S.",
        linkedin_url="https://linkedin.com/in/prakhar-s-old",
        supplied_company_name="",
        headline="Software Engineer@ Tech Mahindra|Python",
    )
    assert result.status == "apollo_profile_conflict"
    assert result.company_name == ""


def test_changed_profile_without_two_confirmations_is_blocked() -> None:
    resolver = PersonCompanyResolver(ChangedProfileApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Different Person",
        linkedin_url="https://linkedin.com/in/different-person",
        supplied_company_name="",
        headline="",
    )
    assert result.status == "apollo_profile_conflict"
    assert result.company_name == ""


class ApolloWithoutCurrentCompany:
    def person_company(self, linkedin_url: str, person_name: str) -> PersonOrganization:
        raise ApolloCurrentCompanyUnavailableError(
            "Apollo matched 'Raymond Moore' but organization_id is empty"
        )


class ApolloWithNullPrimaryAndHistoryFallback:
    def person_company(self, linkedin_url: str, person_name: str) -> PersonOrganization:
        return PersonOrganization(
            name="Latest Historical Company",
            selection_source="employment_history_fallback",
        )


def test_structurally_complete_apollo_company_does_not_need_csv_confirmation() -> None:
    resolver = PersonCompanyResolver(FakeApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ada", linkedin_url="https://linkedin.com/in/ada",
        supplied_company_name="", headline="Engineer",
    )
    assert result.status == "apollo_structurally_verified"
    assert result.company_name == "Apollo Organization"
    assert result.error == ""


def test_matched_person_without_current_company_has_specific_status() -> None:
    resolver = PersonCompanyResolver(ApolloWithoutCurrentCompany())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Raymond Moore",
        linkedin_url="https://linkedin.com/in/raymond-moore-901b7551",
        supplied_company_name="",
        headline="",
    )
    assert result.status == "apollo_current_company_unavailable"
    assert result.company_name == ""
    assert "organization_id is empty" in result.error


def test_null_primary_organization_uses_history_with_warning() -> None:
    resolver = PersonCompanyResolver(  # type: ignore[arg-type]
        ApolloWithNullPrimaryAndHistoryFallback()
    )
    result = resolver.resolve(
        person_name="Raymond Moore",
        linkedin_url="https://linkedin.com/in/raymond-moore-901b7551",
        supplied_company_name="Ignored CSV Company",
        headline="Ignored headline at Another Company",
    )
    assert result.status == "apollo_employment_history_fallback"
    assert result.company_name == "Latest Historical Company"
    assert "primary organization name was null" in result.error
    assert "manual confirmation" in result.error


def test_supplied_csv_company_does_not_override_apollo() -> None:
    resolver = PersonCompanyResolver(FakeApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ada", linkedin_url="https://linkedin.com/in/ada",
        supplied_company_name="Actual Company", headline="",
    )
    assert result.status == "apollo_structurally_verified"
    assert result.company_name == "Apollo Organization"


class RegitzeLikeApollo:
    def person_company(self, linkedin_url: str, person_name: str) -> PersonOrganization:
        return PersonOrganization(
            name="Xellia Pharmaceuticals",
            organization_id="xellia-id",
            current_employments=(
                CurrentEmploymentEvidence(
                    organization_id="xellia-id",
                    title="Vice President Corporate Communications & Public Affairs",
                ),
            ),
        )


def test_regitze_like_incomplete_apollo_company_requires_confirmation() -> None:
    resolver = PersonCompanyResolver(RegitzeLikeApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Regitze Reeh",
        linkedin_url="https://linkedin.com/in/regitze-reeh",
        supplied_company_name="",
        headline="Head of Corporate Affairs at Harbour Energy",
    )
    assert result.status == "apollo_reported_current"
    assert result.company_name == "Xellia Pharmaceuticals"
    assert "current-employment evidence was incomplete" in result.error


class IreneuszLikeApollo:
    def person_company(self, linkedin_url: str, person_name: str) -> PersonOrganization:
        return PersonOrganization(
            name="ORLEN S.A.",
            domain="orlen.pl",
            organization_id="orlen-id",
            person_headline="Prezes Zarzadu w Orlen S.A.",
            current_employments=(
                CurrentEmploymentEvidence(
                    organization_id="orlen-id",
                    organization_name="ORLEN S.A.",
                    title="Chief Executive Officer",
                    start_date="2024-04-01",
                ),
            ),
        )


def test_named_dated_current_role_is_cross_verified() -> None:
    resolver = PersonCompanyResolver(IreneuszLikeApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ireneusz Fafara",
        linkedin_url="https://linkedin.com/in/ireneusz-fafara",
        supplied_company_name="",
        headline="President of the Management Board / CEO at ORLEN S.A.",
    )
    assert result.status == "apollo_structurally_verified"
    assert result.company_name == "ORLEN S.A."
    assert result.error == ""


def test_csv_former_role_does_not_override_api_only_resolution() -> None:
    apollo = IreneuszLikeApollo()
    resolver = PersonCompanyResolver(apollo)  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ireneusz Fafara",
        linkedin_url="https://linkedin.com/in/ireneusz-fafara",
        supplied_company_name="",
        headline="Former Chief Executive Officer at ORLEN S.A.",
    )
    assert result.status == "apollo_structurally_verified"
    assert result.company_name == "ORLEN S.A."


class AtulLikeApollo:
    def person_company(self, linkedin_url: str, person_name: str) -> PersonOrganization:
        return PersonOrganization(
            name="SKF ISEAM",
            organization_id="skf-id",
            current_employments=(
                CurrentEmploymentEvidence(
                    organization_id="skf-id",
                    organization_name="SKF India Limited",
                    start_date="2000-12-01",
                ),
            ),
        )


def test_matching_organization_id_allows_current_company_name_alias() -> None:
    resolver = PersonCompanyResolver(AtulLikeApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Atul Dharap",
        linkedin_url="https://linkedin.com/in/atul-dharap",
        supplied_company_name="",
        headline="Safety, Health & Environment Manager at SKF India Ltd.",
    )
    assert result.status == "apollo_structurally_verified"
    assert result.company_name == "SKF ISEAM"
