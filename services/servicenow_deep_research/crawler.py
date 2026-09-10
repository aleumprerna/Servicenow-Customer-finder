from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from .evidence_extractor import evidence_snippets
from .schemas import EvidenceFinding


LOGGER = logging.getLogger(__name__)
Resolver = Callable[..., Iterable[tuple]]


class UnsafeResearchTarget(ValueError):
    """Raised when a stored company domain is not safe to request."""


def normalize_domain(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise UnsafeResearchTarget("The company does not have an official website domain.")
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeResearchTarget("Only a public HTTP or HTTPS company domain can be researched.")
    try:
        custom_port = parsed.port
    except ValueError as exc:
        raise UnsafeResearchTarget("The company domain contains an invalid port.") from exc
    if parsed.username or parsed.password or custom_port:
        raise UnsafeResearchTarget("Company domains cannot contain credentials or a custom port.")
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeResearchTarget("The company domain is not valid.") from exc
    if not hostname or "." not in hostname:
        raise UnsafeResearchTarget("A public company domain is required.")
    return hostname


def validate_public_domain(domain: str, resolver: Resolver = socket.getaddrinfo) -> None:
    hostname = normalize_domain(domain)
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal", ".lan")):
        raise UnsafeResearchTarget("Local or internal company domains are not allowed.")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise UnsafeResearchTarget("Private or reserved network addresses are not allowed.")
    try:
        addresses = {item[4][0] for item in resolver(hostname, 443, type=socket.SOCK_STREAM)}
    except (OSError, socket.gaierror) as exc:
        raise UnsafeResearchTarget("The official company domain could not be resolved.") from exc
    if not addresses:
        raise UnsafeResearchTarget("The official company domain could not be resolved.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeResearchTarget("The company domain resolved to an invalid address.") from exc
        if not ip.is_global:
            raise UnsafeResearchTarget("The company domain resolves to a private or reserved network.")


def is_same_site(url: str, domain: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in {"http", "https"} and (host == domain or host.endswith(f".{domain}"))


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._in_title = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        clean = " ".join(data.split())
        if not clean or self._ignored_depth:
            return
        self.text_parts.append(clean)
        if self._in_title:
            self.title_parts.append(clean)


@dataclass(frozen=True)
class CrawlReport:
    findings: list[EvidenceFinding]
    sources_checked: int
    discovered_urls: list[str]
    visited_urls: list[str] = field(default_factory=list)


class BoundedOfficialCrawler:
    """Small same-site crawler with strict URL, byte, page, and time limits."""

    PRIORITY_TERMS = (
        "servicenow", "service-now", "career", "job", "technology", "digital", "platform",
        "press", "news", "investor", "annual", "report", "procurement", "supplier", "case-study",
    )

    def __init__(
        self,
        *,
        max_pages: int = 12,
        page_timeout_seconds: float = 8.0,
        max_content_chars: int = 250_000,
        max_elapsed_seconds: float = 45.0,
        resolver: Resolver = socket.getaddrinfo,
        session: requests.Session | None = None,
    ) -> None:
        self.max_pages = max(1, min(int(max_pages), 40))
        self.page_timeout_seconds = max(1.0, float(page_timeout_seconds))
        self.max_content_chars = max(10_000, int(max_content_chars))
        self.max_elapsed_seconds = max(5.0, float(max_elapsed_seconds))
        self.resolver = resolver
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": "ServiceNowCustomerResearch/1.0 (+bounded official-site verification)"})

    def _get(self, url: str, domain: str) -> requests.Response:
        current = _canonical_url(url)
        for _ in range(4):
            if not is_same_site(current, domain):
                raise UnsafeResearchTarget("A company page redirected outside its official domain.")
            validate_public_domain(urlsplit(current).hostname or "", self.resolver)
            response = self.session.get(
                current,
                timeout=self.page_timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise requests.RequestException("Redirect response did not include a location")
            current = _canonical_url(urljoin(current, location))
        raise requests.TooManyRedirects("Official page exceeded the redirect limit")

    def _read_limited(self, response: requests.Response) -> str:
        chunks: list[bytes] = []
        total = 0
        byte_limit = min(self.max_content_chars * 4, 1_000_000)
        for chunk in response.iter_content(chunk_size=16_384):
            if not chunk:
                continue
            total += len(chunk)
            if total > byte_limit:
                break
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")[: self.max_content_chars]

    @staticmethod
    def _priority(url: str) -> tuple[int, int]:
        path = urlsplit(url).path.casefold()
        return (0 if any(term in path for term in BoundedOfficialCrawler.PRIORITY_TERMS) else 1, len(path))

    def crawl(self, official_domain: str) -> CrawlReport:
        domain = normalize_domain(official_domain)
        validate_public_domain(domain, self.resolver)
        seeds = [
            f"https://{domain}/",
            f"https://{domain}/robots.txt",
            f"https://{domain}/sitemap.xml",
        ]
        if not domain.startswith("www."):
            seeds.append(f"https://www.{domain}/")
        queue: deque[str] = deque(seeds)
        queued = {_canonical_url(item) for item in seeds}
        visited: set[str] = set()
        visited_urls: list[str] = []
        findings: list[EvidenceFinding] = []
        discovered: list[str] = []
        analyzed_pages = 0
        started = time.monotonic()

        while queue and len(visited) < self.max_pages:
            if time.monotonic() - started >= self.max_elapsed_seconds:
                LOGGER.info("Deep research crawl reached its elapsed-time limit", extra={"domain": domain})
                break
            url = _canonical_url(queue.popleft())
            if url in visited or not is_same_site(url, domain):
                continue
            visited.add(url)
            visited_urls.append(url)
            try:
                response = self._get(url, domain)
                with response:
                    if response.status_code >= 400:
                        continue
                    content_type = response.headers.get("Content-Type", "").casefold()
                    if not any(kind in content_type for kind in ("html", "text/plain", "xml")):
                        continue
                    body = self._read_limited(response)
                    final_url = _canonical_url(response.url or url)
            except (requests.RequestException, UnsafeResearchTarget) as exc:
                LOGGER.info("Deep research skipped official page %s: %s", url, exc)
                continue

            path = urlsplit(final_url).path.casefold()
            discovery_document = path.endswith(("robots.txt", ".xml")) or "xml" in content_type
            discovered_from_document: list[str] = []
            if path.endswith("robots.txt"):
                discovered_from_document.extend(
                    line.split(":", 1)[1].strip()
                    for line in body.splitlines()
                    if line.casefold().startswith("sitemap:") and ":" in line
                )
            elif discovery_document:
                discovered_from_document.extend(
                    match.strip() for match in re.findall(r"<loc[^>]*>(.*?)</loc>", body, re.I | re.S)
                )

            parser = _PageParser()
            try:
                parser.feed(body)
            except Exception:
                LOGGER.info("Deep research could not parse official page %s", url)
                continue
            title = " ".join(parser.title_parts)[:300] or final_url
            page_text = " ".join(parser.text_parts)
            analyzed_pages += 1
            if not discovery_document:
                findings.extend(
                    evidence_snippets(
                        page_text,
                        url=final_url,
                        page_title=title,
                        official_domain=domain,
                        citation_grounded=True,
                    )
                )
            candidates: list[str] = []
            for href in [*discovered_from_document, *parser.links]:
                candidate = _canonical_url(urljoin(final_url, href))
                if is_same_site(candidate, domain) and candidate not in visited and candidate not in queued:
                    candidates.append(candidate)
                    queued.add(candidate)
                    discovered.append(candidate)
            for candidate in sorted(candidates, key=self._priority)[: self.max_pages * 20]:
                queue.append(candidate)

        LOGGER.info(
            "Deep research crawl for %s discovered %s URLs, analyzed %s pages, and extracted %s findings",
            domain,
            len(discovered),
            analyzed_pages,
            len(findings),
        )
        return CrawlReport(
            findings=findings,
            sources_checked=len(visited),
            discovered_urls=discovered,
            visited_urls=visited_urls,
        )
