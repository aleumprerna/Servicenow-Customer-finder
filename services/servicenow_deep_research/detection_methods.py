from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable, Iterable

from .schemas import EvidenceFinding


METHOD_LABELS = {
    "official": "Official ServiceNow Evidence",
    "procurement": "Procurement / Contracts",
    "technical": "Technical / API Fingerprints",
    "dns": "DNS / Subdomain",
    "github": "GitHub / Developer Evidence",
    "integration": "Integration Evidence",
    "partner": "Partner Evidence",
    "technographic": "Technographic Evidence",
    "employee": "Employee / Job Evidence",
    "historical": "Historical Evidence",
}
ALL_METHODS = tuple(METHOD_LABELS)

MODULE_PATTERNS = {
    "ITSM": r"\bitsm\b|it service management",
    "ITOM": r"\bitom\b|operations management",
    "ITAM": r"\bitam\b|asset management",
    "CSM": r"\bcsm\b|customer service management",
    "HRSD": r"\bhrsd\b|hr service delivery",
    "SecOps": r"\bsecops\b|security operations",
    "GRC/IRM": r"\bgrc\b|\birm\b|integrated risk management",
    "SPM": r"\bspm\b|strategic portfolio management",
    "App Engine": r"app engine",
}

INTEGRATIONS = (
    "okta", "entra id", "cyberark", "sap", "oracle", "workday", "salesforce",
    "splunk", "crowdstrike", "sentinel", "jira", "azure devops", "github", "gitlab",
    "aws", "azure", "gcp", "mulesoft", "boomi", "workato",
)


def official_evidence(item: EvidenceFinding) -> bool:
    return "servicenow.com" in item.url.casefold() or item.evidence_type.startswith("official_servicenow")


def procurement_evidence(item: EvidenceFinding) -> bool:
    text = f"{item.url} {item.page_title} {item.evidence}".casefold()
    return any(term in text for term in ("tender", "contract", "procurement", "rfp", "ted.europa"))


def technical_evidence(item: EvidenceFinding) -> bool:
    text = item.evidence.casefold()
    return any(term in text for term in ("/api/now", "oauth_token.do", "sys_id", "sysparm_query", "gliderecord", "table api"))


def dns_evidence(item: EvidenceFinding) -> bool:
    text = f"{item.url} {item.evidence}".casefold()
    return item.evidence_type == "servicenow_subdomain" or bool(re.search(r"\b(?:snow|itsm|service|servicedesk|support|portal|employee|now)\.[a-z0-9.-]+", text))


def github_evidence(item: EvidenceFinding) -> bool:
    text = f"{item.url} {item.evidence}".casefold()
    return "github.com" in text or any(term in text for term in ("snow_instance", "sn_instance", "servicenowclient", "gliderecord"))


def integration_evidence(item: EvidenceFinding) -> bool:
    text = item.evidence.casefold()
    return "servicenow" in text and any(term in text for term in INTEGRATIONS)


def partner_evidence(item: EvidenceFinding) -> bool:
    return item.category == "PARTNER_EVIDENCE" or item.evidence_type == "partner_or_service_provider"


def technographic_evidence(item: EvidenceFinding) -> bool:
    text = f"{item.url} {item.page_title}".casefold()
    return any(term in text for term in ("apollo", "6sense", "zoominfo", "demandbase", "similartech", "datanyze", "builtwith"))


def employee_evidence(item: EvidenceFinding) -> bool:
    text = f"{item.page_title} {item.evidence}".casefold()
    return any(term in text for term in ("developer", "architect", "cmdb manager", "service management lead", "csa", "cis-", "job", "career", "hiring"))


def historical_evidence(item: EvidenceFinding) -> bool:
    text = item.evidence.casefold()
    return bool(re.search(r"\b(?:former|previously|migrat(?:ed|ing)|expand(?:ed|ing)|historical|legacy)\b", text))


DETECTORS: dict[str, Callable[[EvidenceFinding], bool]] = {
    "official": official_evidence,
    "procurement": procurement_evidence,
    "technical": technical_evidence,
    "dns": dns_evidence,
    "github": github_evidence,
    "integration": integration_evidence,
    "partner": partner_evidence,
    "technographic": technographic_evidence,
    "employee": employee_evidence,
    "historical": historical_evidence,
}

SCORES = {
    "procurement": 30, "official": 30, "partner": 25, "technical": 25,
    "github": 25, "integration": 20, "dns": 20, "technographic": 15,
    "employee": 10, "historical": 5,
}


def normalize_methods(values: Iterable[str] | None) -> tuple[str, ...]:
    requested = {str(value).strip().casefold() for value in (values or ())}
    if not requested or "all" in requested:
        return ALL_METHODS
    return tuple(method for method in ALL_METHODS if method in requested)


def method_for_finding(item: EvidenceFinding, selected: Iterable[str]) -> str | None:
    for method in normalize_methods(selected):
        if DETECTORS[method](item):
            return method
    return None


def _evidence_year(item: EvidenceFinding) -> int | None:
    match = re.search(r"\b(20\d{2})\b", f"{item.page_title} {item.evidence}")
    return int(match.group(1)) if match else None


def recency_factor(item: EvidenceFinding, now_year: int | None = None) -> float:
    year = _evidence_year(item)
    if year is None:
        return 1.0
    age = max(0, (now_year or datetime.now(timezone.utc).year) - year)
    return 1.0 if age == 0 else 0.9 if age == 1 else 0.75 if age == 2 else 0.5 if age <= 4 else 0.25


def score_findings(company_name: str, findings: Iterable[EvidenceFinding], selected: Iterable[str]) -> dict[str, object]:
    methods = normalize_methods(selected)
    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    modules: set[str] = set()
    total = 0.0
    historical = False
    for item in findings:
        method = method_for_finding(item, methods)
        if not method:
            continue
        key = (item.url.rstrip("/").casefold(), method)
        if key in seen:
            continue
        seen.add(key)
        factor = recency_factor(item)
        raw = SCORES[method]
        adjusted = round(raw * factor, 2)
        # DNS and other weak/ambiguous signals remain supporting evidence.
        total += adjusted
        historical = historical or historical_evidence(item)
        text = f"{item.page_title} {item.evidence}"
        modules.update(name for name, pattern in MODULE_PATTERNS.items() if re.search(pattern, text, re.I))
        evidence.append({
            "type": method,
            "source": item.page_title,
            "url": item.url if item.citation_grounded else "",
            "evidence_text": item.evidence,
            "date": str(_evidence_year(item) or ""),
            "raw_score": raw,
            "recency_factor": factor,
            "adjusted_score": adjusted,
        })
    score = min(100, round(total))
    strong = any(item["raw_score"] >= 25 for item in evidence)
    if score >= 70 and strong:
        status = "Confirmed"
    elif score >= 50:
        status = "Very High Confidence"
    elif score >= 30:
        status = "Probable"
    elif score >= 15:
        status = "Possible"
    else:
        status = "Weak" if evidence else "Unknown"
    if historical and status in {"Weak", "Possible"}:
        status = "Former Customer"
    return {
        "company_name": company_name,
        "servicenow_status": status,
        "confidence_score": score,
        "current_customer_probability": score,
        "detected_modules": sorted(modules),
        "evidence": evidence,
        "buying_signals": [],
        "reason": f"{len(evidence)} unique evidence source(s) scored across {len(methods)} selected detection method(s).",
        "selected_methods": list(methods),
    }
