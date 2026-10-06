from __future__ import annotations

import asyncio, base64, csv, hashlib, html, io, json, logging, os, sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import msal
import requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SCOPES = ["User.Read", "Mail.Send"]
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
N8N_BULK_RECEIPT_URL = "http://localhost:5678/webhook/show-params-webhook/"
# Route login audit messages through Uvicorn's configured application logger so
# INFO records are visible in the same terminal where the web app is running.
LOGGER = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class MicrosoftAuthSettings:
    client_id: str
    client_secret: str
    tenant_id: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return all((self.client_id, self.client_secret, self.tenant_id))

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"


def microsoft_auth_settings() -> MicrosoftAuthSettings:
    base = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    return MicrosoftAuthSettings(
        os.getenv("MICROSOFT_CLIENT_ID", "").strip(),
        os.getenv("MICROSOFT_CLIENT_SECRET", "").strip(),
        os.getenv("MICROSOFT_TENANT_ID", "").strip(),
        os.getenv("MICROSOFT_REDIRECT_URI", f"{base}/auth/callback").strip(),
    )


def _fernet(settings: MicrosoftAuthSettings) -> Fernet:
    material = os.getenv("MICROSOFT_TOKEN_KEY", "").strip() or settings.client_secret
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest()))


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
    def __init__(self) -> None:
        self.path = Path(__file__).resolve().parent / "data" / "workflow.db"

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

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
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT a.id,a.email,a.display_name,a.tenant_id,a.connected_at,
                          d.parms AS account_data,d.updated_at AS account_data_updated_at
                   FROM microsoft_accounts a
                   LEFT JOIN microsoft_account_data d ON d.email=a.email COLLATE NOCASE
                   ORDER BY a.email""")]

    def save_account_data(self, email: str, parms: object) -> bool:
        self.initialize()
        with self.connect() as conn:
            account = conn.execute(
                "SELECT 1 FROM microsoft_accounts WHERE email=? COLLATE NOCASE", (email,)
            ).fetchone()
            if not account:
                return False
            conn.execute(
                """INSERT INTO microsoft_account_data(email,parms) VALUES(?,?)
                   ON CONFLICT(email) DO UPDATE SET parms=excluded.parms,
                   updated_at=CURRENT_TIMESTAMP""",
                (email, json.dumps(parms, ensure_ascii=False)),
            )
        return True

    def account_data(self, email: str) -> dict[str, object] | None:
        self.initialize()
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
        encrypted = _fernet(microsoft_auth_settings()).encrypt(cache.encode()).decode()
        LOGGER.info(
            "Microsoft token cache encrypted as one blob | email=%s | contains=%s | ciphertext_prefix=%s... | "
            "ciphertext_length=%s | sha256=%s",
            email,
            cache_summary.get("credential_counts", {}),
            encrypted[:24],
            len(encrypted),
            hashlib.sha256(encrypted.encode()).hexdigest(),
        )
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
        encrypted = _fernet(microsoft_auth_settings()).encrypt(cache.encode()).decode()
        with self.connect() as conn:
            conn.execute("UPDATE microsoft_accounts SET token_cache=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (encrypted, account_id))

    def delete_account(self, account_id: int) -> None:
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
        encrypted = _fernet(microsoft_auth_settings()).encrypt(payload.encode()).decode()
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
            payload = json.loads(_fernet(microsoft_auth_settings()).decrypt(row["encrypted_payload"].encode()))
        except (InvalidToken, ValueError, json.JSONDecodeError):
            return None
        return payload["flow"], payload.get("cache", "")


STORE = EmailStore()


def _client(settings: MicrosoftAuthSettings, cache: msal.SerializableTokenCache | None = None):
    return msal.ConfidentialClientApplication(settings.client_id, authority=settings.authority,
                                               client_credential=settings.client_secret, token_cache=cache)


def _page(content: str, title: str = "Microsoft email") -> str:
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6fa;color:#172033;font-family:Segoe UI,Arial,sans-serif}}main{{width:min(1000px,calc(100% - 32px));margin:36px auto}}.card{{background:#fff;border:1px solid #dfe5ee;border-radius:14px;padding:24px;margin-bottom:18px;box-shadow:0 8px 30px #24324a12}}h1,h2{{margin-top:0}}label{{display:block;font-weight:650;margin:14px 0 6px}}input,textarea,select{{width:100%;padding:11px;border:1px solid #cbd3df;border-radius:8px;font:inherit}}textarea{{min-height:150px}}button,.button{{display:inline-block;border:0;border-radius:8px;background:#2563eb;color:#fff;padding:11px 16px;text-decoration:none;font-weight:700;cursor:pointer}}.secondary{{background:#354052}}.danger{{background:#b42318}}.row{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.account{{display:flex;justify-content:space-between;align-items:center;gap:15px;padding:12px 0;border-top:1px solid #edf0f5}}.muted{{color:#687386}}.message{{padding:12px;border-radius:8px;background:#eaf7ee;color:#176b35}}.error{{background:#fff0f0;color:#9d1c1c}}nav{{display:flex;gap:10px;margin-bottom:18px}}@media(max-width:650px){{.row{{grid-template-columns:1fr}}}}
</style></head><body><main>{content}</main></body></html>'''


def _login_page(*, user: dict[str, str] | None = None, error: str = "", configured: bool = True,
                accounts: list[dict[str, object]] | None = None) -> str:
    notice = f'<p class="message error">{html.escape(error)}</p>' if error else ""
    setup = "" if configured else '<p class="message error">Add Microsoft credentials from <code>.env.example</code> to <code>.env</code>.</p>'
    account_items = []
    for account in accounts or []:
        account_items.append(
            f'<div class="account"><div><strong>{html.escape(str(account["display_name"]))}</strong><br>'
            f'<span class="muted">{html.escape(str(account["email"]))}</span>'
            f'<div class="muted account-api-response" data-email="{html.escape(str(account["email"]), quote=True)}">'
            f'Loading saved API data...</div></div>'
            f'<form method="post" action="/accounts/{account["id"]}/remove">'
            f'<button class="danger">Remove</button></form></div>'
        )
    items = "".join(account_items)
    signed_in_email = str(user.get("preferred_username") or user.get("email") or "") if user else ""
    current = (f'<h2>Welcome, {html.escape(user.get("name") or "Microsoft user")}</h2>'
               f'<p>Signed in as <strong>{html.escape(signed_in_email)}</strong>.</p>') if user else ""
    n8n_action = (f' <a class="button secondary" href="{N8N_BULK_RECEIPT_URL}?parms={quote(signed_in_email, safe="")}" '
                  f'target="_blank" rel="noopener noreferrer">Send bulk receipt email with n8n</a>') if signed_in_email else ""
    disabled = ' aria-disabled="true" style="pointer-events:none;opacity:.5"' if not configured else ""
    sign_in_action = (f'<a class="button" href="/auth/microsoft"{disabled}>Sign in with Microsoft</a> ') if not user else ""
    account_data_script = '''<script>
document.querySelectorAll(".account-api-response").forEach(async (element) => {
  const email = element.dataset.email;
  try {
    const response = await fetch(`/api/microsoft/account-data/${encodeURIComponent(email)}`);
    const result = await response.json();
    element.textContent = response.ok && result.parms !== null
      ? `API response: ${JSON.stringify(result.parms)}`
      : "API response: No stored data";
  } catch (error) {
    element.textContent = "API response: Unable to load data";
  }
});
</script>'''
    return _page(f'''{notice}<section class="card"><h1>Microsoft account</h1>{current}{setup}<p class="muted">Only one sender account can be connected. Passwords are never stored. The app will request permission to view your basic profile and send mail.</p>{sign_in_action}<a class="button secondary" href="/email">Open email workspace</a>{n8n_action}{items or '<p class="muted">No account connected yet.</p>'}<p><a href="/logout">Sign out of this browser session</a></p></section>{account_data_script}''', "Microsoft account")


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return HTMLResponse(_login_page(user=request.session.get("microsoft_user"), error=error,
                                    configured=microsoft_auth_settings().configured, accounts=STORE.accounts()))


@router.get("/auth/microsoft")
async def microsoft_login(request: Request):
    settings = microsoft_auth_settings()
    if not settings.configured:
        return RedirectResponse("/login?error=" + quote("Microsoft login is not configured."), 303)
    cache = msal.SerializableTokenCache()
    flow = _client(settings, cache).initiate_auth_code_flow(
        scopes=SCOPES, redirect_uri=settings.redirect_uri,
        prompt="select_account", response_mode="form_post",
    )
    STORE.save_oauth_flow(str(flow["state"]), flow, cache.serialize())
    return RedirectResponse(flow["auth_uri"], 302)


@router.api_route("/auth/callback", methods=["GET", "POST"])
async def microsoft_callback(request: Request):
    settings = microsoft_auth_settings()
    auth_response = dict(await request.form()) if request.method == "POST" else dict(request.query_params)
    saved = STORE.pop_oauth_flow(str(auth_response.get("state", "")))
    if not settings.configured or not saved:
        return RedirectResponse("/login?error=" + quote("Login session expired. Please try again."), 303)
    flow, serialized = saved
    cache = msal.SerializableTokenCache()
    if serialized: cache.deserialize(serialized)
    try:
        result = _client(settings, cache).acquire_token_by_auth_code_flow(flow, auth_response)
    except ValueError:
        return RedirectResponse("/login?error=" + quote("Microsoft login validation failed."), 303)
    if "error" in result:
        return RedirectResponse("/login?error=" + quote(str(result.get("error_description") or result["error"])), 303)
    claims, cached = result.get("id_token_claims", {}), cache.find(msal.TokenCache.CredentialType.ACCOUNT)
    home_id = str(cached[0].get("home_account_id") if cached else claims.get("oid") or "")
    STORE.save_account(claims, home_id, cache.serialize())
    request.session["microsoft_user"] = claims
    return RedirectResponse("/login", 303)


@router.get("/logout")
async def microsoft_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@router.post("/accounts/{account_id}/remove")
async def remove_account(account_id: int):
    STORE.delete_account(account_id)
    return RedirectResponse("/login", 303)


@router.post("/api/microsoft/account-data")
async def save_microsoft_account_data(request: Request):
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "A JSON body is required."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "The JSON body must be an object."}, status_code=400)
    email = str(body.get("email") or "").strip()
    if not email or "parms" not in body:
        return JSONResponse({"error": "Both email and parms are required."}, status_code=422)
    if not STORE.save_account_data(email, body["parms"]):
        return JSONResponse({"error": "Connected Microsoft account not found."}, status_code=404)
    return {
        "status": "success",
        "message": "Data successfully stored.",
        "email": email,
        "parms": body["parms"],
    }


@router.get("/api/microsoft/account-data/{email}")
async def get_microsoft_account_data(email: str):
    saved = STORE.account_data(email)
    if not saved:
        return JSONResponse({"error": "Account data not found."}, status_code=404)
    return saved


def _email_page(message: str = "", error: bool = False) -> str:
    account_options = "".join(f'<option value="{a["id"]}">{html.escape(str(a["email"]))}</option>' for a in STORE.accounts())
    template_options = "".join(f'<option value="{t["id"]}">{html.escape(str(t["name"]))}</option>' for t in STORE.templates())
    notice = f'<p class="message{" error" if error else ""}">{html.escape(message)}</p>' if message else ""
    return _page(f'''<nav><a class="button secondary" href="/login">Accounts</a></nav>{notice}<section class="card"><h1>Bulk email</h1><p class="muted">Each recipient receives a separate message. Maximum 100 recipients.</p><form method="post" action="/email/send"><label>Sender account</label><select name="account_id" required>{account_options}</select><label>Template</label><select name="template_id" required>{template_options}</select><label>Recipients</label><textarea name="recipients" required placeholder="Jane Doe,jane@example.com&#10;John Doe,john@example.com"></textarea><p class="muted">One per line: <code>Name,email@example.com</code>. Use <code>{{{{name}}}}</code> and <code>{{{{email}}}}</code> in templates.</p><button>Send bulk email</button></form></section><section class="card"><h2>Create or update template</h2><form method="post" action="/email/templates"><div class="row"><div><label>Template name</label><input name="name" required></div><div><label>Subject</label><input name="subject" required></div></div><label>Message</label><textarea name="body" required></textarea><button>Save template</button></form></section>''', "Email workspace")


@router.get("/email", response_class=HTMLResponse)
async def email_page(message: str = "", kind: str = ""):
    return HTMLResponse(_email_page(message, kind == "error"))


@router.post("/email/templates")
async def save_email_template(name: str = Form(...), subject: str = Form(...), body: str = Form(...)):
    if not all((name.strip(), subject.strip(), body.strip())):
        return RedirectResponse("/email?kind=error&message=" + quote("All template fields are required."), 303)
    STORE.save_template(name.strip(), subject.strip(), body.strip())
    return RedirectResponse("/email?message=" + quote("Template saved."), 303)


def _parse_recipients(value: str) -> list[tuple[str, str]]:
    result = []
    for row in csv.reader(io.StringIO(value)):
        if not row or not any(x.strip() for x in row): continue
        name, address = ("", row[0].strip()) if len(row) == 1 else (row[0].strip(), row[1].strip())
        if "@" not in address or any(x in address for x in "\r\n"): raise ValueError(f"Invalid recipient: {address}")
        result.append((name, address))
    if not 1 <= len(result) <= 100: raise ValueError("Enter between 1 and 100 recipients.")
    return result


def _access_token(account: dict[str, object]) -> str:
    settings, cache = microsoft_auth_settings(), msal.SerializableTokenCache()
    try:
        cache.deserialize(_fernet(settings).decrypt(str(account["token_cache"]).encode()).decode())
    except InvalidToken as exc:
        raise RuntimeError("Saved authorization cannot be decrypted. Reconnect this account.") from exc
    client = _client(settings, cache)
    accounts = client.get_accounts()
    if not accounts: raise RuntimeError("Saved Microsoft session expired. Reconnect this account.")
    result = client.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise RuntimeError(str((result or {}).get("error_description") or "Authorization expired. Reconnect this account."))
    if cache.has_state_changed: STORE.update_cache(int(account["id"]), cache.serialize())
    return str(result["access_token"])


def _send_bulk(account_id: int, template: dict[str, object], recipients: list[tuple[str, str]]) -> tuple[int, int]:
    account = STORE.account(account_id)
    if not account: raise RuntimeError("Sender account was not found.")
    token, sent, failed = _access_token(account), 0, 0
    for name, address in recipients:
        subject = str(template["subject"]).replace("{{name}}", name).replace("{{email}}", address)
        body = str(template["body"]).replace("{{name}}", name).replace("{{email}}", address)
        payload = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": html.escape(body).replace("\n", "<br>")}, "toRecipients": [{"emailAddress": {"address": address}}]}, "saveToSentItems": True}
        try:
            response = requests.post(GRAPH_SEND_URL, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, data=json.dumps(payload), timeout=30)
            response.raise_for_status(); STORE.delivery(account_id, address, subject, "sent"); sent += 1
        except requests.RequestException as exc:
            STORE.delivery(account_id, address, subject, "failed", str(exc)); failed += 1
    return sent, failed


@router.post("/email/send")
async def send_bulk_email(account_id: int = Form(...), template_id: int = Form(...), recipients: str = Form(...)):
    template = STORE.template(template_id)
    if not template: return RedirectResponse("/email?kind=error&message=" + quote("Template was not found."), 303)
    try:
        sent, failed = await asyncio.to_thread(_send_bulk, account_id, template, _parse_recipients(recipients))
    except (ValueError, RuntimeError) as exc:
        return RedirectResponse("/email?kind=error&message=" + quote(str(exc)), 303)
    return RedirectResponse(f'/email?{"kind=error&" if failed else ""}message=' + quote(f"Sent {sent}; failed {failed}."), 303)
