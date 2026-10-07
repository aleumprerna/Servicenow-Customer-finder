from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from playwright_service.api import create_app
from playwright_service.browser.connection import FormNotFoundError
from playwright_service.browser.errors import PreparationError
from playwright_service.config import AutomationSettings
from playwright_service.schemas import CompanyResponse, PreparationResponse


@pytest.fixture
def client():
    with TestClient(create_app(AutomationSettings(delay_between_companies_seconds=0))) as test_client:
        yield test_client


def company(**overrides):
    return {"company_name": "Adobe", "headquarters": "San Jose, California, United States", **overrides}


def test_batch_waits_for_automation_and_returns_normalized_results(client, monkeypatch):
    calls = []

    async def check(companies):
        calls.extend(companies)
        return [CompanyResponse(
            **item.model_dump(), servicenow_customer="No", check_status="completed",
            checked_at=datetime.now(timezone.utc),
        ) for item in companies]

    monkeypatch.setattr(client.app.state.automation, "check_companies", check)
    response = client.post("/api/check-companies", json={"companies": [
        company(), company(company_name="Example Ltd", headquarters="London", country_code="UK"),
    ]})
    assert response.status_code == 200
    data = response.json()
    assert len(calls) == 2
    assert [row.country_code for row in calls] == ["US", "GB"]
    assert data["success"] is True
    assert data["completed"] == data["total"] == 2
    assert data["errors"] == data["manual_review"] == 0
    assert [row["company_name"] for row in data["results"]] == ["Adobe", "Example Ltd"]
    assert data["results"][0]["match_score"] is None
    assert client.get("/health").json() == {"status": "ok", "busy": False}


@pytest.mark.parametrize("companies", [
    [], [company(company_name="   ")], [company(headquarters="San Jose")],
    [company(country_code="ZZ")], [company(country_code="ZZ - Nowhere")],
    [company(country="India", country_code="US")], [company(unexpected="value")],
    [company()] * 101,
])
def test_invalid_inputs_never_start_browser(client, monkeypatch, companies):
    async def unexpected(_companies):
        pytest.fail("Invalid input must not trigger browser automation")

    monkeypatch.setattr(client.app.state.automation, "check_companies", unexpected)
    assert client.post("/api/check-companies", json={"companies": companies}).status_code == 422


@pytest.mark.parametrize("error,status", [
    (ConnectionError("Chrome unavailable"), 503),
    (PermissionError("Playwright driver was blocked"), 503),
    (FormNotFoundError("Please log in"), 409),
    (PreparationError("Waiting for Login", "Login timed out"), 409),
])
def test_session_setup_errors_are_http_errors_and_release_lock(client, monkeypatch, error, status):
    async def fail(_companies):
        raise error

    monkeypatch.setattr(client.app.state.automation, "check_companies", fail)
    response = client.post("/api/check-companies", json={"companies": [company()]})
    assert response.status_code == status
    assert "detail" in response.json()
    assert not client.app.state.browser_lock.locked()


def test_busy_browser_rejects_second_request_without_automation(client, monkeypatch):
    async def unexpected(_companies):
        pytest.fail("Busy browser must not receive another batch")

    async def acquire():
        await client.app.state.browser_lock.acquire()

    monkeypatch.setattr(client.app.state.automation, "check_companies", unexpected)
    client.portal.call(acquire)
    try:
        assert client.get("/health").json()["busy"] is True
        assert client.post("/api/check-companies", json={"companies": [company()]}).status_code == 409
        assert client.get("/api/session").status_code == 409
        assert client.post("/api/prepare").status_code == 409
    finally:
        client.portal.call(client.app.state.browser_lock.release)


def test_session_endpoint_checks_readiness(client, monkeypatch):
    async def inspect():
        return {"browser_connected": True, "authenticated": True, "page_kind": "partner_information"}

    monkeypatch.setattr(client.app.state.automation, "inspect_session", inspect)
    assert client.get("/api/session").json()["page_kind"] == "partner_information"
    assert "/api/check-companies" in client.get("/openapi.json").json()["paths"]


def test_prepare_endpoint_needs_no_companies_and_does_not_search(client, monkeypatch):
    calls = []

    async def prepare():
        calls.append("prepare")
        return PreparationResponse()

    async def unexpected(_companies):
        pytest.fail("Preparation must not search companies")

    monkeypatch.setattr(client.app.state.automation, "prepare_session", prepare)
    monkeypatch.setattr(client.app.state.automation, "check_companies", unexpected)
    response = client.post("/api/prepare")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["page_kind"] == "customer_information"
    assert calls == ["prepare"]
    assert not client.app.state.browser_lock.locked()
