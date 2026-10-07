import subprocess
import sys

import msal
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from microsoft_identity import (
    AuthenticationError,
    EmailStore,
    MicrosoftAuthenticator,
    MicrosoftAuthSettings,
)
from microsoft_identity.crypto import _fernet
from microsoft_identity.web import create_router


def configured_store(path):
    settings = MicrosoftAuthSettings("client", "secret", "tenant", "http://test/auth/callback", "token-key")
    return settings, EmailStore(path, settings=settings)


class FakeClient:
    def __init__(self, settings, cache):
        self.cache = cache

    def initiate_auth_code_flow(self, **kwargs):
        return {"state": "saved-state", "auth_uri": "https://login.example/authorize"}

    def acquire_token_by_auth_code_flow(self, flow, response):
        assert flow["state"] == response["state"]
        return {"id_token_claims": {"oid": "home-id", "name": "Person", "preferred_username": "person@example.com"}}

    def get_accounts(self):
        return [{"home_account_id": "home-id"}]

    def acquire_token_silent(self, scopes, account):
        self.cache.has_state_changed = True
        return {"access_token": "test-access-token"}


def test_service_completes_login_and_consumes_state_once(tmp_path, monkeypatch):
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "unrelated-project-secret")
    settings, store = configured_store(tmp_path / "auth.db")
    auth = MicrosoftAuthenticator(settings, store, client_factory=FakeClient)

    assert auth.begin_login() == "https://login.example/authorize"
    result = auth.complete_login({"state": "saved-state", "code": "test-code"})
    account = store.account(result.account_id)
    assert account["email"] == "person@example.com"
    assert account["token_cache"] != msal.SerializableTokenCache().serialize()
    assert auth.access_token(account) == "test-access-token"
    with pytest.raises(AuthenticationError, match="expired"):
        auth.complete_login({"state": "saved-state", "code": "reused-code"})


def test_refresh_persists_cache_in_injected_store(tmp_path, monkeypatch):
    settings, store = configured_store(tmp_path / "auth.db")
    account_id = store.save_account({"email": "person@example.com"}, "home-id", "{}")
    refreshed = []
    monkeypatch.setattr(store, "update_cache", lambda account_id, cache: refreshed.append((account_id, cache)))
    auth = MicrosoftAuthenticator(settings, store, client_factory=FakeClient)
    assert auth.access_token(store.account(account_id)) == "test-access-token"
    assert refreshed == [(account_id, msal.SerializableTokenCache().serialize())]


def test_token_decryption_requires_matching_settings(tmp_path):
    settings, store = configured_store(tmp_path / "auth.db")
    wrong = MicrosoftAuthSettings("client", "secret", "tenant", "http://test/callback", "other-key")
    encrypted = _fernet(wrong).encrypt(b"{}").decode()
    auth = MicrosoftAuthenticator(settings, store, client_factory=FakeClient)
    with pytest.raises(AuthenticationError, match="cannot be decrypted"):
        auth.access_token({"id": 1, "token_cache": encrypted})


def make_app(store, settings):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-session-secret")
    app.include_router(create_router(store, settings))

    @app.get("/test-session")
    def test_session(request: Request):
        request.session["microsoft_user"] = {"name": "Person"}
        return {"status": "ok"}

    @app.get("/test-session-state")
    def test_session_state(request: Request):
        return dict(request.session)

    return TestClient(app)


def test_independent_routers_and_remove_clear_session(tmp_path):
    settings, first = configured_store(tmp_path / "first.db")
    _, second = configured_store(tmp_path / "second.db")
    account_id = first.save_account({"email": "first@example.com", "name": "First"}, "first-id", "{}")
    second.save_account({"email": "second@example.com", "name": "Second"}, "second-id", "{}")
    first_client, second_client = make_app(first, settings), make_app(second, settings)
    assert "first@example.com" in first_client.get("/login").text
    assert "second@example.com" not in first_client.get("/login").text
    first_client.get("/test-session")
    response = first_client.post(f"/accounts/{account_id}/remove", follow_redirects=False)
    assert response.status_code == 303
    assert first_client.get("/test-session-state").json() == {}
    assert first.accounts() == []
    assert "second@example.com" in second_client.get("/login").text


def test_import_does_not_load_app_or_fastapi():
    result = subprocess.run(
        [sys.executable, "-c",
         "import microsoft_identity, sys; assert 'app' not in sys.modules; "
         "assert 'fastapi' not in sys.modules; assert 'microsoft_auth' not in sys.modules"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
