from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_service.browser import servicenow
from playwright_service.models import CheckStatus


class Locator:
    def __init__(self, frame, kind):
        self.frame = frame
        self.kind = kind

    @property
    def first(self):
        return self

    def nth(self, _index):
        return self

    async def count(self):
        return len(self.frame.names) if self.kind == "results" else 1

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def is_checked(self):
        return True

    async def fill(self, value):
        self.frame.actions.append(("fill", value))

    async def press(self, key):
        self.frame.actions.append(("press", key))

    async def select_option(self, *, value):
        self.frame.country = value
        self.frame.actions.append(("country", value))

    async def input_value(self):
        return self.frame.country

    async def click(self):
        self.frame.actions.append(("click", self.kind))
        self.frame.searched = True
        self.frame.names = [] if self.frame.no_results else ["Adobe Inc."]

    async def inner_text(self):
        if self.kind == "body":
            return "Customer\n Information: " + (
                "No customers found" if self.frame.searched and self.frame.no_results
                else "\n".join(self.frame.names)
            )
        return "Adobe\nInc."

    async def get_attribute(self, _attribute):
        return None

    def locator(self, _selector):
        return Locator(self.frame, "name")


class Frame:
    def __init__(self, no_results):
        self.no_results = no_results
        self.searched = False
        self.names = []
        self.actions = []
        self.country = ""

    def locator(self, selector):
        kinds = {
            servicenow.SELECTORS["customer_name_radio"]: "radio",
            servicenow.SELECTORS["customer_name"]: "input",
            servicenow.SELECTORS["country_select"]: "country",
            "body": "body",
            ".customer-name": "results",
        }
        return Locator(self, kinds[selector])

    def get_by_role(self, role, *, name, exact):
        assert (role, name, exact) == ("button", "Search", True)
        return Locator(self, "search")


class Expectations:
    def __init__(self, locator):
        self.locator = locator

    async def to_be_visible(self, **_kwargs):
        assert await self.locator.is_visible()

    async def to_be_enabled(self, **_kwargs):
        assert await self.locator.is_enabled()

    async def to_be_attached(self, **_kwargs):
        assert await self.locator.count()


@pytest.mark.asyncio
@pytest.mark.parametrize("no_results,customer", [(False, "Yes"), (True, "No")])
async def test_search_clicks_button_after_filling_and_reads_results(monkeypatch, no_results, customer):
    # Exercise the actual search, body normalization, name extraction, and result
    # waiting code. Stub only browser locators and Playwright assertions.
    monkeypatch.setattr(servicenow, "expect", Expectations)
    frame = Frame(no_results)
    checker = servicenow.ServiceNowChecker(
        page=SimpleNamespace(is_closed=lambda: False), frame=frame,
        timeout_seconds=1, match_threshold=85, review_threshold=70,
        save_screenshots=False, debug_dir=Path("debug"), result_selectors=(".customer-name",),
    )
    result = await checker.search_with_retry("Adobe Inc.", "US")
    assert frame.actions.index(("fill", "Adobe")) < frame.actions.index(("click", "search"))
    assert ("country", "string:US") in frame.actions
    assert result.customer == customer
    assert result.status == CheckStatus.COMPLETED
    assert result.returned_names == (() if no_results else ("Adobe Inc.",))
