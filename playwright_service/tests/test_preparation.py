from types import SimpleNamespace

import pytest

from playwright_service.browser import preparation


@pytest.mark.asyncio
@pytest.mark.parametrize("page_kind", ["partner_information", "customer_information"])
async def test_first_automation_prepares_partner_or_existing_customer_page(monkeypatch, page_kind):
    connection = SimpleNamespace(page_kind=page_kind, frame=object(), browser=object())
    ready_connection = SimpleNamespace(page_kind="customer_information", frame=connection.frame)
    actions = []

    async def authenticated(*_args, **_kwargs):
        return connection

    async def manager(*_args):
        actions.append("ritik.d")

    async def implementation(*_args):
        actions.append("Implementation")

    async def continue_page(*_args):
        actions.append("Continue")
        return ready_connection

    async def ready(*_args):
        actions.append("customer_name_search")
        return ready_connection

    monkeypatch.setattr(preparation, "wait_for_authenticated_session", authenticated)
    monkeypatch.setattr(preparation, "_select_engagement_manager", manager)
    monkeypatch.setattr(preparation, "_select_implementation", implementation)
    monkeypatch.setattr(preparation, "_continue_to_customer_information", continue_page)
    monkeypatch.setattr(preparation, "_ready_customer_name_search", ready)
    result = await preparation.prepare_existing_session(object(), "http://localhost:9222")
    assert result.page_kind == "customer_information"
    assert actions == (
        ["ritik.d", "Implementation", "Continue", "customer_name_search"]
        if page_kind == "partner_information" else ["customer_name_search"]
    )
