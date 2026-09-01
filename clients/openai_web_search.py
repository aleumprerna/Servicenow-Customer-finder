from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import OpenAI, OpenAIError

from models.company import CompanyEnrichment
from services.company_matcher import company_match_score
from services.country_normalizer import CountryNormalizationError, country_name, normalize_country


class WebSearchError(RuntimeError):
    """OpenAI web research failed or did not return sufficiently reliable data."""


@dataclass(frozen=True, slots=True)
class PersonOrganization:
    name: str
    domain: str = ""
    linkedin_url: str = ""
    confidence: int = 0


_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "domain": {"type": "string"},
        "company_linkedin_url": {"type": "string"},
        "headquarters_city": {"type": "string"},
        "headquarters_state": {"type": "string"},
        "headquarters_country": {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": [
        "company_name",
        "domain",
        "company_linkedin_url",
        "headquarters_city",
        "headquarters_state",
        "headquarters_country",
        "confidence",
    ],
    "additionalProperties": False,
}


class OpenAIWebSearchClient:
    """Resolve current employers and headquarters with OpenAI's built-in web search."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5.4-mini",
        timeout_seconds: float = 45.0,
        max_retries: int = 2,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.client = client or OpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def _research(self, prompt: str) -> dict[str, Any]:
        try:
            response = self.client.responses.create(
                model=self.model,
                tools=[{"type": "web_search"}],
                tool_choice={"type": "web_search"},
                max_tool_calls=3,
                input=prompt,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "company_research",
                        "strict": True,
                        "schema": _JSON_SCHEMA,
                    }
                },
            )
            payload = json.loads(response.output_text)
        except (OpenAIError, json.JSONDecodeError, TypeError, AttributeError) as exc:
            raise WebSearchError(f"OpenAI web search failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise WebSearchError("OpenAI web search returned an unexpected response")
        return payload

    def person_company(self, linkedin_url: str, person_name: str = "") -> PersonOrganization:
        if not linkedin_url.strip():
            raise WebSearchError("A LinkedIn profile URL is required to identify the employer")
        payload = self._research(
            "Use web search to identify this person's current employer. Verify the person name and "
            "LinkedIn profile URL against public sources. Return empty strings rather than guessing. "
            "The headquarters fields are not needed for this request.\n"
            f"Person name: {person_name}\nLinkedIn profile: {linkedin_url}"
        )
        company_name = str(payload.get("company_name", "")).strip()
        confidence = int(payload.get("confidence", 0))
        if not company_name or confidence < 70:
            raise WebSearchError("Web search could not reliably identify the current employer")
        return PersonOrganization(
            name=company_name,
            domain=str(payload.get("domain", "")).strip(),
            linkedin_url=str(payload.get("company_linkedin_url", "")).strip(),
            confidence=confidence,
        )

    def enrich(self, company_name: str, linkedin_url: str = "", domain: str = "") -> CompanyEnrichment:
        payload = self._research(
            "Use web search to identify the requested company and its global corporate headquarters. "
            "Prefer the company's official website and reliable corporate sources. Return empty strings "
            "rather than guessing, and use the country name (not a country code).\n"
            f"Company: {company_name}\nCompany LinkedIn URL: {linkedin_url}\nDomain: {domain}"
        )
        returned_name = str(payload.get("company_name", "")).strip()
        confidence = int(payload.get("confidence", 0))
        score = company_match_score(company_name, returned_name)
        if not returned_name or confidence < 70 or score < 70:
            raise WebSearchError("Web search could not reliably match the requested company")
        raw_country = str(payload.get("headquarters_country", "")).strip()
        try:
            code = normalize_country(raw_country)
        except CountryNormalizationError as exc:
            raise WebSearchError("Web search did not return a usable headquarters country") from exc
        city = str(payload.get("headquarters_city", "")).strip()
        state = str(payload.get("headquarters_state", "")).strip()
        country = country_name(code)
        headquarters = ", ".join(part for part in (city, state, country) if part)
        return CompanyEnrichment(
            company_name=returned_name,
            country=country,
            country_code=code,
            city=city,
            state=state,
            headquarters=headquarters,
            linkedin_url=str(payload.get("company_linkedin_url", "")).strip(),
            domain=str(payload.get("domain", "")).strip(),
            match_score=score,
        )
