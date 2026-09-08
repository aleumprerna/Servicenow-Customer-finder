"""Evidence-driven ServiceNow end-customer research."""

from .research_service import (
    DeepResearchError,
    DeepResearchService,
    LLMResearchProvider,
    OpenAIResearchProvider,
    ResearchConfigurationError,
)
from .schemas import DeepResearchResult, EvidenceFinding, ResearchClassification
from .customer_page import CustomerPageCheck, ServiceNowCustomerPageVerifier

__all__ = [
    "DeepResearchError",
    "DeepResearchResult",
    "DeepResearchService",
    "EvidenceFinding",
    "LLMResearchProvider",
    "OpenAIResearchProvider",
    "ResearchClassification",
    "ResearchConfigurationError",
    "CustomerPageCheck",
    "ServiceNowCustomerPageVerifier",
]
