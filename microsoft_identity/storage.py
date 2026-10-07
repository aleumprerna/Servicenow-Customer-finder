from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from cryptography.fernet import InvalidToken

from .config import MicrosoftAuthSettings, PostgresTokenSettings, microsoft_auth_settings
from .crypto import _fernet

LOGGER = logging.getLogger("uvicorn.error")


def _token_cache_summary(cache: str) -> dict[str, object]:
    """Return useful MSAL cache diagnostics without exposing token credentials."""
    try:
        payload = json.loads(cache)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"serialized_bytes": len(cache.encode()), "format": "unrecognized"}

    credential_counts: dict[str, int] = {}
    expiry_values: dict[str, list[str]] = {}
    if isinstance(payload, dict):
        for credential_type, entries in payload.items():
            if not isinstance(entries, dict):
                continue
            credential_counts[str(credential_type)] = len(entries)
            expiries = sorted({
                str(entry.get("expires_on"))
                for entry in entries.values()
                if isinstance(entry, dict) and entry.get("expires_on")
            })
            if expiries:
                expiry_values[str(credential_type)] = expiries
    return {
        "serialized_bytes": len(cache.encode()),
        "credential_counts": credential_counts,
        "expires_on": expiry_values,
    }


class EmailStore:
    def __init__(
        self,
        path: Path | str | None = None,
        *,
        settings: MicrosoftAuthSettings | None = None,
        postgres: PostgresTokenSettings | None = None,
        use_environment_postgres: bool | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else Path.cwd() / "data" / "workflow.db"
        self._default_path = self.path
        self._settings = settings
        self._postgres = postgres
        self._use_environment_postgres = (
            path is None if use_environment_postgres is None else use_environment_postgres
        )

    @property
    def settings(self) -> MicrosoftAuthSettings:
        return self._settings if self._settings is not None else microsoft_auth_settings()

    @property
    def postgres(self) -> PostgresTokenSettings | None:
        if self._postgres is not None:
            return self._postgres
        if self._use_environment_postgres and self.path == self._default_path:
            return PostgresTokenSettings.from_env()
        return None

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @property
    def uses_postgres_tokens(self) -> bool:
        return self.postgres is not None

    def connect_tokens(self) -> psycopg.Connection:
        """Connect only to the remote MSAL token table through the SSH tunnel."""
        config = self.postgres
        if config is None:
            raise RuntimeError("PostgreSQL token storage is not configured")
        return psycopg.connect(
            host=config.host, port=config.port, dbname=config.dbname,
            user=config.user, password=config.resolve_password(),
            connect_timeout=5, row_factory=dict_row,
        )

    @property
    def token_table(self) -> str:
        schema = self.postgres.schema if self.postgres else "sales_agent"
        if not schema.replace("_", "").isalnum():
            raise ValueError("MSAL_DB_SCHEMA must contain only letters, numbers, and underscores")
        return f'"{schema}"."msal_tokens"'

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS microsoft_accounts (
              id INTEGER PRIMARY KEY, home_account_id TEXT NOT NULL UNIQUE,
              email TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
              tenant_id TEXT NOT NULL DEFAULT '', token_cache TEXT NOT NULL,
              connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS email_templates (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
              subject TEXT NOT NULL, body TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS email_deliveries (
              id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
              recipient TEXT NOT NULL, subject TEXT NOT NULL,
              status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
              sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS microsoft_oauth_flows (
              state TEXT PRIMARY KEY, encrypted_payload TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS microsoft_account_data (
              email TEXT PRIMARY KEY COLLATE NOCASE, parms TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)
            conn.execute("DELETE FROM microsoft_oauth_flows WHERE created_at < datetime('now', '-15 minutes')")
            conn.execute("""DELETE FROM microsoft_accounts
                            WHERE id NOT IN (
                              SELECT id FROM microsoft_accounts
                              ORDER BY datetime(updated_at) DESC, id DESC LIMIT 1
                            )""")

    def accounts(self) -> list[dict[str, object]]:
        self.initialize()
        if self.uses_postgres_tokens:
            with self.connect_tokens() as conn:
                rows = conn.execute(
                    f"SELECT id,email,display_name,tenant_id,connected_at "
                    f"FROM {self.token_table} ORDER BY email"
                ).fetchall()
            with self.connect() as conn:
                local_data = {
                    str(row["email"]).casefold(): dict(row)
                    for row in conn.execute("SELECT email,parms,updated_at FROM microsoft_account_data")
                }
            return [
                {
                    **dict(row),
                    "account_data": local_data.get(str(row["email"]).casefold(), {}).get("parms"),
                    "account_data_updated_at": local_data.get(str(row["email"]).casefold(), {}).get("updated_at"),
                }
                for row in rows
            ]
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT a.id,a.email,a.display_name,a.tenant_id,a.connected_at,
                          d.parms AS account_data,d.updated_at AS account_data_updated_at
                   FROM microsoft_accounts a
                   LEFT JOIN microsoft_account_data d ON d.email=a.email COLLATE NOCASE
                   ORDER BY a.email""")]

    def save_account_data(self, email: str, parms: object) -> bool:
        self.initialize()
        if self.uses_postgres_tokens:
            with self.connect_tokens() as token_conn:
                account = token_conn.execute(
                    f"SELECT 1 FROM {self.token_table} WHERE lower(email)=lower(%s)", (email,)
                ).fetchone()
        else:
            with self.connect() as conn:
                account = conn.execute(
                    "SELECT 1 FROM microsoft_accounts WHERE email=? COLLATE NOCASE", (email,)
                ).fetchone()
        if not account:
            return False
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO microsoft_account_data(email,parms) VALUES(?,?)
                   ON CONFLICT(email) DO UPDATE SET parms=excluded.parms,
                   updated_at=CURRENT_TIMESTAMP""",
                (email, json.dumps(parms, ensure_ascii=False)),
            )
        return True

    def account_data(self, email: str) -> dict[str, object] | None:
        self.initialize()
        if self.uses_postgres_tokens:
            with self.connect_tokens() as token_conn:
                account = token_conn.execute(
                    f"SELECT email FROM {self.token_table} WHERE lower(email)=lower(%s)", (email,)
                ).fetchone()
            if not account:
                return None
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT email,parms,created_at,updated_at FROM microsoft_account_data "
                    "WHERE email=? COLLATE NOCASE", (email,)
                ).fetchone()
            result = dict(row) if row else {
                "email": account["email"], "parms": None,
                "created_at": None, "updated_at": None,
            }
            result["parms"] = json.loads(str(result["parms"])) if result["parms"] is not None else None
            result["status"] = "success" if result["parms"] is not None else "no_data"
            return result
        with self.connect() as conn:
            row = conn.execute(
                """SELECT a.email,d.parms,d.created_at,d.updated_at
                   FROM microsoft_accounts a
                   LEFT JOIN microsoft_account_data d ON d.email=a.email COLLATE NOCASE
                   WHERE a.email=? COLLATE NOCASE""",
                (email,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["parms"] = json.loads(str(result["parms"])) if result["parms"] is not None else None
        result["status"] = "success" if result["parms"] is not None else "no_data"
        return result

    def account(self, account_id: int) -> dict[str, object] | None:
        self.initialize()
        if self.uses_postgres_tokens:
            with self.connect_tokens() as conn:
                row = conn.execute(
                    f"SELECT * FROM {self.token_table} WHERE id=%s", (account_id,)
                ).fetchone()
                return dict(row) if row else None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM microsoft_accounts WHERE id=?", (account_id,)).fetchone()
            return dict(row) if row else None

    def save_account(self, claims: dict[str, object], home_id: str, cache: str) -> int:
        self.initialize()
        email = str(claims.get("preferred_username") or claims.get("email") or "")
        name, tenant = str(claims.get("name") or email), str(claims.get("tid") or "")
        cache_summary = _token_cache_summary(cache)
        LOGGER.info(
            "Microsoft token cache before encryption | email=%s | summary=%s",
            email,
            cache_summary,
        )
        encrypted = _fernet(self.settings).encrypt(cache.encode()).decode()
        LOGGER.info(
            "Microsoft token cache encrypted as one blob | email=%s | contains=%s | ciphertext_prefix=%s... | "
            "ciphertext_length=%s | sha256=%s",
            email,
            cache_summary.get("credential_counts", {}),
            encrypted[:24],
            len(encrypted),
            hashlib.sha256(encrypted.encode()).hexdigest(),
        )
        if self.uses_postgres_tokens:
            user_id = self.postgres.user_id
            if user_id is None:
                raise RuntimeError("MSAL_USER_ID is required to save into sales_agent.msal_tokens")
            with self.connect_tokens() as conn:
                conn.execute(
                    f"DELETE FROM {self.token_table} WHERE home_account_id<>%s AND user_id=%s",
                    (home_id, user_id),
                )
                row = conn.execute(
                    f"""INSERT INTO {self.token_table}
                    (home_account_id,email,display_name,tenant_id,token_cache,user_id)
                    VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(home_account_id) DO UPDATE SET
                    email=excluded.email,display_name=excluded.display_name,
                    tenant_id=excluded.tenant_id,token_cache=excluded.token_cache,
                    user_id=excluded.user_id,updated_at=CURRENT_TIMESTAMP
                    RETURNING id,email,display_name,tenant_id,connected_at,updated_at""",
                    (home_id, email, name, tenant, encrypted, user_id),
                ).fetchone()
                account = dict(row)
            LOGGER.info(
                "Microsoft login saved to DB | table=sales_agent.msal_tokens | record=%s",
                {**account, "home_account_id": "<redacted>", "token_cache": "<encrypted; redacted>"},
            )
            return int(account["id"])
        with self.connect() as conn:
            # This application intentionally supports one Microsoft sender only.
            conn.execute("DELETE FROM microsoft_accounts WHERE home_account_id<>?", (home_id,))
            conn.execute("""INSERT INTO microsoft_accounts(home_account_id,email,display_name,tenant_id,token_cache)
              VALUES(?,?,?,?,?) ON CONFLICT(home_account_id) DO UPDATE SET email=excluded.email,
              display_name=excluded.display_name,tenant_id=excluded.tenant_id,token_cache=excluded.token_cache,
              updated_at=CURRENT_TIMESTAMP""", (home_id, email, name, tenant, encrypted))
            row = conn.execute(
                """SELECT id,email,display_name,tenant_id,connected_at,updated_at
                   FROM microsoft_accounts WHERE home_account_id=?""",
                (home_id,),
            ).fetchone()
            account = dict(row)

        LOGGER.info(
            "Microsoft login saved to DB | table=microsoft_accounts | record=%s",
            {
                **account,
                "home_account_id": "<redacted>",
                "token_cache": "<encrypted; redacted>",
            },
        )
        return int(account["id"])

    def update_cache(self, account_id: int, cache: str) -> None:
        encrypted = _fernet(self.settings).encrypt(cache.encode()).decode()
        if self.uses_postgres_tokens:
            with self.connect_tokens() as conn:
                conn.execute(
                    f"UPDATE {self.token_table} SET token_cache=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                    (encrypted, account_id),
                )
            return
        with self.connect() as conn:
            conn.execute("UPDATE microsoft_accounts SET token_cache=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (encrypted, account_id))

    def delete_account(self, account_id: int) -> None:
        if self.uses_postgres_tokens:
            with self.connect_tokens() as conn:
                conn.execute(f"DELETE FROM {self.token_table} WHERE id=%s", (account_id,))
            return
        with self.connect() as conn:
            conn.execute("DELETE FROM microsoft_accounts WHERE id=?", (account_id,))

    def templates(self) -> list[dict[str, object]]:
        self.initialize()
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM email_templates ORDER BY name")]

    def template(self, template_id: int) -> dict[str, object] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM email_templates WHERE id=?", (template_id,)).fetchone()
            return dict(row) if row else None

    def save_template(self, name: str, subject: str, body: str) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute("""INSERT INTO email_templates(name,subject,body) VALUES(?,?,?)
              ON CONFLICT(name) DO UPDATE SET subject=excluded.subject,body=excluded.body,
              updated_at=CURRENT_TIMESTAMP""", (name, subject, body))

    def delivery(self, account_id: int, recipient: str, subject: str, status: str, error: str = "") -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO email_deliveries(account_id,recipient,subject,status,error) VALUES(?,?,?,?,?)",
                         (account_id, recipient, subject, status, error[:1000]))

    def save_oauth_flow(self, state: str, flow: dict[str, object], cache: str) -> None:
        self.initialize()
        payload = json.dumps({"flow": flow, "cache": cache})
        encrypted = _fernet(self.settings).encrypt(payload.encode()).decode()
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO microsoft_oauth_flows(state,encrypted_payload) VALUES(?,?)",
                         (state, encrypted))

    def pop_oauth_flow(self, state: str) -> tuple[dict[str, object], str] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT encrypted_payload FROM microsoft_oauth_flows WHERE state=?", (state,)).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM microsoft_oauth_flows WHERE state=?", (state,))
        try:
            payload = json.loads(_fernet(self.settings).decrypt(row["encrypted_payload"].encode()))
        except (InvalidToken, ValueError, json.JSONDecodeError):
            return None
        return payload["flow"], payload.get("cache", "")


