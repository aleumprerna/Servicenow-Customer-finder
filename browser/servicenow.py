from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from playwright.async_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeoutError, expect

from models.company import CheckStatus, SearchResult
from services.company_matcher import find_best_match
from services.country_normalizer import country_name, servicenow_country_value


LOGGER = logging.getLogger(__name__)

SELECTORS = {
    "customer_name_radio": 'input[name="customer-search-criteria"][value="customer_name"]',
    "country_select": 'select[name="customerSearchCountry"]',
    "customer_name": 'input[name="customerSearchText"]',
    "search_button": "button.search-btn",
}

# Keep site-specific result selectors here. Override them without editing code by
# setting SERVICENOW_RESULT_SELECTORS to a JSON array in .env.
DEFAULT_RESULT_SELECTORS = (
    'tr:has(input[name="customer-selection"]) [ng-bind-html="customer.partyNm"]',
    'tr:has(input[name="customer-selection"]) .account-name',
    'tr:has(input[name="customer-selection"])',
    '[data-testid="customer-search-results"] [data-customer-name]',
    '[data-testid="customer-search-results"] [data-testid="customer-name"]',
    '[aria-label="Customer search results"] [data-customer-name]',
    ".customer-search-results .customer-name",
    "table.customer-search-results tbody tr",
)

NO_RESULTS_PATTERNS = (
    r"\bno customers? found\b",
    r"\bcannot find (?:an? )?(?:account|customer)\b",
    r"\bno results(?: found)?\b",
    r"\b0 customers? found\b",
)
TECHNICAL_ERROR_PATTERNS = (
    r"\brequest timed out\b",
    r"\bsomething went wrong\b",
    r"\bservice unavailable\b",
    r"\binternal server error\b",
)


class SessionExpiredError(RuntimeError):
    pass


class SearchTechnicalError(RuntimeError):
    pass


def safe_filename(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip()).strip("_").lower()
    return (normalized or "company")[:80]


class ServiceNowChecker:
    def __init__(
        self,
        *,
        page: Page,
        frame: Frame,
        timeout_seconds: float,
        match_threshold: int,
        review_threshold: int,
        save_screenshots: bool,
        debug_dir: Path,
        result_selectors: tuple[str, ...] = (),
    ) -> None:
        self.page = page
        self.frame = frame
        self.timeout_ms = int(timeout_seconds * 1000)
        self.match_threshold = match_threshold
        self.review_threshold = review_threshold
        self.save_screenshots = save_screenshots
        self.debug_dir = debug_dir
        self.result_selectors = result_selectors or DEFAULT_RESULT_SELECTORS

    async def assert_session_active(self) -> None:
        if self.page.is_closed():
            raise SessionExpiredError("The ServiceNow browser page was closed")
        try:
            form_count = await self.frame.locator(SELECTORS["customer_name_radio"]).count()
        except Exception as exc:
            raise SessionExpiredError("The ServiceNow form frame was detached or replaced") from exc
        if form_count == 0:
            raise SessionExpiredError(
                "ServiceNow session appears to have expired. Please login again and rerun the application. "
                "Already completed companies have been saved."
            )

    async def reset_customer_form(self) -> None:
        await self.assert_session_active()
        customer_input = self.frame.locator(SELECTORS["customer_name"]).first
        if await customer_input.count() and await customer_input.is_enabled():
            await customer_input.fill("")
            await customer_input.press("Escape")

    async def search_with_retry(self, company_name: str, country_code: str) -> SearchResult:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return await self._search_once(company_name, country_code)
            except (PlaywrightTimeoutError, SearchTechnicalError) as exc:
                last_error = exc
                if attempt == 0:
                    LOGGER.warning("Temporary ServiceNow search failure; making one controlled retry")
                    await self.reset_customer_form()
                    continue
        if self.save_screenshots:
            await self._save_screenshot(company_name, "error")
        raise SearchTechnicalError(str(last_error or "ServiceNow search failed"))

    async def _search_once(self, company_name: str, country_code: str) -> SearchResult:
        await self.assert_session_active()
        await self.reset_customer_form()
        radio = self.frame.locator(SELECTORS["customer_name_radio"]).first
        await expect(radio).to_be_visible(timeout=self.timeout_ms)
        if not await radio.is_checked():
            await radio.check()

        await self._select_country(country_code)
        customer_input = self.frame.locator(SELECTORS["customer_name"]).first
        await expect(customer_input).to_be_enabled(timeout=self.timeout_ms)
        await customer_input.fill(company_name)

        if self.save_screenshots:
            await self._save_screenshot(company_name, "before_search")

        before_text = await self._body_text()
        before_names = await self._extract_result_names()
        button = await self._search_button()
        await expect(button).to_be_enabled(timeout=self.timeout_ms)
        await button.click()

        outcome, names = await self._wait_for_result(before_text, before_names, button)
        if self.save_screenshots:
            await self._save_screenshot(company_name, "results")

        if outcome == "technical_error":
            raise SearchTechnicalError("ServiceNow displayed a technical error after search")
        if outcome == "no_results":
            return SearchResult(customer="No", status=CheckStatus.COMPLETED)
        if outcome == "unknown":
            await self._save_debug_artifacts(company_name, "results_unknown")
            return SearchResult(
                customer="Unknown",
                status=CheckStatus.MANUAL_REVIEW,
                error_message=(
                    "Search completed, but no configured result selector matched. "
                    "Review saved HTML/screenshot and configure SERVICENOW_RESULT_SELECTORS."
                ),
            )

        LOGGER.info("ServiceNow returned %d candidate(s): %s", len(names), "; ".join(names))
        best = find_best_match(company_name, names)
        return SearchResult(
            customer="Yes",
            matched_name=best.name,
            match_score=best.score,
            status=CheckStatus.COMPLETED,
            returned_names=tuple(names),
        )

    async def _select_country(self, country_code: str) -> None:
        select = self.frame.locator(SELECTORS["country_select"]).first
        await expect(select).to_be_attached(timeout=self.timeout_ms)
        option_value = servicenow_country_value(country_code)
        label = f"{country_code.upper()} - {country_name(country_code)}"
        try:
            await select.select_option(value=option_value)
            if await self._native_country_selection_is_reflected(select, country_code):
                return
        except Exception as exc:
            LOGGER.debug("Native country selection failed: %s", exc)

        await self._select_country_with_select2(select, label, country_code)
        if not await self._native_country_selection_is_reflected(select, country_code):
            raise SearchTechnicalError(f"Country selection failed for {label}")

    async def _native_country_selection_is_reflected(
        self, select: Locator, country_code: str
    ) -> bool:
        if await select.input_value() != servicenow_country_value(country_code):
            return False
        customer_input = self.frame.locator(SELECTORS["customer_name"]).first
        try:
            await expect(customer_input).to_be_enabled(timeout=min(self.timeout_ms, 3000))
        except PlaywrightTimeoutError:
            return False

        return True

    async def _select_country_with_select2(
        self, select: Locator, label: str, country_code: str
    ) -> None:
        parent = select.locator("xpath=..")
        container = parent.locator(".select2-container").first
        if await container.count() == 0:
            container = select.locator("xpath=following-sibling::*[contains(@class, 'select2-container')][1]")
        if await container.count() == 0:
            raise SearchTechnicalError("Could not locate the visible Select2 country control")
        await container.click()

        search_input = self.frame.locator(
            ".select2-drop-active input.select2-input, .select2-container--open input.select2-search__field"
        ).last
        await expect(search_input).to_be_visible(timeout=self.timeout_ms)
        await search_input.fill(label)

        visible_options = self.frame.locator(".select2-results li, .select2-results__option")
        exact_option = visible_options.filter(
            has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
        ).first
        if await exact_option.count() == 0:
            exact_option = visible_options.filter(
                has_text=re.compile(rf"^\s*{re.escape(country_code)}\s*-", re.I)
            ).first
        await expect(exact_option).to_be_visible(timeout=self.timeout_ms)
        await exact_option.click()

    async def _search_button(self) -> Locator:
        by_role = self.frame.get_by_role("button", name="Search", exact=True)
        if await by_role.count():
            return by_role.first
        return self.frame.locator(SELECTORS["search_button"]).first

    async def _wait_for_result(
        self, before_text: str, before_names: list[str], button: Locator
    ) -> tuple[str, list[str]]:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + (self.timeout_ms / 1000)
        last_text = before_text
        stable_polls = 0
        saw_text_change = False
        seen_busy = False
        before_had_no_results = any(
            re.search(pattern, before_text, re.I) for pattern in NO_RESULTS_PATTERNS
        )
        before_had_technical_error = any(
            re.search(pattern, before_text, re.I) for pattern in TECHNICAL_ERROR_PATTERNS
        )
        while loop.time() < deadline:
            await self.assert_session_active()
            try:
                if not await button.is_enabled():
                    seen_busy = True
            except Exception:
                pass
            button_ready = await button.is_enabled()
            lifecycle_completed = seen_busy and button_ready
            names = await self._extract_result_names()
            if names and (names != before_names or lifecycle_completed):
                return "results", names
            text = await self._body_text()
            has_technical_error = any(
                re.search(pattern, text, re.I) for pattern in TECHNICAL_ERROR_PATTERNS
            )
            if has_technical_error and (not before_had_technical_error or lifecycle_completed):
                return "technical_error", []
            has_no_results = any(
                re.search(pattern, text, re.I) for pattern in NO_RESULTS_PATTERNS
            )
            if has_no_results and (not before_had_no_results or lifecycle_completed):
                return "no_results", []

            if text != before_text:
                saw_text_change = True
            if text == last_text:
                stable_polls += 1
            else:
                stable_polls = 0
            last_text = text

            # The ServiceNow page does not always show a no-results message.
            # If the post-search DOM settles with no customer rows, treat it as no match.
            elapsed = loop.time() - started_at
            if saw_text_change and stable_polls >= 4 and elapsed >= 3.0 and button_ready:
                return "no_results", []
            await asyncio.sleep(0.25)
        raise SearchTechnicalError("Customer search request timed out")

    async def _extract_result_names(self) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for selector in self.result_selectors:
            locator = self.frame.locator(selector)
            count = min(await locator.count(), 100)
            for index in range(count):
                item = locator.nth(index)
                if not await item.is_visible():
                    continue
                text = await self._result_item_name(item)
                key = text.casefold()
                if text and key not in seen:
                    seen.add(key)
                    names.append(text)
            if names:
                break
        return names

    async def _result_item_name(self, item: Locator) -> str:
        explicit_name = await item.get_attribute("data-customer-name")
        if explicit_name:
            return re.sub(r"\s+", " ", explicit_name).strip()

        name_locator = item.locator(
            '[ng-bind-html="customer.partyNm"], .account-name span, .account-name'
        ).first
        if await name_locator.count():
            name_text = await name_locator.inner_text()
            if name_text.strip():
                return re.sub(r"\s+", " ", name_text).strip()

        return re.sub(r"\s+", " ", await item.inner_text()).strip()

    async def _body_text(self) -> str:
        return re.sub(r"\s+", " ", await self.frame.locator("body").inner_text()).strip()

    async def _save_screenshot(self, company_name: str, suffix: str) -> None:
        try:
            directory = self.debug_dir / "screenshots"
            directory.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(
                path=directory / f"{safe_filename(company_name)}_{suffix}.png", full_page=True
            )
        except Exception as exc:
            LOGGER.warning("Could not save debug screenshot: %s", exc)

    async def _save_debug_artifacts(self, company_name: str, suffix: str) -> None:
        await self._save_screenshot(company_name, suffix)
        try:
            directory = self.debug_dir / "html"
            directory.mkdir(parents=True, exist_ok=True)
            html = await self.frame.locator("html").evaluate("element => element.outerHTML")
            (directory / f"{safe_filename(company_name)}_{suffix}.html").write_text(
                html, encoding="utf-8"
            )
        except Exception as exc:
            LOGGER.warning("Could not save debug HTML: %s", exc)
