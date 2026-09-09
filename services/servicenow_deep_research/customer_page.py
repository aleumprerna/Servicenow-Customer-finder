from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

import requests


_LEGAL_SUFFIXES = {
    "ag", "asa", "as", "bv", "co", "company", "corp", "corporation", "gmbh",
    "inc", "incorporated", "limited", "ltd", "llc", "nv", "oy", "plc", "pte",
    "sa", "sas", "spa",
}
_GENERIC_WORDS = _LEGAL_SUFFIXES | {"group", "holding", "holdings", "the"}
_STORY_MARKERS = (
    "customer story",
    "customer details",
    "recommended stories",
    "share this story",
)


@dataclass(frozen=True)
class CustomerPageCheck:
    # None means every HTTP attempt failed, so absence was not established.
    found: bool | None
    url: str = ""
    checked_urls: tuple[str, ...] = ()


def _words(value: str) -> list[str]:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[a-z0-9]+", ascii_value.casefold())


def customer_story_slugs(company_name: str) -> list[str]:
    """Build a small, deterministic set of likely ServiceNow customer-story slugs."""

    words = _words(company_name)
    if not words:
        return []
    variants: list[list[str]] = [words]
    without_legal = list(words)
    while without_legal and without_legal[-1] in _LEGAL_SUFFIXES:
        without_legal.pop()
    if without_legal:
        variants.append(without_legal)
    without_generic = [word for word in without_legal if word not in _GENERIC_WORDS]
    if without_generic:
        variants.append(without_generic)
    # Brand-only pages are common (for example, an entity such as "SKF India Ltd"
    # may use the parent brand slug). Page-content validation prevents false matches.
    if words[0] not in _GENERIC_WORDS and len(words[0]) >= 3:
        variants.append([words[0]])

    slugs: list[str] = []
    for variant in variants:
        slug = "-".join(variant)
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs[:4]


def _is_servicenow_story_url(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    return (
        parsed.scheme in {"http", "https"}
        and (hostname == "servicenow.com" or hostname.endswith(".servicenow.com"))
        and "/customers/" in path
        and path.endswith(".html")
    )


def _plain_text(content: str) -> str:
    without_markup = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", content, flags=re.I | re.S)
    without_markup = re.sub(r"<[^>]+>", " ", without_markup)
    return " ".join(html.unescape(without_markup).casefold().split())


def _page_matches_company(company_name: str, content: str) -> bool:
    text = _plain_text(content)
    if not any(marker in text for marker in _STORY_MARKERS):
        return False
    company_words = [word for word in _words(company_name) if word not in _GENERIC_WORDS]
    if not company_words:
        company_words = _words(company_name)
    distinctive = [word for word in company_words if len(word) >= 3]
    if not distinctive:
        return False
    original_first = re.findall(r"[A-Za-z0-9]+", company_name)
    if original_first and original_first[0].isupper() and 2 <= len(original_first[0]) <= 6:
        # Acronym brands commonly publish one global story while the resolved
        # employee entity includes a region or legal suffix (SKF India, ABB Ltd).
        return bool(re.search(rf"\b{re.escape(original_first[0].casefold())}\b", text))
    # All distinctive brand words must occur for short names. For longer names,
    # two matching words are sufficient to tolerate regional/legal-name variants.
    matches = sum(bool(re.search(rf"\b{re.escape(word)}\b", text)) for word in distinctive)
    required = len(distinctive) if len(distinctive) <= 2 else 2
    return matches >= required


class ServiceNowCustomerPageVerifier:
    def __init__(self, *, timeout_seconds: float = 15.0, session: requests.Session | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def check(self, company_name: str, discovered_urls: Iterable[str] = ()) -> CustomerPageCheck:
        candidates: list[str] = []
        for url in discovered_urls:
            clean = str(url or "").strip()
            if _is_servicenow_story_url(clean) and clean not in candidates:
                candidates.append(clean)
        for slug in customer_story_slugs(company_name):
            for path_prefix in ("", "/in"):
                url = f"https://www.servicenow.com{path_prefix}/customers/{slug}.html"
                if url not in candidates:
                    candidates.append(url)

        checked: list[str] = []
        received_response = False
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ServiceNowCustomerVerifier/1.0)"}
        for candidate in candidates[:8]:
            checked.append(candidate)
            try:
                response = self.session.get(
                    candidate,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    allow_redirects=True,
                )
            except requests.RequestException:
                continue
            received_response = True
            final_url = str(response.url or candidate)
            if response.status_code != 200 or not _is_servicenow_story_url(final_url):
                continue
            if _page_matches_company(company_name, response.text):
                return CustomerPageCheck(True, final_url, tuple(checked))
        return CustomerPageCheck(False if received_response else None, "", tuple(checked))
