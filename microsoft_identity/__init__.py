"""Reusable Microsoft authentication, encrypted token storage, and Graph email support.

FastAPI integration is optional: import create_router from microsoft_identity.web.
Importing this package performs no database, network, or environment-file operations.
"""

from .config import MicrosoftAuthSettings, PostgresTokenSettings
from .service import AuthenticationError, AuthStore, LoginResult, MicrosoftAuthenticator
from .storage import EmailStore

__all__ = [
    "AuthenticationError", "AuthStore", "EmailStore", "LoginResult",
    "MicrosoftAuthenticator", "MicrosoftAuthSettings", "PostgresTokenSettings",
]

