from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Browser, Frame, Page, Playwright


FORM_RADIO = 'input[name="customer-search-criteria"][value="customer_name"]'


class FormNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConnectedServiceNow:
    browser: Browser
    page: Page
    frame: Frame


async def _frame_has_form(frame: Frame) -> bool:
    try:
        radio_count = await frame.locator(FORM_RADIO).count()
        heading_count = await frame.get_by_text("Customer Information", exact=True).count()
        return radio_count > 0 and heading_count > 0
    except Exception:
        return False


async def connect_to_servicenow(playwright: Playwright, cdp_url: str) -> ConnectedServiceNow:
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=15_000)
    except Exception as exc:
        raise ConnectionError(
            f"Could not connect to Chrome at {cdp_url}. Start Chrome with remote debugging enabled."
        ) from exc

    for context in browser.contexts:
        for page in context.pages:
            for frame in page.frames:
                if await _frame_has_form(frame):
                    return ConnectedServiceNow(browser=browser, page=page, frame=frame)

    raise FormNotFoundError(
        "ServiceNow Customer Information form was not found. Open the required page in the "
        "Chrome instance started with remote debugging and run the script again."
    )
