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
    source_tags: tuple[str, ...] = ()
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
        "official_servicenow_customer": "Official ServiceNow customer",
        "servicenow_customer_page": "Official ServiceNow customer",
        "official_servicenow_partner": "Official ServiceNow partner",
        "servicenow_partner_page": "Official ServiceNow partner",
        "servicenow_integration_app": "ServiceNow integration app",
        "none": "No confirming source",
    }
    return labels.get(normalized, _text(value).replace("_", " ").title())


def _safe_url(value: Any) -> str:
    url = _text(value).strip("\"'[]() ")
    parts = urlsplit(url)
    return url if parts.scheme in {"http", "https"} and parts.netloc else ""


def _source_tags(
    source_type: str,
    research_sources: tuple[str, ...],
    citations: tuple[EvidenceCitation, ...],
) -> tuple[str, ...]:
    """Return stable UI labels for the places that contributed evidence."""

    tags: list[str] = []

    def add(label: str) -> None:
        if label not in tags:
            tags.append(label)

    normalized_source = source_type.casefold()
    normalized_research = {item.casefold().replace("_", " ") for item in research_sources}
    if normalized_source == "web search" or normalized_research.intersection(
        {"google", "web search", "websearch", "bing"}
    ):
        add("Web search")
    if "servicenow" in normalized_source and "integration app" in normalized_source:
        add("ServiceNow integration app")
    if "servicenow" in normalized_source and "customer" in normalized_source:
        add("ServiceNow customer page")
    if "servicenow" in normalized_source and "partner" in normalized_source:
        add("ServiceNow partner page")

    for citation in citations:
        combined = f"{citation.citation_type} {citation.title}".casefold()
        parts = urlsplit(citation.url)
        host = parts.netloc.casefold().removeprefix("www.")
        path = parts.path.casefold()
        is_servicenow = host == "servicenow.com" or host.endswith(".servicenow.com")
        if (is_servicenow and "/customer" in path) or (
            "servicenow" in combined and "customer" in combined
        ):
            add("ServiceNow customer page")
        if (is_servicenow and "/partner" in path) or (
            "servicenow" in combined and "partner" in combined
        ):
            add("ServiceNow partner page")

    if source_type == "Official ServiceNow" and not any(
        tag.startswith("ServiceNow ") for tag in tags
    ):
        add("ServiceNow website")
    return tuple(tags)


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
        for item in evidence_urls:
            if isinstance(item, dict):
                citation_items.append(
                    {
                        "type": _text(item.get("type")) or source_type or "Evidence",
                        "title": _text(item.get("title")) or "Official evidence",
                        "url": item.get("url"),
                    }
                )
            else:
                citation_items.append(
                    {"type": source_type or "Evidence", "title": "Evidence", "url": item}
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

    citation_tuple = tuple(citations)
    return N8nEvidence(
        delivery_status=delivery_status,
        servicenow_status=_text(result.get("servicenow_user")),
        verification_status=_text(result.get("verification_status")),
        source_type=source_type,
        evidence_strength=_text(result.get("evidence_strength")),
        evidence_note=_text(result.get("evidence_note")),
        research_sources=research_sources,
        citations=citation_tuple,
        source_tags=_source_tags(source_type, research_sources, citation_tuple),
    )
