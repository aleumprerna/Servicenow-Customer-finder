"""Browser automation exports, loaded only when automation is requested.

Keeping this package initializer lightweight lets the web UI and Microsoft
login page run on machines where Windows policy blocks Playwright's native
greenlet extension.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ConnectedServiceNow",
    "FormNotFoundError",
    "ServiceNowChecker",
    "SessionExpiredError",
    "connect_to_servicenow",
]


def __getattr__(name: str) -> Any:
    if name in {"ConnectedServiceNow", "FormNotFoundError", "connect_to_servicenow"}:
        from browser import connection

        return getattr(connection, name)
    if name in {"ServiceNowChecker", "SessionExpiredError"}:
        from browser import servicenow

        return getattr(servicenow, name)
    raise AttributeError(name)
