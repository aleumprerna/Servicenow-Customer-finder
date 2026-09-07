from __future__ import annotations

import json
import logging
import re
from typing import Any

from config import load_settings

logger = logging.getLogger(__name__)


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


def resolve_company_from_web(
    person_name: str,
    linkedin_url: str,
    headline: str = "",
    api_key: str | None = None,
    *,
    base_url: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Search the web to resolve a person's current company from their LinkedIn profile.

    Uses the configured LLM provider with a fallback to headline parsing. OpenAI
    can use hosted web search; GLM receives the supplied profile context only.
    """
    settings = load_settings()
    provider_name = (provider or settings.llm_provider).casefold()
    key = api_key or settings.llm_api_key
    selected_model = model or settings.llm_model
    selected_base_url = base_url or settings.llm_base_url

    # 1. Attempt resolution with the configured model if its API key is present.
    if key:
        try:
            import openai

            client = openai.OpenAI(api_key=key, base_url=selected_base_url)
            prompt = (
                "You are an expert corporate researcher.\n"
                "Your task is to identify the current company/employer of this person.\n"
                f"Person Name: {person_name}\n"
                f"LinkedIn Profile URL: {linkedin_url}\n"
                f"Headline / Current Role Context: {headline}\n\n"
                + (
                    "Search the web, including recent reliable sources, to determine the current employer.\n"
                    if provider_name == "openai"
                    else "Use only the supplied profile URL and headline context. Do not claim to have browsed the web.\n"
                )
                +
                "Return ONLY a valid JSON object in the exact format:\n"
                "{\n"
                '  "company_name": "Company Name",\n'
                '  "confidence": "high|medium|low",\n'
                '  "reason": "Brief explanation with sources"\n'
                "}\n"
                "Do not include markdown code fences or any explanatory text outside the JSON."
            )

            if provider_name == "openai":
                response = client.responses.create(
                    model=selected_model,
                    tools=[{"type": "web_search_preview"}],
                    input=prompt,
                )
                raw_text = getattr(response, "output_text", str(response)).strip()
            else:
                response = client.chat.completions.create(
                    model=selected_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                )
                raw_text = str(response.choices[0].message.content or "").strip()

            # Parse JSON from response
            json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                company_name = str(data.get("company_name") or "").strip()
                if company_name and company_name.casefold() not in {"null", "none", "unknown", "n/a"}:
                    return {
                        "success": True,
                        "company_name": company_name,
                        "confidence": str(data.get("confidence") or "medium"),
                        "reason": str(data.get("reason") or "Resolved via web search"),
                        "source": (
                            "openai_web_search" if provider_name == "openai" else "glm_profile_context"
                        ),
                    }
        except Exception as exc:
            logger.warning("%s company resolution failed: %s", provider_name.upper(), exc)

    # 2. Fallback: Extract from headline
    headline_company = extract_company_from_headline(headline)
    if headline_company:
        return {
            "success": True,
            "company_name": headline_company,
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
