from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

import pandas as pd

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


class CSVService:
    def __init__(self, input_path: Path, output_path: Path) -> None:
        self.input_path = input_path
        self.output_path = output_path
        self.frame = self._load()

    def _load(self) -> pd.DataFrame:
        source = self.output_path if self.output_path.exists() else self.input_path
        if not source.exists():
            raise FileNotFoundError(f"Input CSV was not found: {self.input_path}")
        frame = pd.read_csv(source, dtype=str, keep_default_na=False)
        if "company_name" not in frame.columns:
            raise ValueError("CSV must contain a company_name column")
        if "linkedin_url" not in frame.columns:
            frame["linkedin_url"] = ""
        for column in (*OUTPUT_COLUMNS, *OPTIONAL_INPUT_COLUMNS):
            if column not in frame.columns:
                frame[column] = "pending" if column == "check_status" else ""
        ordered = OUTPUT_COLUMNS + OPTIONAL_INPUT_COLUMNS
        extras = [column for column in frame.columns if column not in ordered]
        return frame[ordered + extras]

    def selected_indices(
        self, *, force: bool, company: str | None, limit: int | None
    ) -> list[int]:
        selected: list[int] = []
        company_key = company.casefold().strip() if company else None
        for index, row in self.frame.iterrows():
            if company_key and str(row["company_name"]).casefold().strip() != company_key:
                continue
            if not force and str(row["check_status"]).strip() == CheckStatus.COMPLETED:
                continue
            selected.append(int(index))
            if limit is not None and len(selected) >= limit:
                break
        return selected

    def record(self, index: int) -> CompanyRecord:
        row = self.frame.loc[index]
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
            if key not in self.frame.columns:
                self.frame[key] = ""
            if isinstance(value, CheckStatus):
                value = value.value
            # The frame intentionally uses string dtype so identifiers and country
            # codes round-trip exactly across pandas versions (including pandas 3).
            self.frame.at[index, key] = "" if value is None else str(value)

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.output_path.stem}.", suffix=".tmp", dir=self.output_path.parent
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            self.frame.to_csv(temp_path, index=False)
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
