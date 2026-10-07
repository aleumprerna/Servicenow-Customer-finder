from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .country_normalizer import CountryNormalizationError, country_name, normalize_country


class CompanyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    company_name: str = Field(min_length=1, max_length=300)
    headquarters: str = Field(min_length=1, max_length=1000)
    country: str | None = Field(default=None, min_length=1, max_length=100)
    country_code: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def resolve_country(self) -> "CompanyInput":
        if self.country_code or self.country:
            code = normalize_country(self.country_code or self.country)
            if self.country and normalize_country(self.country) != code:
                raise ValueError("country and country_code must identify the same country")
        else:
            # Full location strings must end in a country, e.g. San Jose, CA, US.
            try:
                code = normalize_country(self.headquarters.rsplit(",", 1)[-1].strip())
            except CountryNormalizationError as exc:
                raise ValueError(
                    "Provide country_code or country when headquarters does not end in a country "
                    "(example: 'San Jose, California, United States')"
                ) from exc
        self.country = country_name(code)  # Also verifies ISO prefixes such as 'US - United States'.
        self.country_code = code
        return self


class CheckCompaniesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    companies: list[CompanyInput] = Field(min_length=1, max_length=100)


class PreparationResponse(BaseModel):
    success: bool = True
    ready: bool = True
    page_kind: Literal["customer_information"] = "customer_information"
    message: str = "Customer Information is ready for company and headquarters searches."


class CompanyResponse(BaseModel):
    company_name: str
    headquarters: str
    country: str
    country_code: str
    servicenow_customer: Literal["Yes", "No", "Unknown"]
    servicenow_matched_name: str = ""
    match_score: int | None = None
    check_status: Literal["completed", "manual_review", "error"]
    returned_names: list[str] = Field(default_factory=list)
    error_message: str = ""
    checked_at: datetime


class CheckCompaniesResponse(BaseModel):
    success: bool
    total: int
    completed: int
    manual_review: int
    errors: int
    results: list[CompanyResponse]

    @classmethod
    def from_results(cls, results: list[CompanyResponse]) -> "CheckCompaniesResponse":
        completed = sum(row.check_status == "completed" for row in results)
        manual_review = sum(row.check_status == "manual_review" for row in results)
        errors = sum(row.check_status == "error" for row in results)
        return cls(
            success=completed == len(results), total=len(results),
            completed=completed, manual_review=manual_review, errors=errors, results=results,
        )
