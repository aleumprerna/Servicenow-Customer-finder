from services.servicenow_deep_research.detection_methods import (
    ALL_METHODS,
    normalize_methods,
    recency_factor,
    score_findings,
)
from services.servicenow_deep_research.schemas import EvidenceFinding


def finding(url: str, evidence: str, *, evidence_type: str = "generic_reference") -> EvidenceFinding:
    return EvidenceFinding(
        url=url,
        page_title="Evidence source",
        evidence=evidence,
        evidence_type=evidence_type,
        category="CUSTOMER_EVIDENCE",
        strength="strong",
        citation_grounded=True,
    )


def test_method_selection_supports_all_or_multiple_methods() -> None:
    assert normalize_methods(["all"]) == ALL_METHODS
    assert normalize_methods(["technical", "procurement"]) == ("procurement", "technical")


def test_scoring_deduplicates_same_source_and_method() -> None:
    result = score_findings(
        "Example",
        [
            finding("https://contracts.example/1", "ServiceNow procurement contract"),
            finding("https://contracts.example/1", "ServiceNow tender award duplicate"),
        ],
        ["procurement"],
    )
    assert result["confidence_score"] == 30
    assert len(result["evidence"]) == 1
    assert result["servicenow_status"] == "Probable"


def test_weak_dns_signal_cannot_confirm_customer() -> None:
    result = score_findings(
        "Example",
        [finding("https://support.example.com", "Possible ServiceNow support subdomain", evidence_type="servicenow_subdomain")],
        ["dns"],
    )
    assert result["servicenow_status"] == "Possible"
    assert result["confidence_score"] == 20


def test_old_evidence_receives_recency_reduction() -> None:
    item = finding("https://example.com/history", "ServiceNow platform announcement 2019")
    assert recency_factor(item, now_year=2026) == 0.25


def test_detects_servicenow_modules() -> None:
    result = score_findings(
        "Example",
        [finding("https://www.servicenow.com/customers/example.html", "Example uses ServiceNow ITSM and ITOM")],
        ["official"],
    )
    assert result["detected_modules"] == ["ITOM", "ITSM"]
