from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Browser, Frame, Locator, Page, Playwright


CUSTOMER_NAME_RADIO = 'input[name="customer-search-criteria"][value="customer_name"]'
CUSTOMER_INFORMATION_SECTION = "#customer-information"
IMPLEMENTATION_RADIO = 'input[name="u_deployment_category"][value="implementation2"]'
# CSS IDs beginning with a digit must be escaped. An attribute selector stays
# readable and works in both Playwright and the browser's querySelectorAll.
IMPLEMENTATION_FALLBACK = '[id="0-implementation2"]'
ENGAGEMENT_MANAGER_HINT = "Select Engagement Manager"


class FormNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConnectedServiceNow:
    browser: Browser
    page: Page
    frame: Frame
    page_kind: str = "customer_information"


async def _frame_has_customer_form(frame: Frame) -> bool:
    try:
        radio = frame.locator(CUSTOMER_NAME_RADIO)
        heading = frame.get_by_text("Customer Information", exact=True)
        section = frame.locator(CUSTOMER_INFORMATION_SECTION)
        # The Angular form renders later wizard steps into the DOM while they are
        # still hidden. Counting those controls therefore reports Customer
        # Information too early and makes navigation continue against a hidden
        # section. Treat a step as active only when its identifying DOM is visible.
        return await radio.count() > 0 and (
            await _any_visible(heading) or await _any_visible(section)
        )
    except Exception:
        return False


async def _frame_has_partner_form(frame: Frame) -> bool:
    try:
        implementation = frame.locator(
            f"{IMPLEMENTATION_RADIO}, {IMPLEMENTATION_FALLBACK}"
        )
        manager = frame.get_by_text(ENGAGEMENT_MANAGER_HINT, exact=False)
        heading = frame.get_by_text("Partner Information", exact=True)
        return await implementation.count() > 0 and (
            await _any_visible(manager) or await _any_visible(heading)
        )
    except Exception:
        return False


async def _any_visible(locator: Locator) -> bool:
    """Return whether any item in a Playwright locator is currently visible."""

    count = await locator.count()
    for index in range(count):
        if await locator.nth(index).is_visible():
            return True
    return False


async def find_servicenow_form(browser: Browser) -> tuple[Page, Frame] | None:
    """Return the authenticated automation page and frame, if currently present."""

    for context in browser.contexts:
        for page in context.pages:
            if page.is_closed():
                continue
            for frame in page.frames:
                if await _frame_has_customer_form(frame):
                    return page, frame
    return None


async def find_servicenow_context(
    browser: Browser, *, allow_partner: bool = False
) -> ConnectedServiceNow | None:
    """Return the current authenticated ServiceNow frame on either supported step."""

    for context in browser.contexts:
        for page in context.pages:
            if page.is_closed():
                continue
            for frame in page.frames:
                if await _frame_has_customer_form(frame):
                    return ConnectedServiceNow(
                        browser=browser,
                        page=page,
                        frame=frame,
                        page_kind="customer_information",
                    )
    if not allow_partner:
        return None
    for context in browser.contexts:
        for page in context.pages:
            if page.is_closed():
                continue
            for frame in page.frames:
                if await _frame_has_partner_form(frame):
                    return ConnectedServiceNow(
                        browser=browser,
                        page=page,
                        frame=frame,
                        page_kind="partner_information",
                    )
    return None


async def connect_to_servicenow(
    playwright: Playwright, cdp_url: str, *, allow_partner: bool = False
) -> ConnectedServiceNow:
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=15_000)
    except Exception as exc:
        raise ConnectionError(
            f"Could not connect to Chrome at {cdp_url}. Start Chrome with remote debugging enabled."
        ) from exc

    match = await find_servicenow_context(browser, allow_partner=allow_partner)
    if match:
        return match

    if allow_partner:
        raise FormNotFoundError(
            "ServiceNow deployment registration was not found. Open the Partner Information or "
            "Customer Information page in the Chrome instance started with remote debugging and "
            "run the script again."
        )
    raise FormNotFoundError(
        "ServiceNow Customer Information form was not found. Open the required page in the "
        "Chrome instance started with remote debugging and run the script again."
    )
