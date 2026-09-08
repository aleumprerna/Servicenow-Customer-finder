from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from openai import OpenAI

from services.gemini_client import GeminiClient

from .classifier import classify_evidence
from .crawler import BoundedOfficialCrawler, UnsafeResearchTarget, normalize_domain
from .evidence_extractor import deduplicate_findings, is_official_url, normalize_model_finding
from .schemas import ClassificationSuggestion, DeepResearchResult, EvidenceCategory, EvidenceFinding


LOGGER = logging.getLogger(__name__)


FINDINGS_FORMAT = {
    "format": {
        "type": "json_schema",
        "name": "servicenow_evidence_findings",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "page_title": {"type": "string"},
                            "evidence": {"type": "string"},
                            "evidence_type": {"type": "string"},
                            "strength": {"type": "string", "enum": ["strong", "medium", "weak"]},
                            "category": {
                                "type": "string",
                                "enum": ["CUSTOMER_EVIDENCE", "PARTNER_EVIDENCE", "AMBIGUOUS"],
                            },
                        },
                        "required": [
                            "url", "page_title", "evidence", "evidence_type", "strength", "category"
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["findings"],
            "additionalProperties": False,
        },
    }
}

CLASSIFICATION_FORMAT = {
    "format": {
        "type": "json_schema",
        "name": "servicenow_customer_classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "CONFIRMED_CUSTOMER", "LIKELY_CUSTOMER", "INCONCLUSIVE",
                        "NO_OFFICIAL_EVIDENCE", "PARTNER_ONLY",
                    ],
                },
                "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                "summary": {"type": "string"},
            },
            "required": ["status", "confidence", "summary"],
            "additionalProperties": False,
        },
    }
}


class DeepResearchError(RuntimeError):
    pass


class ResearchConfigurationError(DeepResearchError):
    pass


class ResearchProviderError(DeepResearchError):
    pass


@dataclass(frozen=True)
class ProviderDiscovery:
    findings: list[EvidenceFinding]
    source_urls: set[str]


def _response_source_urls(response: Any) -> set[str]:
    try:
        payload = response.model_dump(mode="json")
    except Exception:
        return set()
    urls: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "url" and isinstance(item, str) and item.startswith(("http://", "https://")):
                    urls.add(item.rstrip("/"))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return urls


def _source_key(url: str) -> str:
    parsed = urlsplit(url)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
        ]
    )
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/") or "/", query, "")
    )


def _json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1)
    else:
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            value = value[start : end + 1]
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ResearchProviderError("The research provider returned an invalid structured result.") from exc
    if not isinstance(parsed, dict):
        raise ResearchProviderError("The research provider did not return a JSON object.")
    return parsed


class LLMResearchProvider:
    """OpenAI-compatible adapter; deterministic rules make the final status decision."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-4o",
        base_url: str | None = None,
        provider_name: str = "openai",
        supports_hosted_web_search: bool = True,
        timeout_seconds: float = 180.0,
        max_retries: int = 4,
        retry_base_seconds: float = 2.0,
    ) -> None:
        if not str(api_key or "").strip():
            raise ResearchConfigurationError(
                f"An API key is required for the {provider_name.upper()} research provider."
            )
        self.model = model
        self.provider_name = provider_name.casefold()
        self.supports_hosted_web_search = supports_hosted_web_search
        if self.provider_name == "gemini":
            self.client = GeminiClient(
                api_key,
                model=model,
                base_url=base_url or "https://generativelanguage.googleapis.com/v1beta",
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
            )
        else:
            self.client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
                max_retries=1,
            )

    @property
    def provider_label(self) -> str:
        return f"{self.provider_name}:{self.model}"

    @staticmethod
    def _findings(
        payload: dict[str, Any],
        domain: str,
        *,
        official_only: bool,
        source_urls: set[str],
    ) -> list[EvidenceFinding]:
        output: list[EvidenceFinding] = []
        source_keys = {_source_key(url) for url in source_urls}
        raw_findings = payload.get("findings", [])
        if not isinstance(raw_findings, list):
            return output
        for raw in raw_findings[:20]:
            if not isinstance(raw, dict):
                continue
            try:
                finding = normalize_model_finding(raw, domain)
            except ValueError:
                continue
            if finding is None or (official_only and not is_official_url(finding.url, domain)):
                continue
            if source_keys and _source_key(finding.url) not in source_keys:
                continue
            output.append(finding)
        return output

    def discover(
        self,
        *,
        company_name: str,
        official_domain: str,
        existing_context: dict[str, Any],
        official_only: bool,
    ) -> ProviderDiscovery:
        # TokenRouter's GLM endpoint is text-only and does not expose a hosted
        # search tool. The bounded crawler remains the source of truth there.
        if not getattr(self, "supports_hosted_web_search", True):
            return ProviderDiscovery(findings=[], source_urls=set())
        scope = "Only use sources hosted on the official domain or its subdomains." if official_only else (
            "Use reliable third-party sources. Exclude social forums, aggregators, and unsourced directories."
        )
        prompt = f"""
Research whether {company_name} is an end customer that internally uses ServiceNow.
Official domain: {official_domain}
Existing verification context: {json.dumps(existing_context, ensure_ascii=False)[:4000]}
{scope}

Separate end-customer evidence from partner, reseller, consulting, integrator, or client-delivery evidence.
A ServiceNow partnership or implementing ServiceNow for clients NEVER proves internal customer usage.
Return JSON only with this shape:
{{"findings":[{{"url":"https://...","page_title":"...","evidence":"short factual excerpt or close paraphrase","evidence_type":"explicit_internal_usage|internal_role_or_platform_management|partner_or_service_provider|generic_reference","strength":"strong|medium|weak","category":"CUSTOMER_EVIDENCE|PARTNER_EVIDENCE|AMBIGUOUS"}}]}}
Do not invent URLs or evidence. Return an empty findings list when evidence is absent.
""".strip()
        search_tool: dict[str, Any] = {"type": "web_search", "search_context_size": "high"}
        if official_only:
            search_tool["filters"] = {"allowed_domains": [official_domain]}
        try:
            if getattr(self, "provider_name", "openai") == "gemini":
                response = self.client.generate(prompt, use_google_search=True)
                payload = _json_object(response.text)
                source_urls = response.source_urls
            else:
                response = self.client.responses.create(
                    model=self.model,
                    tools=[search_tool],
                    include=["web_search_call.action.sources"],
                    text=FINDINGS_FORMAT,
                    input=prompt,
                )
                payload = _json_object(response.output_text)
                source_urls = _response_source_urls(response)
        except ResearchProviderError:
            raise
        except Exception as exc:
            raise ResearchProviderError(f"The research provider request failed: {exc}") from exc
        return ProviderDiscovery(
            findings=self._findings(
                payload,
                official_domain,
                official_only=official_only,
                source_urls=source_urls,
            ),
            source_urls=source_urls,
        )

    def suggest_classification(
        self, *, company_name: str, official_domain: str, findings: list[EvidenceFinding]
    ) -> ClassificationSuggestion | None:
        evidence = [item.model_dump(mode="json") for item in findings]
        prompt = f"""
Classify whether {company_name} ({official_domain}) internally uses ServiceNow using only the evidence JSON below.
Partner/reseller/consulting/integrator evidence alone must be PARTNER_ONLY, never a customer.
Explicit official internal-use evidence may be CONFIRMED_CUSTOMER. Indirect but credible internal platform/job evidence may be LIKELY_CUSTOMER. Generic mentions are INCONCLUSIVE. No relevant evidence is NO_OFFICIAL_EVIDENCE.
Return JSON only: {{"status":"CONFIRMED_CUSTOMER|LIKELY_CUSTOMER|INCONCLUSIVE|NO_OFFICIAL_EVIDENCE|PARTNER_ONLY","confidence":0,"summary":"concise explanation"}}
Evidence: {json.dumps(evidence, ensure_ascii=False)[:28000]}
""".strip()
        try:
            if self.provider_name == "gemini":
                output_text = self.client.generate(prompt).text
            elif self.supports_hosted_web_search:
                response = self.client.responses.create(
                    model=self.model,
                    text=CLASSIFICATION_FORMAT,
                    input=prompt,
                )
                output_text = response.output_text
            else:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                )
                output_text = response.choices[0].message.content or ""
            return ClassificationSuggestion.model_validate(_json_object(output_text))
        except Exception as exc:
            LOGGER.info("Deep research model synthesis was unavailable: %s", exc)
            return None


class DeepResearchService:
    def __init__(self, *, provider: LLMResearchProvider, crawler: BoundedOfficialCrawler) -> None:
        self.provider = provider
        self.crawler = crawler

    def research(
        self,
        *,
        company_name: str,
        official_domain: str,
        existing_context: dict[str, Any] | None = None,
        research_depth: str = "deep",
    ) -> DeepResearchResult:
        if research_depth != "deep":
            raise DeepResearchError("Only the deep research mode is currently supported.")
        company = " ".join(str(company_name or "").split())
        if not company:
            raise DeepResearchError("A resolved company name is required for Deep Research.")
        try:
            domain = normalize_domain(official_domain)
            LOGGER.info(
                "Deep research started for %s on %s (depth=%s)", company, domain, research_depth
            )
            crawl_report = self.crawler.crawl(domain)
        except UnsafeResearchTarget as exc:
            raise DeepResearchError(str(exc)) from exc

        context = dict(existing_context or {})
        findings = list(crawl_report.findings)
        sources_checked = crawl_report.sources_checked
        LOGGER.info("Deep research official crawl finished for %s (%s pages)", domain, sources_checked)
        if crawl_report.discovered_urls:
            LOGGER.info(
                "Deep research discovered URLs for %s: %s",
                domain,
                crawl_report.discovered_urls[:10],
            )

        provider_failed: ResearchProviderError | None = None
        try:
            official_discovery = self.provider.discover(
                    company_name=company,
                    official_domain=domain,
                    existing_context=context,
                    official_only=True,
                )
            findings.extend(official_discovery.findings)
            sources_checked += len(official_discovery.source_urls)
        except ResearchProviderError as exc:
            provider_failed = exc
            LOGGER.warning("Official-domain research failed for %s: %s", domain, exc)

        findings = deduplicate_findings(findings)
        LOGGER.info("Deep research retained %s unique evidence findings for %s", len(findings), domain)
        has_customer_signal = any(
            item.category == EvidenceCategory.CUSTOMER_EVIDENCE for item in findings
        )
        if not has_customer_signal and provider_failed is None:
            try:
                external = self.provider.discover(
                    company_name=company,
                    official_domain=domain,
                    existing_context=context,
                    official_only=False,
                )
                sources_checked += len(external.source_urls)
                findings.extend(external.findings)
            except ResearchProviderError as exc:
                provider_failed = provider_failed or exc
                LOGGER.warning("External research failed for %s: %s", domain, exc)

        findings = deduplicate_findings(findings)
        if provider_failed and not findings:
            raise provider_failed
        suggestion = self.provider.suggest_classification(
            company_name=company,
            official_domain=domain,
            findings=findings,
        )
        result = classify_evidence(
            company_name=company,
            official_domain=domain,
            findings=findings,
            sources_checked=sources_checked,
            research_depth=research_depth,
            model_provider=(
                f"{getattr(self.provider, 'provider_label', f'openai:{self.provider.model}')}"
                "+deterministic-rules"
            ),
            suggestion=suggestion,
        )
        LOGGER.info(
            "Deep research completed for %s: %s (%s%%, %s relevant sources)",
            domain,
            result.status,
            result.confidence,
            result.relevant_sources,
        )
        return result


# Backwards-compatible import for callers and stored integrations that used the
# original provider name before GLM support was added.
OpenAIResearchProvider = LLMResearchProvider
