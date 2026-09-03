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
) -> dict[str, Any]:
    """Search the web to resolve a person's current company from their LinkedIn profile.

    Uses OpenAI web search preview with a fallback to headline parsing.
    """
    key = api_key or load_settings().openai_api_key

    # 1. Attempt OpenAI web search preview if API key is present
    if key:
        try:
            import openai

            client = openai.OpenAI(api_key=key)
            prompt = (
                "You are an expert corporate researcher.\n"
                "Your task is to identify the current company/employer of this person.\n"
                f"Person Name: {person_name}\n"
                f"LinkedIn Profile URL: {linkedin_url}\n"
                f"Headline / Current Role Context: {headline}\n\n"
                "Search the web (including LinkedIn profile data, recent posts, company announcements, "
                "news, and executive directories) to determine their current employer / company name.\n"
                "Return ONLY a valid JSON object in the exact format:\n"
                "{\n"
                '  "company_name": "Company Name",\n'
                '  "confidence": "high|medium|low",\n'
                '  "reason": "Brief explanation with sources"\n'
                "}\n"
                "Do not include markdown code fences or any explanatory text outside the JSON."
            )

            response = client.responses.create(
                model="gpt-4o",
                tools=[{"type": "web_search_preview"}],
                input=prompt,
            )
            raw_text = getattr(response, "output_text", str(response)).strip()

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
                        "source": "openai_web_search",
                    }
        except Exception as exc:
            logger.warning("OpenAI web search company resolution failed: %s", exc)

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
