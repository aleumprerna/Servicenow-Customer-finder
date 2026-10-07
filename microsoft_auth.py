"""Project adapter for the reusable :mod:`microsoft_identity` library.

Existing imports remain supported; new integrations should use microsoft_identity.
"""

from pathlib import Path

from microsoft_identity import EmailStore, MicrosoftAuthSettings, PostgresTokenSettings, MicrosoftAuthenticator
from microsoft_identity.config import microsoft_auth_settings
from microsoft_identity.crypto import _fernet
from microsoft_identity.email import GRAPH_SEND_URL, _parse_recipients, send_bulk
from microsoft_identity.service import SCOPES, _client
from microsoft_identity.storage import _token_cache_summary
from microsoft_identity.web import _login_page, _page, create_router

STORE = EmailStore(
    Path(__file__).resolve().parent / "data" / "workflow.db",
    use_environment_postgres=True,
)
router = create_router(STORE)


def _access_token(account: dict[str, object]) -> str:
    return MicrosoftAuthenticator(STORE.settings, STORE).access_token(account)


def _send_bulk(account_id: int, template: dict[str, object], recipients: list[tuple[str, str]]) -> tuple[int, int]:
    return send_bulk(STORE, MicrosoftAuthenticator(STORE.settings, STORE), account_id, template, recipients)
