from pathlib import Path

from clients.apollo import PersonOrganization
from workflow.database import WorkflowDatabase
from workflow.person_company import PersonCompanyResolver
from workflow.service import parse_people_csv, sync_pipeline_results


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


class FakeApollo:
    def person_company(self, linkedin_url: str, person_name: str) -> PersonOrganization:
        assert linkedin_url == "https://linkedin.com/in/ada"
        assert person_name == "Ada"
        return PersonOrganization(
            name="Apollo Organization", domain="apollo.io",
            linkedin_url="https://linkedin.com/company/apolloio",
        )


def test_company_resolver_uses_apollo_person_profile_not_headline() -> None:
    resolver = PersonCompanyResolver(FakeApollo())  # type: ignore[arg-type]
    result = resolver.resolve(
        person_name="Ada", linkedin_url="https://linkedin.com/in/ada",
        supplied_company_name="Provided Ltd", headline="Engineer at Example Corp",
    )
    assert result.company_name == "Apollo Organization"
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
        "source_person_id,company_name,servicenow_customer,check_status,country\n"
        f"{person['id']},Example Corp,No,completed,United Kingdom\n",
        encoding="utf-8",
    )
    assert sync_pipeline_results(database, run_id, output) == 1
    negative = database.unsent_negative_checks(run_id)
    assert len(negative) == 1
    assert negative[0]["company_name"] == "Example Corp"


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
