import pytest

from browser.connection import _frame_has_customer_form, _frame_has_partner_form


class FakeLocator:
    def __init__(self, *visibility: bool) -> None:
        self.visibility = visibility
        self.index = 0

    async def count(self) -> int:
        return len(self.visibility)

    def nth(self, index: int) -> "FakeLocator":
        item = FakeLocator(*self.visibility)
        item.index = index
        return item

    async def is_visible(self) -> bool:
        return self.visibility[self.index]


class FakeFrame:
    def __init__(
        self,
        *,
        customer_radio: tuple[bool, ...] = (),
        customer_heading: tuple[bool, ...] = (),
        customer_section: tuple[bool, ...] = (),
        implementation_radio: tuple[bool, ...] = (),
        manager_hint: tuple[bool, ...] = (),
        partner_heading: tuple[bool, ...] = (),
    ) -> None:
        self.locators = {
            'input[name="customer-search-criteria"][value="customer_name"]': FakeLocator(
                *customer_radio
            ),
            "#customer-information": FakeLocator(*customer_section),
            'input[name="u_deployment_category"][value="implementation2"], [id="0-implementation2"]': FakeLocator(
                *implementation_radio
            ),
        }
        self.text = {
            "Customer Information": FakeLocator(*customer_heading),
            "Select Engagement Manager": FakeLocator(*manager_hint),
            "Partner Information": FakeLocator(*partner_heading),
        }

    def locator(self, selector: str) -> FakeLocator:
        return self.locators[selector]

    def get_by_text(self, text: str, *, exact: bool) -> FakeLocator:
        return self.text[text]


@pytest.mark.asyncio
async def test_hidden_customer_step_is_not_detected_as_active() -> None:
    frame = FakeFrame(
        customer_radio=(False,),
        customer_heading=(False,),
        customer_section=(False,),
        implementation_radio=(True,),
        manager_hint=(True,),
        partner_heading=(True,),
    )

    assert await _frame_has_customer_form(frame) is False
    assert await _frame_has_partner_form(frame) is True


@pytest.mark.asyncio
async def test_visible_customer_step_is_detected() -> None:
    frame = FakeFrame(
        customer_radio=(True,),
        customer_heading=(True,),
        customer_section=(True,),
    )

    assert await _frame_has_customer_form(frame) is True
