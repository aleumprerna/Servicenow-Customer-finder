import os
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


def test_checkpoint_save_retries_a_transient_windows_file_lock(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "companies.csv"
    output = tmp_path / "checked.csv"
    source.write_text("company_name\nExample Corp\n", encoding="utf-8")
    service = CSVService(source, output)
    service.update(0, check_status=CheckStatus.COMPLETED)
    attempts = 0
    real_replace = os.replace

    def flaky_replace(source_path, destination_path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("checkpoint is briefly in use")
        real_replace(source_path, destination_path)

    monkeypatch.setattr("services.csv_service.os.replace", flaky_replace)
    monkeypatch.setattr("services.csv_service.time.sleep", lambda _seconds: None)

    service.save()

    assert attempts == 2
    assert CSVService(source, output).record(0).check_status == CheckStatus.COMPLETED
