from __future__ import annotations

from .schemas import (
    ClassificationSuggestion,
    DeepResearchResult,
    EvidenceCategory,
    EvidenceFinding,
    EvidenceStrength,
    ResearchClassification,
)


def classify_evidence(
    *,
    company_name: str,
    official_domain: str,
    findings: list[EvidenceFinding],
    sources_checked: int,
    research_depth: str,
    model_provider: str,
    suggestion: ClassificationSuggestion | None = None,
) -> DeepResearchResult:
    customer = [item for item in findings if item.category == EvidenceCategory.CUSTOMER_EVIDENCE]
    partner = [item for item in findings if item.category == EvidenceCategory.PARTNER_EVIDENCE]
    ambiguous = [item for item in findings if item.category == EvidenceCategory.AMBIGUOUS]
    official_strong = [
        item for item in customer if item.official_source and item.strength == EvidenceStrength.STRONG
    ]
    official_medium = [
        item for item in customer if item.official_source and item.strength == EvidenceStrength.MEDIUM
    ]
    external_strong = [
        item for item in customer if not item.official_source and item.strength == EvidenceStrength.STRONG
    ]
    official_servicenow_stories = [
        item for item in customer
        if item.evidence_type == "official_servicenow_customer_story"
        and item.citation_grounded
        and item.strength == EvidenceStrength.STRONG
    ]

    if official_servicenow_stories:
        status = ResearchClassification.CONFIRMED_CUSTOMER
        confidence = min(99, 96 + (len(official_servicenow_stories) - 1) * 2)
        summary = f"An official ServiceNow customer story confirms {company_name} as a customer."
    elif official_strong:
        status = ResearchClassification.CONFIRMED_CUSTOMER
        confidence = min(98, 92 + (len(official_strong) - 1) * 2)
        summary = f"Official {company_name} sources explicitly indicate internal ServiceNow use."
    elif len(official_medium) >= 2:
        status = ResearchClassification.LIKELY_CUSTOMER
        confidence = min(84, 76 + (len(official_medium) - 2) * 3)
        summary = f"Multiple official {company_name} sources indicate an internally managed ServiceNow environment."
    elif official_medium:
        status = ResearchClassification.LIKELY_CUSTOMER
        confidence = 64
        summary = f"An official {company_name} source suggests internal ServiceNow use, but the evidence is indirect."
    elif external_strong:
        status = ResearchClassification.LIKELY_CUSTOMER
        confidence = min(82, 72 + len(external_strong) * 4)
        summary = f"Reliable external evidence indicates {company_name} uses ServiceNow, but no explicit company-domain statement was found."
    elif partner and not customer:
        status = ResearchClassification.PARTNER_ONLY
        confidence = min(98, 88 + len(partner) * 2)
        summary = "ServiceNow partner or service-provider evidence was found, but it does not prove end-customer usage."
    elif ambiguous:
        status = ResearchClassification.INCONCLUSIVE
        confidence = min(54, 32 + len(ambiguous) * 5)
        summary = "ServiceNow references were found, but they do not establish internal use by the company."
    else:
        status = ResearchClassification.NO_OFFICIAL_EVIDENCE
        confidence = 20 if sources_checked else 0
        summary = "No convincing official evidence of internal ServiceNow use was found."

    # A model may improve the wording and fine-tune confidence, but never cross
    # the evidence-derived classification boundary.
    if suggestion and suggestion.status == status:
        summary = suggestion.summary
        ranges = {
            ResearchClassification.CONFIRMED_CUSTOMER: (90, 100),
            ResearchClassification.LIKELY_CUSTOMER: (55, 89),
            ResearchClassification.INCONCLUSIVE: (0, 54),
            ResearchClassification.NO_OFFICIAL_EVIDENCE: (0, 54),
            ResearchClassification.PARTNER_ONLY: (0, 100),
        }
        low, high = ranges[status]
        confidence = max(low, min(high, suggestion.confidence))

    return DeepResearchResult(
        company_name=company_name,
        official_domain=official_domain,
        status=status,
        confidence=confidence,
        summary=summary,
        customer_evidence=customer,
        partner_evidence=partner,
        ambiguous_evidence=ambiguous,
        sources_checked=sources_checked,
        relevant_sources=len({item.url for item in customer + partner + ambiguous}),
        research_depth=research_depth,
        model_provider=model_provider,
    )
