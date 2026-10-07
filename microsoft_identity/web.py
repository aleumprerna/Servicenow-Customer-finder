from __future__ import annotations

import asyncio
import html
import json
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import MicrosoftAuthSettings
from .email import _parse_recipients, send_bulk
from .service import AuthenticationError, MicrosoftAuthenticator
from .storage import EmailStore


def _page(content: str, title: str = "Microsoft email") -> str:
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6fa;color:#172033;font-family:Segoe UI,Arial,sans-serif}}main{{width:min(1000px,calc(100% - 32px));margin:36px auto}}.card{{background:#fff;border:1px solid #dfe5ee;border-radius:14px;padding:24px;margin-bottom:18px;box-shadow:0 8px 30px #24324a12}}h1,h2{{margin-top:0}}label{{display:block;font-weight:650;margin:14px 0 6px}}input,textarea,select{{width:100%;padding:11px;border:1px solid #cbd3df;border-radius:8px;font:inherit}}textarea{{min-height:150px}}button,.button{{display:inline-block;border:0;border-radius:8px;background:#2563eb;color:#fff;padding:11px 16px;text-decoration:none;font-weight:700;cursor:pointer}}.secondary{{background:#354052}}.danger{{background:#b42318}}.row{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.account{{display:flex;justify-content:space-between;align-items:center;gap:15px;padding:18px 0;border-top:1px solid #edf0f5}}.account-actions{{display:flex;align-items:center;gap:10px;flex:0 0 auto}}.account-actions form{{margin:0}}.muted{{color:#687386}}.message{{padding:12px;border-radius:8px;background:#eaf7ee;color:#176b35}}.error{{background:#fff0f0;color:#9d1c1c}}nav{{display:flex;gap:10px;margin-bottom:18px}}@media(max-width:650px){{.row{{grid-template-columns:1fr}}.account{{align-items:flex-start;flex-direction:column}}.account-actions{{flex-wrap:wrap}}}}
</style></head><body><main>{content}</main></body></html>'''


def _login_page(*, user: dict[str, str] | None = None, error: str = "", configured: bool = True,
                accounts: list[dict[str, object]] | None = None) -> str:
    notice = f'<p class="message error">{html.escape(error)}</p>' if error else ""
    setup = "" if configured else '<p class="message error">Add Microsoft credentials from <code>.env.example</code> to <code>.env</code>.</p>'
    account_items = []
    for account in accounts or []:
        account_email = str(account["email"])
        account_items.append(
            f'<div class="account"><div><strong>{html.escape(str(account["display_name"]))}</strong><br>'
            f'<span class="muted">{html.escape(account_email)}</span>'
            f'<div class="muted account-api-response" data-email="{html.escape(account_email, quote=True)}">'
            f'Loading saved API data...</div></div>'
            f'<div class="account-actions"><form method="post" action="/accounts/{account["id"]}/remove" '
            f'onsubmit="return confirm(\'Remove and sign out this Microsoft account?\')">'
            f'<button class="danger">Remove</button></form></div></div>'
        )
    items = "".join(account_items)
    signed_in_email = str(user.get("preferred_username") or user.get("email") or "") if user else ""
    current = (f'<h2>Welcome, {html.escape(user.get("name") or "Microsoft user")}</h2>'
               f'<p>Signed in as <strong>{html.escape(signed_in_email)}</strong>.</p>') if user else ""
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
    return _page(f'''{notice}<section class="card"><h1>Microsoft account</h1>{current}{setup}{sign_in_action}<a class="button secondary" href="/email">Open email workspace</a>{items or '<p class="muted">No account connected yet.</p>'}</section>{account_data_script}''', "Microsoft account")


def create_router(
    store: EmailStore | None = None,
    settings: MicrosoftAuthSettings | None = None,
) -> APIRouter:
    """Create independent Microsoft login and email routes for a FastAPI app.

    The host app must install Starlette SessionMiddleware with a stable secret.
    Explicit settings and a store make the router reusable across applications.
    """
    if store is None:
        store = EmailStore(settings=settings)
    elif settings is not None:
        if store._settings is not None and store.settings != settings:
            raise ValueError("Router and store must use the same Microsoft settings")
        store._settings = settings
    settings_provider = (lambda: settings) if settings is not None else (lambda: store.settings)

    def authenticator() -> MicrosoftAuthenticator:
        return MicrosoftAuthenticator(settings_provider(), store)

    router = APIRouter()


    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str = ""):
        return HTMLResponse(_login_page(user=request.session.get("microsoft_user"), error=error,
                                        configured=settings_provider().configured, accounts=store.accounts()))


    @router.get("/auth/microsoft")
    async def microsoft_login(request: Request):
        try:
            auth_uri = authenticator().begin_login()
        except AuthenticationError as exc:
            return RedirectResponse("/login?error=" + quote(str(exc)), 303)
        return RedirectResponse(auth_uri, 302)


    @router.api_route("/auth/callback", methods=["GET", "POST"])
    async def microsoft_callback(request: Request):
        auth_response = dict(await request.form()) if request.method == "POST" else dict(request.query_params)
        try:
            result = authenticator().complete_login(auth_response)
        except AuthenticationError as exc:
            return RedirectResponse("/login?error=" + quote(str(exc)), 303)
        request.session["microsoft_user"] = result.claims
        return RedirectResponse("/login", 303)


    @router.get("/logout")
    async def microsoft_logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)


    @router.post("/accounts/{account_id}/remove")
    async def remove_account(account_id: int, request: Request):
        store.delete_account(account_id)
        request.session.clear()
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
        if not store.save_account_data(email, body["parms"]):
            return JSONResponse({"error": "Connected Microsoft account not found."}, status_code=404)
        return {
            "status": "success",
            "message": "Data successfully stored.",
            "email": email,
            "parms": body["parms"],
        }


    @router.get("/api/microsoft/account-data/{email}")
    async def get_microsoft_account_data(email: str):
        saved = store.account_data(email)
        if not saved:
            return JSONResponse({"error": "Account data not found."}, status_code=404)
        return saved


    def _email_page(message: str = "", error: bool = False) -> str:
        account_options = "".join(f'<option value="{a["id"]}">{html.escape(str(a["email"]))}</option>' for a in store.accounts())
        template_options = "".join(f'<option value="{t["id"]}">{html.escape(str(t["name"]))}</option>' for t in store.templates())
        notice = f'<p class="message{" error" if error else ""}">{html.escape(message)}</p>' if message else ""
        return _page(f'''<nav><a class="button secondary" href="/login">Accounts</a></nav>{notice}<section class="card"><h1>Bulk email</h1><p class="muted">Each recipient receives a separate message. Maximum 100 recipients.</p><form method="post" action="/email/send"><label>Sender account</label><select name="account_id" required>{account_options}</select><label>Template</label><select name="template_id" required>{template_options}</select><label>Recipients</label><textarea name="recipients" required placeholder="Jane Doe,jane@example.com&#10;John Doe,john@example.com"></textarea><p class="muted">One per line: <code>Name,email@example.com</code>. Use <code>{{{{name}}}}</code> and <code>{{{{email}}}}</code> in templates.</p><button>Send bulk email</button></form></section><section class="card"><h2>Create or update template</h2><form method="post" action="/email/templates"><div class="row"><div><label>Template name</label><input name="name" required></div><div><label>Subject</label><input name="subject" required></div></div><label>Message</label><textarea name="body" required></textarea><button>Save template</button></form></section>''', "Email workspace")


    @router.get("/email", response_class=HTMLResponse)
    async def email_page(message: str = "", kind: str = ""):
        return HTMLResponse(_email_page(message, kind == "error"))


    @router.post("/email/templates")
    async def save_email_template(name: str = Form(...), subject: str = Form(...), body: str = Form(...)):
        if not all((name.strip(), subject.strip(), body.strip())):
            return RedirectResponse("/email?kind=error&message=" + quote("All template fields are required."), 303)
        store.save_template(name.strip(), subject.strip(), body.strip())
        return RedirectResponse("/email?message=" + quote("Template saved."), 303)


    @router.post("/email/send")
    async def send_bulk_email(account_id: int = Form(...), template_id: int = Form(...), recipients: str = Form(...)):
        template = store.template(template_id)
        if not template: return RedirectResponse("/email?kind=error&message=" + quote("Template was not found."), 303)
        try:
            sent, failed = await asyncio.to_thread(send_bulk, store, authenticator(), account_id, template, _parse_recipients(recipients))
        except (ValueError, RuntimeError) as exc:
            return RedirectResponse("/email?kind=error&message=" + quote(str(exc)), 303)
        return RedirectResponse(f'/email?{"kind=error&" if failed else ""}message=' + quote(f"Sent {sent}; failed {failed}."), 303)
    return router

