from __future__ import annotations

import re
from urllib.parse import urlsplit

from .schemas import EvidenceCategory, EvidenceFinding, EvidenceStrength


SERVICENOW_RE = re.compile(
    r"\b(?:service[-\s]*now|now platform|service[-\s]*now\s+(?:itsm|hrsd|csm|itom))\b",
    re.IGNORECASE,
)
PARTNER_RE = re.compile(
    r"\b(?:service[-\s]*now.{0,45}(?:partner|consulting|consultants?|implementation|migration|"
    r"managed services|support (?:and|&) maintenance|reseller|integrator|services provider)|"
    r"partner\s+with\s+service[-\s]*now|"
    r"implement\s+service[-\s]*now\s+for\s+(?:our\s+)?(?:clients?|customers?)|"
    r"(?:help|support|empower|enable|advise)\s+(?:our\s+)?"
    r"(?:clients?|customers?|businesses|enterprises).{0,100}\b(?:using|with|on)\s+"
    r"(?:service[-\s]*now|the now platform)|"
    r"(?:service[-\s]*now|the now platform).{0,100}\b(?:for|to)\s+(?:our\s+)?"
    r"(?:clients?|customers?|businesses|enterprises)|"
    r"(?:service[-\s]*now\s+experts?|\d+\+?\s+service[-\s]*now implementations?))\b",
    re.IGNORECASE,
)
STRONG_CUSTOMER_RE = re.compile(
    r"\b(?:we|our company|our organization)\s+(?:(?:currently|actively)\s+|have\s+)?"
    r"(?:use|uses|run|runs|rely on|deployed|implemented|migrated to)\s+(?:the\s+)?"
    r"(?:service[-\s]*now|now platform)\b|"
    r"\b(?:employees?|colleagues?|staff)\b.{0,70}\b(?:use|uses|access|login|log in|visit)\b.{0,70}"
    r"(?:service[-\s]*now|[a-z0-9-]+\.service-now\.com)\b|"
    r"\b(?:our|internal|enterprise)\s+service[-\s]*now\s+"
    r"(?:platform|instance|environment|portal|system)\b|"
    r"\bservice[-\s]*now\b.{0,90}\b(?:used internally|internal operations|our employees|our staff)\b|"
    r"\b(?:login|log in|visit|access)\s+(?:to\s+)?https?://[a-z0-9.-]+\.service-now\.com\b",
    re.IGNORECASE,
)
INTERNAL_OWNERSHIP_RE = re.compile(
    r"\b(?:internally|internal operations|our (?:employees|colleagues|staff|service[-\s]*now\s+"
    r"(?:platform|instance|environment|portal|system))|(?:employees?|colleagues?|staff).{0,80}"
    r"(?:service[-\s]*now|[a-z0-9-]+\.service-now\.com)|within our (?:company|organization))\b",
    re.IGNORECASE,
)
MEDIUM_CUSTOMER_RE = re.compile(
    r"\b(?:hiring|join our|seeking|looking for|responsible for)\b.{0,120}"
    r"\bservice[-\s]*now\s+(?:administrator|developer|platform|instance|environment|itsm|hrsd|csm|itom)\b|"
    r"\b(?:manage|maintain|administer|support)\b.{0,80}\b(?:our|internal|enterprise)\b.{0,60}"
    r"\bservice[-\s]*now\b|"
    r"\bservice[-\s]*now\s+(?:administrator|developer)\b.{0,120}\b(?:our|internal|enterprise)\b",
    re.IGNORECASE,
)


def is_official_url(url: str, official_domain: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    domain = official_domain.lower().rstrip(".")
    return hostname == domain or hostname.endswith(f".{domain}")


def classify_text(text: str) -> tuple[EvidenceCategory, EvidenceStrength, str]:
    normalized = " ".join(str(text or "").split())
    if not SERVICENOW_RE.search(normalized):
        return EvidenceCategory.IRRELEVANT, EvidenceStrength.WEAK, "irrelevant"
    partner_signal = PARTNER_RE.search(normalized)
    # Service-provider language wins unless the same excerpt explicitly says
    # the company owns or uses ServiceNow for its own people or operations.
    if partner_signal and not INTERNAL_OWNERSHIP_RE.search(normalized):
        return EvidenceCategory.PARTNER_EVIDENCE, EvidenceStrength.STRONG, "partner_or_service_provider"
    if STRONG_CUSTOMER_RE.search(normalized):
        return EvidenceCategory.CUSTOMER_EVIDENCE, EvidenceStrength.STRONG, "explicit_internal_usage"
    if partner_signal:
        return EvidenceCategory.PARTNER_EVIDENCE, EvidenceStrength.STRONG, "partner_or_service_provider"
    if MEDIUM_CUSTOMER_RE.search(normalized):
        return EvidenceCategory.CUSTOMER_EVIDENCE, EvidenceStrength.MEDIUM, "internal_role_or_platform_management"
    return EvidenceCategory.AMBIGUOUS, EvidenceStrength.WEAK, "generic_servicenow_reference"


def evidence_snippets(
    text: str,
    *,
    url: str,
    page_title: str,
    official_domain: str,
    limit: int = 4,
    citation_grounded: bool = False,
) -> list[EvidenceFinding]:
    compact = " ".join(str(text or "").split())
    findings: list[EvidenceFinding] = []
    seen: set[str] = set()
    for match in SERVICENOW_RE.finditer(compact):
        start = max(0, match.start() - 220)
        end = min(len(compact), match.end() + 260)
        snippet = compact[start:end].strip(" -|,.;")
        key = snippet.casefold()
        if not snippet or key in seen:
            continue
        seen.add(key)
        category, strength, evidence_type = classify_text(snippet)
        if category == EvidenceCategory.IRRELEVANT:
            continue
        findings.append(
            EvidenceFinding(
                url=url,
                page_title=page_title or url,
                evidence=snippet,
                evidence_type=evidence_type,
                strength=strength,
                category=category,
                official_source=is_official_url(url, official_domain),
                citation_grounded=citation_grounded,
            )
        )
        if len(findings) >= limit:
            break
    return findings


def normalize_model_finding(raw: dict[str, object], official_domain: str) -> EvidenceFinding | None:
    url = str(raw.get("url") or "").strip()
    evidence = str(raw.get("evidence") or raw.get("snippet") or "").strip()
    if not url or not evidence:
        return None
    category, strength, evidence_type = classify_text(evidence)
    if category in {EvidenceCategory.IRRELEVANT, EvidenceCategory.AMBIGUOUS}:
        try:
            model_category = EvidenceCategory(str(raw.get("category") or "AMBIGUOUS").upper())
        except ValueError:
            model_category = EvidenceCategory.AMBIGUOUS
        try:
            model_strength = EvidenceStrength(str(raw.get("strength") or "weak").lower())
        except ValueError:
            model_strength = EvidenceStrength.WEAK
        if model_category == EvidenceCategory.PARTNER_EVIDENCE:
            category, strength = model_category, model_strength
        elif category == EvidenceCategory.IRRELEVANT:
            category, strength = EvidenceCategory.AMBIGUOUS, EvidenceStrength.WEAK
        evidence_type = str(raw.get("evidence_type") or evidence_type or "model_classified_reference")
    # Deterministic partner detection always wins over a model's customer label.
    if PARTNER_RE.search(evidence) and not INTERNAL_OWNERSHIP_RE.search(evidence):
        category = EvidenceCategory.PARTNER_EVIDENCE
        strength = EvidenceStrength.STRONG
        evidence_type = "partner_or_service_provider"
    return EvidenceFinding(
        url=url,
        page_title=str(raw.get("page_title") or raw.get("title") or url),
        evidence=evidence,
        evidence_type=evidence_type,
        strength=strength,
        category=category,
        official_source=is_official_url(url, official_domain),
    )


def deduplicate_findings(findings: list[EvidenceFinding]) -> list[EvidenceFinding]:
    deduplicated: list[EvidenceFinding] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.url.rstrip("/").casefold(), finding.evidence.casefold()[:240])
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(finding)
    return deduplicated
