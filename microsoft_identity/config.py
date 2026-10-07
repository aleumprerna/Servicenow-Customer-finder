from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class MicrosoftAuthSettings:
    client_id: str
    client_secret: str = field(repr=False)
    tenant_id: str
    redirect_uri: str
    token_key: str = field(default="", repr=False)

    @property
    def configured(self) -> bool:
        return all((self.client_id, self.client_secret, self.tenant_id))

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @classmethod
    def from_env(cls) -> MicrosoftAuthSettings:
        base = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
        return cls(
            os.getenv("MICROSOFT_CLIENT_ID", "").strip(),
            os.getenv("MICROSOFT_CLIENT_SECRET", "").strip(),
            os.getenv("MICROSOFT_TENANT_ID", "").strip(),
            os.getenv("MICROSOFT_REDIRECT_URI", f"{base}/auth/callback").strip(),
            os.getenv("MICROSOFT_TOKEN_KEY", "").strip(),
        )


def microsoft_auth_settings() -> MicrosoftAuthSettings:
    return MicrosoftAuthSettings.from_env()


@dataclass(frozen=True)
class PostgresTokenSettings:
    host: str
    port: int = 5433
    dbname: str = "outreachgpt"
    user: str = "outreachgpt"
    password: str = field(default="", repr=False)
    schema: str = "sales_agent"
    user_id: int | None = None
    ssh_key: str = ""
    ssh_host: str = ""
    container: str = "outreachgpt-postgress_db-1"

    @classmethod
    def from_env(cls) -> PostgresTokenSettings | None:
        host = os.getenv("MSAL_DB_HOST", "").strip()
        if not host:
            return None
        raw_id = os.getenv("MSAL_USER_ID", "").strip()
        return cls(
            host=host, port=int(os.getenv("MSAL_DB_PORT", "5433")),
            dbname=os.getenv("MSAL_DB_NAME", "outreachgpt"),
            user=os.getenv("MSAL_DB_USER", "outreachgpt"),
            password=os.getenv("MSAL_DB_PASSWORD", "").strip(),
            schema=os.getenv("MSAL_DB_SCHEMA", "sales_agent").strip(),
            user_id=int(raw_id) if raw_id else None,
            ssh_key=os.getenv("MSAL_SSH_KEY", "").strip(),
            ssh_host=os.getenv("MSAL_SSH_HOST", "").strip(),
            container=os.getenv("MSAL_DB_CONTAINER", "outreachgpt-postgress_db-1").strip(),
        )

    def resolve_password(self) -> str:
        if self.password:
            return self.password
        return _ssh_password(self.ssh_key, self.ssh_host, self.container)


@lru_cache(maxsize=8)
def _ssh_password(key: str, host: str, container: str) -> str:
    if not key or not host:
        raise RuntimeError(
            "Set MSAL_DB_PASSWORD, or configure MSAL_SSH_KEY and MSAL_SSH_HOST "
            "to load it securely from EC2."
        )
    result = subprocess.run(
        ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         host, "docker", "exec", container, "printenv", "POSTGRES_PASSWORD"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    password = result.stdout.strip()
    if not password:
        raise RuntimeError("EC2 PostgreSQL container returned an empty password")
    return password

