from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

import msal
from cryptography.fernet import InvalidToken

from .config import MicrosoftAuthSettings
from .crypto import _fernet

SCOPES = ["User.Read", "Mail.Send"]


class AuthStore(Protocol):
    """Storage contract for callers that want to supply their own repository."""

    def save_oauth_flow(self, state: str, flow: dict[str, object], cache: str) -> None: ...
    def pop_oauth_flow(self, state: str) -> tuple[dict[str, object], str] | None: ...
    def save_account(self, claims: dict[str, object], home_id: str, cache: str) -> int: ...
    def update_cache(self, account_id: int, cache: str) -> None: ...


class AuthenticationError(RuntimeError):
    """A login or saved-token problem that can be displayed to the user."""


@dataclass(frozen=True)
class LoginResult:
    account_id: int
    claims: dict[str, object]


def _client(settings: MicrosoftAuthSettings, cache: msal.SerializableTokenCache | None = None):
    return msal.ConfidentialClientApplication(
        settings.client_id, authority=settings.authority,
        client_credential=settings.client_secret, token_cache=cache,
    )


class MicrosoftAuthenticator:
    """Microsoft OAuth and token refresh, usable independently of any web framework."""

    def __init__(
        self, settings: MicrosoftAuthSettings, store: AuthStore,
        *, client_factory: Callable = _client,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client_factory = client_factory

    def begin_login(self) -> str:
        if not self.settings.configured:
            raise AuthenticationError("Microsoft login is not configured.")
        cache = msal.SerializableTokenCache()
        flow = self.client_factory(self.settings, cache).initiate_auth_code_flow(
            scopes=SCOPES, redirect_uri=self.settings.redirect_uri,
            prompt="select_account", response_mode="form_post",
        )
        self.store.save_oauth_flow(str(flow["state"]), flow, cache.serialize())
        return str(flow["auth_uri"])

    def complete_login(self, auth_response: dict[str, str]) -> LoginResult:
        saved = self.store.pop_oauth_flow(str(auth_response.get("state", "")))
        if not self.settings.configured or not saved:
            raise AuthenticationError("Login session expired. Please try again.")
        flow, serialized = saved
        cache = msal.SerializableTokenCache()
        if serialized:
            cache.deserialize(serialized)
        try:
            result = self.client_factory(self.settings, cache).acquire_token_by_auth_code_flow(
                flow, auth_response,
            )
        except ValueError as exc:
            raise AuthenticationError("Microsoft login validation failed.") from exc
        if "error" in result:
            raise AuthenticationError(str(result.get("error_description") or result["error"]))
        claims = result.get("id_token_claims", {})
        cached = cache.find(msal.TokenCache.CredentialType.ACCOUNT)
        home_id = str(cached[0].get("home_account_id") if cached else claims.get("oid") or "")
        account_id = self.store.save_account(claims, home_id, cache.serialize())
        return LoginResult(account_id=account_id, claims=claims)

    def access_token(self, account: dict[str, object]) -> str:
        cache = msal.SerializableTokenCache()
        try:
            cache.deserialize(_fernet(self.settings).decrypt(str(account["token_cache"]).encode()).decode())
        except InvalidToken as exc:
            raise AuthenticationError(
                "Saved authorization cannot be decrypted. Reconnect this account."
            ) from exc
        client = self.client_factory(self.settings, cache)
        accounts = client.get_accounts()
        if not accounts:
            raise AuthenticationError("Saved Microsoft session expired. Reconnect this account.")
        result = client.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise AuthenticationError(str(
                (result or {}).get("error_description") or "Authorization expired. Reconnect this account."
            ))
        if cache.has_state_changed:
            self.store.update_cache(int(account["id"]), cache.serialize())
        return str(result["access_token"])

