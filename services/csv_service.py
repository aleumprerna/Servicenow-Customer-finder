from __future__ import annotations

import csv
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from models.company import CheckStatus, CompanyRecord


OUTPUT_COLUMNS = [
    "company_name",
    "linkedin_url",
    "headquarters",
    "country",
    "country_code",
    "apollo_company_name",
    "servicenow_customer",
    "servicenow_matched_name",
    "servicenow_screenshot",
    "match_score",
    "check_status",
    "error_message",
    "checked_at",
]
OPTIONAL_INPUT_COLUMNS = ["domain", "country_override"]


class _CellAccessor:
    def __init__(self, frame: "_CSVFrame") -> None:
        self._frame = frame

    def __getitem__(self, key: tuple[int, str]) -> str:
        index, column = key
        return self._frame.rows[index].get(column, "")

    def __setitem__(self, key: tuple[int, str], value: str) -> None:
        index, column = key
        if column not in self._frame.columns:
            self._frame.columns.append(column)
        self._frame.rows[index][column] = value


class _CSVFrame:
    """Minimal table interface used by CSVService and its callers."""

    def __init__(self, columns: list[str], rows: list[dict[str, str]]) -> None:
        self.columns = columns
        self.rows = rows
        self.at = _CellAccessor(self)


class CSVService:
    def __init__(self, input_path: Path, output_path: Path) -> None:
        self.input_path = input_path
        self.output_path = output_path
        self.frame = self._load()

    def _load(self) -> _CSVFrame:
        source = self.output_path if self.output_path.exists() else self.input_path
        if not source.exists():
            raise FileNotFoundError(f"Input CSV was not found: {self.input_path}")
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = list(reader.fieldnames or [])
            rows = [
                {key: "" if value is None else value for key, value in row.items() if key is not None}
                for row in reader
            ]
        if "company_name" not in columns:
            raise ValueError("CSV must contain a company_name column")
        if "linkedin_url" not in columns:
            columns.append("linkedin_url")
        for column in (*OUTPUT_COLUMNS, *OPTIONAL_INPUT_COLUMNS):
            if column not in columns:
                columns.append(column)
            default = "pending" if column == "check_status" else ""
            for row in rows:
                row.setdefault(column, default)
        ordered = OUTPUT_COLUMNS + OPTIONAL_INPUT_COLUMNS
        extras = [column for column in columns if column not in ordered]
        return _CSVFrame(ordered + extras, rows)

    def selected_indices(
        self, *, force: bool, company: str | None, limit: int | None
    ) -> list[int]:
        selected: list[int] = []
        company_key = company.casefold().strip() if company else None
        for index, row in enumerate(self.frame.rows):
            if company_key and str(row["company_name"]).casefold().strip() != company_key:
                continue
            if not force and str(row["check_status"]).strip() == CheckStatus.COMPLETED:
                continue
            selected.append(int(index))
            if limit is not None and len(selected) >= limit:
                break
        return selected

    def record(self, index: int) -> CompanyRecord:
        row = self.frame.rows[index]
        raw_score = str(row.get("match_score", "")).strip()
        raw_status = str(row.get("check_status", "pending")).strip() or "pending"
        try:
            status = CheckStatus(raw_status)
        except ValueError:
            status = CheckStatus.PENDING
        return CompanyRecord(
            company_name=str(row["company_name"]),
            linkedin_url=str(row.get("linkedin_url", "")),
            domain=str(row.get("domain", "")),
            country_override=str(row.get("country_override", "")),
            headquarters=str(row.get("headquarters", "")),
            country=str(row.get("country", "")),
            country_code=str(row.get("country_code", "")),
            apollo_company_name=str(row.get("apollo_company_name", "")),
            servicenow_customer=str(row.get("servicenow_customer", "")),
            servicenow_matched_name=str(row.get("servicenow_matched_name", "")),
            servicenow_screenshot=str(row.get("servicenow_screenshot", "")),
            match_score=int(float(raw_score)) if raw_score else None,
            check_status=status,
            error_message=str(row.get("error_message", "")),
            checked_at=str(row.get("checked_at", "")),
        )

    def update(self, index: int, **values: Any) -> None:
        for key, value in values.items():
            if isinstance(value, CheckStatus):
                value = value.value
            self.frame.at[index, key] = "" if value is None else str(value)

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.output_path.stem}.", suffix=".tmp", dir=self.output_path.parent
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            with temp_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.frame.columns, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self.frame.rows)
            for attempt in range(20):
                try:
                    os.replace(temp_path, self.output_path)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    # A dashboard progress read can briefly hold the target on
                    # Windows. Retry without losing the completed row update.
                    time.sleep(0.05)
        finally:
            temp_path.unlink(missing_ok=True)
