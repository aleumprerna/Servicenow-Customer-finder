from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceCategory(StrEnum):
    CUSTOMER_EVIDENCE = "CUSTOMER_EVIDENCE"
    PARTNER_EVIDENCE = "PARTNER_EVIDENCE"
    AMBIGUOUS = "AMBIGUOUS"
    IRRELEVANT = "IRRELEVANT"


class EvidenceStrength(StrEnum):
    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


class ResearchClassification(StrEnum):
    CONFIRMED_CUSTOMER = "CONFIRMED_CUSTOMER"
    LIKELY_CUSTOMER = "LIKELY_CUSTOMER"
    INCONCLUSIVE = "INCONCLUSIVE"
    NO_OFFICIAL_EVIDENCE = "NO_OFFICIAL_EVIDENCE"
    PARTNER_ONLY = "PARTNER_ONLY"


class EvidenceFinding(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    url: str
    page_title: str = "Untitled source"
    evidence: str
    evidence_type: str = "generic_reference"
    strength: EvidenceStrength = EvidenceStrength.WEAK
    category: EvidenceCategory = EvidenceCategory.AMBIGUOUS
    official_source: bool = False
    # True only when the URL came from a page the crawler fetched, an observed
    # ServiceNow customer-directory result, or search-provider grounding
    # metadata. Model-written URLs alone are not citations and must never be
    # rendered as links.
    citation_grounded: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(str(value).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Evidence URL must use HTTP or HTTPS")
        return parsed.geturl()

    @field_validator("page_title", "evidence", "evidence_type")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return " ".join(str(value or "").split())[:1200]


class ClassificationSuggestion(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    status: ResearchClassification
    confidence: int = Field(ge=0, le=100)
    summary: str = Field(min_length=1, max_length=1000)


class DeepResearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    company_name: str
    official_domain: str
    status: ResearchClassification
    confidence: int = Field(ge=0, le=100)
    summary: str
    customer_evidence: list[EvidenceFinding] = Field(default_factory=list)
    partner_evidence: list[EvidenceFinding] = Field(default_factory=list)
    ambiguous_evidence: list[EvidenceFinding] = Field(default_factory=list)
    visited_urls: list[str] = Field(default_factory=list)
    sources_checked: int = Field(default=0, ge=0)
    relevant_sources: int = Field(default=0, ge=0)
    research_depth: str = "deep"
    model_provider: str = "llm+rules"
    servicenow_customer_page_found: bool | None = None
    servicenow_customer_page_url: str = ""
    selected_methods: list[str] = Field(default_factory=list)
    detection_result: dict[str, Any] = Field(default_factory=dict)
    researched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def as_storage_values(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        return {
            "classification_status": payload["status"],
            "confidence": int(payload["confidence"]),
            "summary": payload["summary"],
            "customer_evidence": payload["customer_evidence"],
            "partner_evidence": payload["partner_evidence"],
            "ambiguous_evidence": payload["ambiguous_evidence"],
            "visited_urls": payload["visited_urls"],
            "sources_checked": int(payload["sources_checked"]),
            "relevant_sources": int(payload["relevant_sources"]),
            "research_depth": payload["research_depth"],
            "model_provider": payload["model_provider"],
            "servicenow_customer_page_found": payload["servicenow_customer_page_found"],
            "servicenow_customer_page_url": payload["servicenow_customer_page_url"],
            "selected_methods": payload["selected_methods"],
            "detection_result": payload["detection_result"],
            "researched_at": payload["researched_at"],
        }
