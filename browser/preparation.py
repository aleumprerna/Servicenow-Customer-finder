from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Callable

from playwright.async_api import (
    Browser,
    Frame,
    Locator,
    Playwright,
    expect,
)

from browser.connection import (
    CUSTOMER_INFORMATION_SECTION,
    CUSTOMER_NAME_RADIO,
    ENGAGEMENT_MANAGER_HINT,
    IMPLEMENTATION_FALLBACK,
    IMPLEMENTATION_RADIO,
    ConnectedServiceNow,
    FormNotFoundError,
    connect_to_servicenow,
    find_servicenow_context,
)


SELECT2_SEARCH_INPUT = (
    ".select2-drop-active input.select2-input, "
    ".select2-container--open input.select2-search__field"
)
SELECT2_RESULTS = ".select2-results li, .select2-results__option"
CUSTOMER_NAME_INPUT = 'input[name="customerSearchText"]'
CUSTOMER_COUNTRY_SELECT = 'select[name="customerSearchCountry"]'


@dataclass(frozen=True, slots=True)
class PreparationState:
    status: str
    detail: str
    tone: str


class PreparationError(RuntimeError):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step = step
        self.detail = detail


async def prepare_existing_session(
    playwright: Playwright,
    cdp_url: str,
    *,
    status_callback: Callable[[str, str, str], None] | None = None,
    login_timeout_seconds: float = 300.0,
    action_timeout_seconds: float = 20.0,
) -> ConnectedServiceNow:
    """Wait for manual login, prepare the same page, and stop on Customer Information."""

    timeout_ms = int(action_timeout_seconds * 1000)
    connection = await wait_for_authenticated_session(
        playwright,
        cdp_url,
        login_timeout_seconds=login_timeout_seconds,
        status_callback=status_callback,
    )
    if connection.page_kind == "partner_information":
        await _select_engagement_manager(connection.frame, timeout_ms, status_callback)
        await _select_implementation(connection.frame, timeout_ms, status_callback)
        connection = await _continue_to_customer_information(
            connection.browser, connection.frame, timeout_ms, status_callback
        )
    connection = await _ready_customer_name_search(connection, timeout_ms, status_callback)
    _publish(
        status_callback,
        PreparationState(
            status="Ready",
            detail="Customer Information is ready for the existing scraping workflow",
            tone="ready",
        ),
    )
    return connection


async def wait_for_authenticated_session(
    playwright: Playwright,
    cdp_url: str,
    *,
    login_timeout_seconds: float,
    status_callback: Callable[[str, str, str], None] | None = None,
) -> ConnectedServiceNow:
    deadline = asyncio.get_running_loop().time() + login_timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            connection = await connect_to_servicenow(playwright, cdp_url, allow_partner=True)
        except ConnectionError:
            _publish(
                status_callback,
                PreparationState(
                    status="Waiting for Login",
                    detail="Chrome is not connected yet. Open the existing debug-enabled Chrome window.",
                    tone="waiting",
                ),
            )
        except FormNotFoundError:
            _publish(
                status_callback,
                PreparationState(
                    status="Waiting for Login",
                    detail=(
                        "Waiting for a signed-in ServiceNow deployment registration page in the "
                        "existing Chrome session"
                    ),
                    tone="waiting",
                ),
            )
        else:
            detail = (
                "Authenticated Partner Information page detected"
                if connection.page_kind == "partner_information"
                else "Authenticated Customer Information page detected"
            )
            _publish(
                status_callback,
                PreparationState(status="Login Detected", detail=detail, tone="logged-in"),
            )
            return connection
        await asyncio.sleep(2.0)
    raise PreparationError(
        "Waiting for Login",
        f"Timed out after {int(login_timeout_seconds)} seconds waiting for a logged-in ServiceNow page.",
    )


async def _select_engagement_manager(
    frame: Frame,
    timeout_ms: int,
    status_callback: Callable[[str, str, str], None] | None,
) -> None:
    _publish(
        status_callback,
        PreparationState(
            status="Selecting Engagement Manager",
            detail="Selecting ritik.d from the visible Engagement Manager Select2 control",
            tone="working",
        ),
    )
    field_group = frame.locator(".form-group").filter(
        has=frame.get_by_text("Engagement Manager Name", exact=True)
    ).first
    control = await _visible_locator(
        (
            field_group.locator(".select2-container, .select2-selection"),
            frame.locator(
                '.select2-container:has-text("Select Engagement Manager"), '
                '.select2-selection:has-text("Select Engagement Manager")'
            ),
        ),
        timeout_ms,
    )
    search_input = frame.locator(SELECT2_SEARCH_INPUT).last
    if not await search_input.is_visible():
        await control.scroll_into_view_if_needed()
        await control.click()
    await expect(search_input).to_be_visible(timeout=timeout_ms)
    await search_input.fill("ritik.d")

    option = await _visible_locator(
        (
            frame.get_by_role("option", name=re.compile(r"\britik\.d\b", re.I)),
            frame.locator(SELECT2_RESULTS).filter(
                has_text=re.compile(r"\britik\.d\b", re.I)
            ),
        ),
        timeout_ms,
    )
    await option.click()

    # Verify the real Angular-bound field, not matching text elsewhere in the
    # page or in a stale Select2 result. ServiceNow uses a trailing space in this
    # field's name and stores the selected user's sys_id as its value.
    bound_input = field_group.locator('input[name="engManagerField "]').first
    await expect(bound_input).not_to_have_value("", timeout=timeout_ms)
    await expect(control).not_to_have_class(re.compile(r"\bselect2-default\b"), timeout=timeout_ms)


async def _select_implementation(
    frame: Frame,
    timeout_ms: int,
    status_callback: Callable[[str, str, str], None] | None,
) -> None:
    _publish(
        status_callback,
        PreparationState(
            status="Selecting Implementation",
            detail="Selecting the Implementation deployment category",
            tone="working",
        ),
    )
    radio = frame.locator(f"{IMPLEMENTATION_RADIO}, {IMPLEMENTATION_FALLBACK}").first
    await expect(radio).to_be_attached(timeout=timeout_ms)
    await radio.scroll_into_view_if_needed()

    label = frame.locator("label[for='0-implementation2']").first
    try:
        if await label.count():
            await label.click(force=True)
        else:
            await radio.check(force=True)
    except Exception as exc:
        raise PreparationError("Selecting Implementation", str(exc)) from exc

    await expect(radio).to_be_checked(timeout=timeout_ms)


async def _continue_to_customer_information(
    browser: Browser,
    frame: Frame,
    timeout_ms: int,
    status_callback: Callable[[str, str, str], None] | None,
) -> ConnectedServiceNow:
    _publish(
        status_callback,
        PreparationState(
            status="Navigating to Customer Search",
            detail="Continuing from Partner Information to Customer Information",
            tone="working",
        ),
    )
    button = frame.get_by_role("button", name="Continue").first
    await expect(button).to_be_visible(timeout=timeout_ms)
    await button.scroll_into_view_if_needed()
    await button.click()

    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        connection = await find_servicenow_context(browser, allow_partner=False)
        if connection:
            return connection
        await asyncio.sleep(0.25)
    raise PreparationError(
        "Navigating to Customer Search",
        "Continue was clicked, but Customer Information did not load in time.",
    )


async def _ready_customer_name_search(
    connection: ConnectedServiceNow,
    timeout_ms: int,
    status_callback: Callable[[str, str, str], None] | None,
) -> ConnectedServiceNow:
    _publish(
        status_callback,
        PreparationState(
            status="Navigating to Customer Search",
            detail="Switching the Customer Information page to search by customer name",
            tone="working",
        ),
    )
    frame = connection.frame
    section = frame.locator(CUSTOMER_INFORMATION_SECTION).first
    await expect(section).to_be_visible(timeout=timeout_ms)

    radio = frame.locator(CUSTOMER_NAME_RADIO).first
    await expect(radio).to_be_attached(timeout=timeout_ms)
    # ServiceNow's styled label overlays the native radio input.
    await radio.check(force=True)
    await expect(radio).to_be_checked(timeout=timeout_ms)

    customer_input = frame.locator(CUSTOMER_NAME_INPUT).first
    country_select = frame.locator(CUSTOMER_COUNTRY_SELECT).first
    # The name input intentionally remains disabled until the per-company
    # country is selected by ServiceNowChecker._select_country().
    await expect(customer_input).to_be_attached(timeout=timeout_ms)
    await expect(country_select).to_be_attached(timeout=timeout_ms)
    return connection


async def _visible_locator(candidates: tuple[Locator, ...], timeout_ms: int) -> Locator:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        for locator in candidates:
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(count):
                item = locator.nth(index)
                try:
                    if await item.is_visible():
                        return item
                except Exception:
                    continue
        await asyncio.sleep(0.2)
    raise PreparationError(
        "Selecting Engagement Manager",
        "Could not find the visible Select Engagement Manager control.",
    )


def _publish(
    status_callback: Callable[[str, str, str], None] | None, state: PreparationState
) -> None:
    if status_callback is not None:
        status_callback(state.status, state.detail, state.tone)
