from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CheckStatus(StrEnum):
    PENDING = "pending"
    APOLLO_SUCCESS = "apollo_success"
    APOLLO_FAILED = "apollo_failed"
    SEARCHING = "searching"
    COMPLETED = "completed"
    ERROR = "error"
    MANUAL_REVIEW = "manual_review"


class ApolloCompany(BaseModel):
    model_config = ConfigDict(frozen=True)

    company_name: str
    country: str
    country_code: str
    city: str = ""
    state: str = ""
    headquarters: str
    linkedin_url: str = ""
    domain: str = ""
    match_score: int = Field(ge=0, le=100)


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    customer: str
    matched_name: str = ""
    match_score: int = Field(default=0, ge=0, le=100)
    status: CheckStatus
    error_message: str = ""
    returned_names: tuple[str, ...] = ()


class CompanyRecord(BaseModel):
    """The normalized subset of a CSV row used by the application."""

    company_name: str
    linkedin_url: str = ""
    domain: str = ""
    country_override: str = ""
    headquarters: str = ""
    country: str = ""
    country_code: str = ""
    apollo_company_name: str = ""
    servicenow_customer: str = ""
    servicenow_matched_name: str = ""
    match_score: int | None = None
    check_status: CheckStatus = CheckStatus.PENDING
    error_message: str = ""
    checked_at: str = ""

    @field_validator("company_name")
    @classmethod
    def require_company_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("company_name is required")
        return value

    def checked_now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
