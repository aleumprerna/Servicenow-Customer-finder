from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    citation_type: str
    title: str
    url: str = ""


@dataclass(frozen=True, slots=True)
class N8nEvidence:
    delivery_status: str
    servicenow_status: str = ""
    verification_status: str = ""
    source_type: str = ""
    evidence_strength: str = ""
    evidence_note: str = ""
    research_sources: tuple[str, ...] = ()
    citations: tuple[EvidenceCitation, ...] = ()
    parse_error: bool = False


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _source_label(value: Any) -> str:
    normalized = _text(value).lower().replace("-", "_").replace(" ", "_")
    labels = {
        "apollo": "Apollo",
        "websearch": "Web search",
        "web_search": "Web search",
        "google": "Web search",
        "servicenow": "ServiceNow",
        "official_servicenow": "Official ServiceNow",
        "none": "No confirming source",
    }
    return labels.get(normalized, _text(value).replace("_", " ").title())


def _safe_url(value: Any) -> str:
    url = _text(value).strip("\"'[]() ")
    parts = urlsplit(url)
    return url if parts.scheme in {"http", "https"} and parts.netloc else ""


def parse_n8n_evidence(delivery_status: str, response: str) -> N8nEvidence:
    if not response.strip():
        return N8nEvidence(delivery_status=delivery_status)
    try:
        payload = json.loads(response)
    except (json.JSONDecodeError, TypeError):
        return N8nEvidence(
            delivery_status=delivery_status,
            verification_status="Stored n8n response is incomplete or invalid JSON",
            parse_error=True,
        )
    if not isinstance(payload, dict):
        return N8nEvidence(delivery_status=delivery_status, parse_error=True)

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    source_type = _source_label(result.get("status_source"))
    if not source_type and result.get("apollo_only") is True:
        source_type = "Apollo"

    raw_sources = result.get("research_sources")
    research_sources = tuple(
        _text(item) for item in raw_sources if _text(item)
    ) if isinstance(raw_sources, list) else ()

    citation_items: list[dict[str, Any]] = []
    for container in (result, payload):
        value = container.get("citations")
        if isinstance(value, list):
            citation_items.extend(item for item in value if isinstance(item, dict))
    evidence_urls = result.get("evidence_urls")
    if isinstance(evidence_urls, list):
        citation_items.extend(
            {"type": source_type or "Evidence", "title": "Evidence", "url": url}
            for url in evidence_urls
        )

    citations: list[EvidenceCitation] = []
    seen: set[tuple[str, str, str]] = set()
    for item in citation_items:
        citation = EvidenceCitation(
            citation_type=_text(item.get("type")) or source_type or "Evidence",
            title=_text(item.get("title")) or "Source",
            url=_safe_url(item.get("url")),
        )
        key = (citation.citation_type, citation.title, citation.url)
        if key not in seen:
            seen.add(key)
            citations.append(citation)

    return N8nEvidence(
        delivery_status=delivery_status,
        servicenow_status=_text(result.get("servicenow_user")),
        verification_status=_text(result.get("verification_status")),
        source_type=source_type,
        evidence_strength=_text(result.get("evidence_strength")),
        evidence_note=_text(result.get("evidence_note")),
        research_sources=research_sources,
        citations=tuple(citations),
    )
