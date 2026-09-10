from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from config import load_settings
from services.country_normalizer import CountryNormalizationError, normalize_country
from services.gemini_client import GeminiClient
from services.kie_client import KieClient
from services.company_matcher import company_match_score

logger = logging.getLogger(__name__)


def _valid_source_urls(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value[:10]:
        url = str(item or "").strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc and url not in output:
            output.append(url)
    return output


def _valid_source_urls_from_payload(value: Any) -> list[str]:
    output: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"url", "uri"} and isinstance(child, str):
                    for url in _valid_source_urls([child]):
                        if url not in output:
                            output.append(url)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return output


def extract_company_from_headline(headline: str) -> str:
    """Extract a company name from common LinkedIn headline patterns.

    Examples:
        'Head of Corporate Affairs at Harbour Energy' -> 'Harbour Energy'
        'Senior Manager @ Acme Corp | Tech Enthusiast' -> 'Acme Corp'
    """
    cleaned = (headline or "").strip()
    if not cleaned:
        return ""

    # Check for pattern with ' at ' or ' @ '
    # Take the segment before any delimiter like '|', '•', '/', or comma if trailing descriptors follow
    first_clause = re.split(r"\s+[|•/]\s+", cleaned)[0].strip()

    match = re.search(r"(?:\bat\b|@)\s+([A-Za-z0-9&.,'’\- ]+)", first_clause, re.IGNORECASE)
    if match:
        candidate = match.group(1).strip()
        # Clean trailing punctuation
        candidate = re.sub(r"[.,;:\-]+$", "", candidate).strip()
        if candidate and len(candidate) > 1:
            return candidate

    return ""


def resolve_company_headquarters(
    company_name: str,
    api_key: str | None = None,
    *,
    company_domain: str = "",
    base_url: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Resolve headquarters for an already-confirmed company without changing its identity."""

    settings = load_settings()
    provider_name = (provider or settings.llm_provider).casefold()
    key = api_key or settings.llm_api_key
    selected_model = model or settings.llm_model
    selected_base_url = base_url or settings.llm_base_url
    company = " ".join(str(company_name or "").split())
    if not company:
        return {"success": False, "error": "A company name is required."}
    if not key or provider_name not in {"kie", "gemini", "openai"}:
        return {
            "success": False,
            "error": "A web-search-capable KIE, Gemini, or OpenAI provider is required.",
        }

    prompt = f"""
Research the headquarters of exactly this confirmed company: {company}
Known domain, if any: {company_domain or "not provided"}

Do not replace the company with a similarly named business, parent, successor, or acquirer.
If the company was acquired, renamed, or is no longer active, return the headquarters of the
named company while it operated and separately mention its current status/successor. Headquarters
means the corporate headquarters, not the employee's work location or a branch office.
Use reliable web sources, preferring the company's official site, filings, investor material, or
the verified company LinkedIn page. Return at least one exact supporting URL.

Return JSON only:
{{"company_name":"{company}","headquarters":"City and state/region, without country",
"country":"Full country name","country_code":"ISO 3166-1 alpha-2 code",
"company_domain":"company.example","source_urls":["https://..."],
"company_status":"active|acquired|renamed|inactive","successor":"",
"confidence":"high|medium|low","reason":"Brief evidence-based explanation"}}
""".strip()
    try:
        if provider_name == "gemini":
            response = GeminiClient(
                key,
                model=selected_model,
                base_url=selected_base_url,
                max_retries=settings.gemini_max_retries,
                retry_base_seconds=settings.gemini_retry_base_seconds,
            ).generate(prompt, use_google_search=True, use_url_context=True)
            raw_text = response.text
            source_urls = sorted(response.source_urls)
        elif provider_name == "kie":
            response = KieClient(key, base_url=selected_base_url).responses.create(
                model=selected_model,
                tools=[{"type": "web_search"}],
                input=prompt,
                reasoning={"effort": settings.kie_reasoning_effort},
            )
            raw_text = response.output_text
            source_urls = _valid_source_urls_from_payload(response.model_dump())
        else:
            import openai

            response = openai.OpenAI(
                api_key=key, base_url=selected_base_url
            ).responses.create(
                model=selected_model,
                tools=[{"type": "web_search"}],
                include=["web_search_call.action.sources"],
                input=prompt,
            )
            raw_text = getattr(response, "output_text", str(response)).strip()
            source_urls = _valid_source_urls_from_payload(response.model_dump())
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            raise ValueError("AI returned no JSON object")
        data = json.loads(match.group(0))
        returned_company = str(data.get("company_name") or "").strip()
        if not returned_company or company_match_score(company, returned_company) < 85:
            raise ValueError("AI returned headquarters for a different company")
        headquarters = " ".join(str(data.get("headquarters") or "").split())
        country = " ".join(str(data.get("country") or "").split())
        country_code = str(data.get("country_code") or "").strip().upper()
        country_code = normalize_country(country_code or country)
        source_urls = list(
            dict.fromkeys([*source_urls, *_valid_source_urls(data.get("source_urls"))])
        )
        if not headquarters or not country or not source_urls:
            raise ValueError("AI could not ground the headquarters and country")
        return {
            "success": True,
            "company_name": company,
            "headquarters": headquarters,
            "country": country,
            "country_code": country_code,
            "company_domain": str(data.get("company_domain") or company_domain).strip(),
            "company_status": str(data.get("company_status") or "").strip(),
            "successor": str(data.get("successor") or "").strip(),
            "confidence": str(data.get("confidence") or "medium"),
            "reason": str(data.get("reason") or "Headquarters verified by web search"),
            "source_urls": source_urls,
            "source": f"{provider_name}_headquarters_search",
        }
    except Exception as exc:
        logger.warning("%s headquarters resolution failed for %s: %s", provider_name.upper(), company, exc)
        return {"success": False, "error": str(exc), "company_name": company}


def resolve_company_from_web(
    person_name: str,
    linkedin_url: str,
    headline: str = "",
    api_key: str | None = None,
    *,
    base_url: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    require_headquarters: bool = False,
    require_grounding: bool = False,
) -> dict[str, Any]:
    """Search the web to resolve a person's current company from their LinkedIn profile.

    Uses the configured LLM provider with a fallback to headline parsing.
    KIE, Gemini, and OpenAI use grounded web search; GLM uses supplied profile context.
    """
    settings = load_settings()
    provider_name = (provider or settings.llm_provider).casefold()
    key = api_key or settings.llm_api_key
    selected_model = model or settings.llm_model
    selected_base_url = base_url or settings.llm_base_url
    failure_reason = ""

    # 1. Attempt resolution with the configured model if its API key is present.
    if key:
        try:
            prompt = (
                "You are an expert corporate researcher.\n"
                "Your task is to identify the current company/employer of this person.\n"
                f"Person Name: {person_name}\n"
                f"LinkedIn Profile URL: {linkedin_url}\n"
                f"Headline / Current Role Context: {headline}\n\n"
                + (
                    "Search the web, including recent reliable sources, to determine the current employer.\n"
                    if provider_name in {"kie", "openai", "gemini"}
                    else "Use only the supplied profile URL and headline context. Do not claim to have browsed the web.\n"
                )
                + "Confirm the person's CURRENT employer, not a previous employer. Find the "
                "company's official headquarters from a reliable company or business source.\n"
                + "You MUST use web search and provide at least one exact supporting URL that "
                "confirms the current employer or company headquarters.\n"
                + "Return ONLY a valid JSON object in the exact format:\n"
                "{\n"
                '  "company_name": "Company Name",\n'
                '  "headquarters": "City and state/region, without country",\n'
                '  "country": "Full country name",\n'
                '  "country_code": "ISO 3166-1 alpha-2 code",\n'
                '  "company_domain": "company.example",\n'
                '  "company_linkedin_url": "https://www.linkedin.com/company/...",\n'
                '  "source_urls": ["https://supporting-source.example/page"],\n'
                '  "confidence": "high|medium|low",\n'
                '  "reason": "Brief explanation with sources"\n'
                "}\n"
                "Do not include markdown code fences or any explanatory text outside the JSON."
            )

            if provider_name == "gemini":
                result = GeminiClient(
                    key,
                    model=selected_model,
                    base_url=selected_base_url,
                    max_retries=settings.gemini_max_retries,
                    retry_base_seconds=settings.gemini_retry_base_seconds,
                ).generate(prompt, use_google_search=True, use_url_context=True)
                raw_text = result.text
                source_urls = sorted(result.source_urls)
            elif provider_name == "kie":
                response = KieClient(key, base_url=selected_base_url).responses.create(
                    model=selected_model,
                    tools=[{"type": "web_search"}],
                    input=prompt,
                    reasoning={"effort": settings.kie_reasoning_effort},
                )
                raw_text = response.output_text
                source_urls = _valid_source_urls_from_payload(response.model_dump())
            elif provider_name == "openai":
                import openai

                client = openai.OpenAI(api_key=key, base_url=selected_base_url)
                response = client.responses.create(
                    model=selected_model,
                    tools=[{"type": "web_search"}],
                    include=["web_search_call.action.sources"],
                    input=prompt,
                )
                raw_text = getattr(response, "output_text", str(response)).strip()
                source_urls = _valid_source_urls_from_payload(response.model_dump())
            else:
                import openai

                client = openai.OpenAI(api_key=key, base_url=selected_base_url)
                response = client.chat.completions.create(
                    model=selected_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                )
                raw_text = str(response.choices[0].message.content or "").strip()
                source_urls = []

            # Parse JSON from response
            json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                company_name = str(data.get("company_name") or "").strip()
                if company_name and company_name.casefold() not in {"null", "none", "unknown", "n/a"}:
                    source_urls = list(
                        dict.fromkeys([*source_urls, *_valid_source_urls(data.get("source_urls"))])
                    )
                    country = str(data.get("country") or "").strip()
                    country_code = str(data.get("country_code") or "").strip().upper()
                    try:
                        country_code = normalize_country(country_code or country) if (country_code or country) else ""
                    except CountryNormalizationError:
                        country_code = ""
                    headquarters = str(data.get("headquarters") or "").strip()
                    validation_error = ""
                    if require_headquarters and (not headquarters or not country or not country_code):
                        validation_error = (
                            f"AI found {company_name}, but could not verify its headquarters and country."
                        )
                    elif require_grounding and provider_name in {"kie", "gemini", "openai"} and not source_urls:
                        validation_error = (
                            f"AI found {company_name}, but returned no supporting web source."
                        )
                    if validation_error:
                        failure_reason = validation_error
                    else:
                        return {
                            "success": True,
                            "company_name": company_name,
                            "headquarters": headquarters,
                            "country": country,
                            "country_code": country_code,
                            "company_domain": str(data.get("company_domain") or "").strip(),
                            "company_linkedin_url": str(
                                data.get("company_linkedin_url") or ""
                            ).strip(),
                            "confidence": str(data.get("confidence") or "medium"),
                            "reason": str(data.get("reason") or "Resolved via web search"),
                            "source_urls": source_urls,
                            "source": (
                                f"{provider_name}_web_search"
                                if provider_name in {"kie", "openai"}
                                else "gemini_google_search"
                                if provider_name == "gemini"
                                else "glm_profile_context"
                            ),
                        }
        except Exception as exc:
            logger.warning("%s company resolution failed: %s", provider_name.upper(), exc)
            failure_reason = str(exc)

    if require_headquarters or require_grounding:
        return {
            "success": False,
            "company_name": "",
            "confidence": "none",
            "error": failure_reason or "AI could not verify the current company and headquarters.",
            "source": "unresolved",
        }

    # 2. Fallback: Extract from headline
    headline_company = extract_company_from_headline(headline)
    if headline_company:
        return {
            "success": True,
            "company_name": headline_company,
            "headquarters": "",
            "country": "",
            "country_code": "",
            "company_domain": "",
            "company_linkedin_url": "",
            "confidence": "medium",
            "reason": f"Extracted from LinkedIn headline: '{headline}'",
            "source": "headline_fallback",
        }

    # 3. Could not resolve
    return {
        "success": False,
        "company_name": "",
        "confidence": "none",
        "error": "Could not identify current company from web search or profile headline.",
        "source": "unresolved",
    }
