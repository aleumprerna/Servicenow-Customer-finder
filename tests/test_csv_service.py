from pathlib import Path

from models.company import CheckStatus
from services.csv_service import CSVService


def test_checkpoint_and_resume(tmp_path: Path) -> None:
    source = tmp_path / "companies.csv"
    output = tmp_path / "checked.csv"
    source.write_text(
        "company_name,linkedin_url\nMicrosoft,https://linkedin.com/company/microsoft\nAdobe,\n",
        encoding="utf-8",
    )

    service = CSVService(source, output)
    service.update(
        0,
        check_status=CheckStatus.COMPLETED,
        servicenow_customer="Yes",
        match_score=96,
    )
    service.save()

    resumed = CSVService(source, output)
    assert resumed.record(0).match_score == 96
    assert resumed.selected_indices(force=False, company=None, limit=None) == [1]
    assert resumed.selected_indices(force=True, company=None, limit=None) == [0, 1]
