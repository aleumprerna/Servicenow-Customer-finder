import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import microsoft_auth
from microsoft_auth import (
    EmailStore,
    MicrosoftAuthSettings,
    N8N_BULK_RECEIPT_URL,
    _login_page,
    _parse_recipients,
    _token_cache_summary,
)


def test_login_page_has_microsoft_action_without_removed_copy() -> None:
    page = _login_page(configured=True)

    assert 'href="/auth/microsoft"' in page
    assert "Sign in with Microsoft" in page
    assert "permission to view your basic profile" not in page
    assert "connect another account" not in page


def test_login_page_explains_missing_configuration() -> None:
    page = _login_page(configured=False)

    assert 'aria-disabled="true"' in page
    assert ".env.example" in page


def test_signed_in_page_escapes_claims() -> None:
    page = _login_page(
        user={"name": "<Admin>", "preferred_username": "person@example.com"}
    )

    assert "Welcome, &lt;Admin&gt;" in page
    assert "<Admin>" not in page
    assert "person@example.com" in page


def test_signed_in_page_links_to_n8n_with_encoded_account_email_in_new_tab() -> None:
    page = _login_page(
        user={"name": "Sales Bot", "preferred_username": "salesbot@aelumconsulting.com"},
        accounts=[{"id": 1, "display_name": "Sales Bot", "email": "salesbot@aelumconsulting.com"}],
    )

    assert "Connect to n8n" in page
    assert f'href="{N8N_BULK_RECEIPT_URL}?parms=salesbot%40aelumconsulting.com"' in page
    assert 'target="_blank"' in page
    assert 'rel="noopener noreferrer"' in page
    assert 'href="/auth/microsoft"' not in page
    assert 'action="/accounts/1/remove"' in page
    assert ">Remove</button>" in page
    assert "Sign out of this browser session" not in page


def test_signed_out_page_does_not_show_n8n_action() -> None:
    page = _login_page(configured=True)

    assert "Connect to n8n" not in page


def test_authority_uses_configured_tenant() -> None:
    settings = MicrosoftAuthSettings("client", "secret", "tenant-id", "http://localhost/callback")

    assert settings.configured is True
    assert settings.authority == "https://login.microsoftonline.com/tenant-id"


def test_successful_account_save_logs_safe_database_record(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "tenant")
    store = EmailStore()
    store.path = tmp_path / "workflow.db"

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        account_id = store.save_account(
            {
                "preferred_username": "person@example.com",
                "name": "Example Person",
                "tid": "tenant-id",
            },
            "sensitive-home-account-id",
            "sensitive-token-cache",
        )

    message = caplog.messages[-1]
    assert account_id == 1
    assert "table=microsoft_accounts" in message
    assert "person@example.com" in message
    assert "Example Person" in message
    assert "tenant-id" in message
    assert "home_account_id': '<redacted>'" in message
    assert "token_cache': '<encrypted; redacted>'" in message
    assert "sensitive-home-account-id" not in message
    assert "sensitive-token-cache" not in message
    encrypted_message = next(m for m in caplog.messages if "token cache encrypted" in m)
    assert "ciphertext_prefix=" in encrypted_message
    assert "ciphertext_length=" in encrypted_message
    assert "sha256=" in encrypted_message
    assert "sensitive-token-cache" not in encrypted_message


def test_encryption_log_names_token_types_without_logging_values(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "tenant")
    store = EmailStore()
    store.path = tmp_path / "workflow.db"
    cache = json.dumps({
        "AccessToken": {"one": {"secret": "access-secret"}},
        "RefreshToken": {"two": {"secret": "refresh-secret"}},
    })

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        store.save_account(
            {"preferred_username": "person@example.com", "name": "Person", "tid": "tenant"},
            "home-id",
            cache,
        )

    encrypted_message = next(m for m in caplog.messages if "encrypted as one blob" in m)
    assert "'AccessToken': 1" in encrypted_message
    assert "'RefreshToken': 1" in encrypted_message
    assert "access-secret" not in encrypted_message
    assert "refresh-secret" not in encrypted_message


def test_saving_a_new_login_replaces_the_previous_account(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "tenant")
    store = EmailStore()
    store.path = tmp_path / "workflow.db"

    store.save_account(
        {"preferred_username": "first@example.com", "name": "First", "tid": "tenant"},
        "first-home-id",
        "first-cache",
    )
    store.save_account(
        {"preferred_username": "second@example.com", "name": "Second", "tid": "tenant"},
        "second-home-id",
        "second-cache",
    )

    accounts = store.accounts()
    assert len(accounts) == 1
    assert accounts[0]["email"] == "second@example.com"


def test_account_data_api_saves_and_gets_parms_for_connected_email(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "tenant")
    monkeypatch.setattr(microsoft_auth.STORE, "path", tmp_path / "workflow.db")
    microsoft_auth.STORE.save_account(
        {"preferred_username": "person@example.com", "name": "Person", "tid": "tenant"},
        "home-id",
        "cache",
    )
    app = FastAPI()
    app.include_router(microsoft_auth.router)
    client = TestClient(app)

    saved = client.post(
        "/api/microsoft/account-data",
        json={"email": "person@example.com", "parms": {"receipt_id": 42, "status": "ready"}},
    )
    fetched = client.get("/api/microsoft/account-data/person@example.com")

    assert saved.status_code == 200
    assert saved.json()["status"] == "success"
    assert saved.json()["message"] == "Data successfully stored."
    assert fetched.status_code == 200
    assert fetched.json()["parms"] == {"receipt_id": 42, "status": "ready"}
    page = _login_page(accounts=microsoft_auth.STORE.accounts())
    assert 'data-email="person@example.com"' in page
    assert "/api/microsoft/account-data/${encodeURIComponent(email)}" in page
    assert "API response:" in page


def test_account_data_api_rejects_unknown_email(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(microsoft_auth.STORE, "path", tmp_path / "workflow.db")
    app = FastAPI()
    app.include_router(microsoft_auth.router)
    client = TestClient(app)

    response = client.post(
        "/api/microsoft/account-data",
        json={"email": "unknown@example.com", "parms": "value"},
    )

    assert response.status_code == 404


def test_account_data_get_returns_no_data_for_connected_email(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "tenant")
    monkeypatch.setattr(microsoft_auth.STORE, "path", tmp_path / "workflow.db")
    microsoft_auth.STORE.save_account(
        {"preferred_username": "person@example.com", "name": "Person", "tid": "tenant"},
        "home-id",
        "cache",
    )
    app = FastAPI()
    app.include_router(microsoft_auth.router)
    client = TestClient(app)

    response = client.get("/api/microsoft/account-data/person@example.com")

    assert response.status_code == 200
    assert response.json()["status"] == "no_data"
    assert response.json()["parms"] is None


def test_token_cache_summary_reports_shape_without_secrets() -> None:
    cache = json.dumps({
        "AccessToken": {
            "one": {"secret": "access-secret", "expires_on": "1893456000"},
        },
        "RefreshToken": {
            "two": {"secret": "refresh-secret"},
        },
        "Account": {
            "three": {"username": "person@example.com"},
        },
    })

    summary = _token_cache_summary(cache)

    assert summary["credential_counts"] == {"AccessToken": 1, "RefreshToken": 1, "Account": 1}
    assert summary["expires_on"] == {"AccessToken": ["1893456000"]}
    assert "access-secret" not in repr(summary)
    assert "refresh-secret" not in repr(summary)


def test_bulk_recipient_parser_accepts_names_and_addresses() -> None:
    assert _parse_recipients("Jane,jane@example.com\nsolo@example.com") == [
        ("Jane", "jane@example.com"), ("", "solo@example.com")
    ]


def test_bulk_recipient_parser_rejects_invalid_and_excessive_inputs() -> None:
    with pytest.raises(ValueError, match="Invalid recipient"):
        _parse_recipients("not-an-email")
    with pytest.raises(ValueError, match="between 1 and 100"):
        _parse_recipients("\n".join(f"person{i}@example.com" for i in range(101)))
