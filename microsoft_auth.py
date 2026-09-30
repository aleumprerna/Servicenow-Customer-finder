from __future__ import annotations

import html
import os
from dataclasses import dataclass
from urllib.parse import quote

import msal
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse


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
    base_url = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    return MicrosoftAuthSettings(
        client_id=os.getenv("MICROSOFT_CLIENT_ID", "").strip(),
        client_secret=os.getenv("MICROSOFT_CLIENT_SECRET", "").strip(),
        tenant_id=os.getenv("MICROSOFT_TENANT_ID", "").strip(),
        redirect_uri=os.getenv(
            "MICROSOFT_REDIRECT_URI", f"{base_url}/auth/callback"
        ).strip(),
    )


def _client(settings: MicrosoftAuthSettings) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        settings.client_id,
        authority=settings.authority,
        client_credential=settings.client_secret,
    )


def _login_page(*, user: dict[str, str] | None = None, error: str = "", configured: bool = True) -> str:
    error_html = (
        f'<div class="message error" role="alert">{html.escape(error)}</div>' if error else ""
    )
    if user:
        name = html.escape(user.get("name") or "Microsoft user")
        email = html.escape(user.get("preferred_username") or user.get("email") or "")
        content = f"""
          <div class="avatar" aria-hidden="true">{name[:1].upper()}</div>
          <h1>Welcome, {name}</h1>
          <p class="subtitle">You are signed in with Microsoft.</p>
          <div class="account"><span>Account</span><strong>{email}</strong></div>
          <a class="button secondary" href="/">Continue to application</a>
          <a class="text-link" href="/logout">Sign out</a>
        """
    else:
        disabled = " disabled aria-disabled=\"true\"" if not configured else ""
        href = "#" if not configured else "/auth/microsoft"
        setup = "" if configured else (
            '<div class="message">Add the Microsoft credentials from <code>.env.example</code> '
            'to your <code>.env</code> file, then restart the server.</div>'
        )
        content = f"""
          <div class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
          <h1>Sign in to continue</h1>
          <p class="subtitle">Use your Microsoft work or school account to access the application.</p>
          {setup}
          <a class="button" href="{href}"{disabled}>
            <span class="microsoft-logo" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
            Sign in with Microsoft
          </a>
          <p class="permission-note">The app will request permission to view your basic profile.</p>
        """
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Microsoft sign in</title><style>
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;font-family:Inter,Segoe UI,Arial,sans-serif;color:#172033;background:radial-gradient(circle at top left,#e8f2ff,transparent 38%),#f5f7fb}}
.card{{width:min(440px,100%);padding:44px;border:1px solid #e2e7ef;border-radius:20px;background:#fff;box-shadow:0 20px 55px rgba(35,55,90,.12);text-align:center}}
.brand-mark,.microsoft-logo{{display:grid;grid-template-columns:repeat(2,1fr);gap:3px}} .brand-mark{{width:42px;height:42px;margin:0 auto 25px}} .microsoft-logo{{width:20px;height:20px}}
.brand-mark i:nth-child(1),.microsoft-logo i:nth-child(1){{background:#f25022}} .brand-mark i:nth-child(2),.microsoft-logo i:nth-child(2){{background:#7fba00}} .brand-mark i:nth-child(3),.microsoft-logo i:nth-child(3){{background:#00a4ef}} .brand-mark i:nth-child(4),.microsoft-logo i:nth-child(4){{background:#ffb900}}
h1{{font-size:28px;margin:0 0 12px}} .subtitle{{color:#687386;line-height:1.55;margin:0 0 28px}} .button{{display:flex;align-items:center;justify-content:center;gap:13px;width:100%;min-height:50px;padding:12px 18px;border-radius:9px;background:#2563eb;color:#fff;text-decoration:none;font-weight:700;box-shadow:0 8px 18px rgba(37,99,235,.2)}}
.button:hover{{background:#1d4ed8}} .button.disabled{{background:#aab3c2;pointer-events:none;box-shadow:none}} .button.secondary{{margin-top:24px;background:#172033}} .text-link{{display:inline-block;margin-top:20px;color:#49627d}} .permission-note{{font-size:13px;color:#8490a3;margin:18px 0 0}}
.message{{margin:0 0 20px;padding:12px;border-radius:8px;background:#fff6dd;color:#6f5211;font-size:14px;line-height:1.45}} .message.error{{background:#fff0f0;color:#a11}} .avatar{{display:grid;place-items:center;width:64px;height:64px;margin:0 auto 22px;border-radius:50%;background:#e8f0ff;color:#245bd6;font-size:27px;font-weight:800}} .account{{display:flex;flex-direction:column;gap:5px;padding:15px;border-radius:9px;background:#f5f7fb;color:#687386}} .account strong{{color:#172033}}
</style></head><body><main class="card">{error_html}{content}</main></body></html>"""


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "") -> HTMLResponse:
    return HTMLResponse(
        _login_page(
            user=request.session.get("microsoft_user"),
            error=error,
            configured=microsoft_auth_settings().configured,
        )
    )


@router.get("/auth/microsoft")
async def microsoft_login(request: Request) -> RedirectResponse:
    settings = microsoft_auth_settings()
    if not settings.configured:
        return RedirectResponse("/login?error=" + quote("Microsoft login is not configured."), 303)
    flow = _client(settings).initiate_auth_code_flow(
        scopes=["User.Read"], redirect_uri=settings.redirect_uri
    )
    request.session["microsoft_auth_flow"] = flow
    return RedirectResponse(flow["auth_uri"], 302)


@router.get("/auth/callback")
async def microsoft_callback(request: Request) -> RedirectResponse:
    settings = microsoft_auth_settings()
    flow = request.session.pop("microsoft_auth_flow", None)
    if not settings.configured or not flow:
        return RedirectResponse("/login?error=" + quote("Login session expired. Please try again."), 303)
    try:
        result = _client(settings).acquire_token_by_auth_code_flow(
            flow, dict(request.query_params)
        )
    except ValueError:
        return RedirectResponse("/login?error=" + quote("Microsoft login validation failed."), 303)
    if "error" in result:
        message = result.get("error_description") or result.get("error") or "Microsoft login failed."
        return RedirectResponse("/login?error=" + quote(str(message)), 303)
    request.session["microsoft_user"] = result.get("id_token_claims", {})
    return RedirectResponse("/login", 303)


@router.get("/logout")
async def microsoft_logout(request: Request) -> RedirectResponse:
    settings = microsoft_auth_settings()
    request.session.clear()
    post_logout = quote(f"{settings.redirect_uri.rsplit('/auth/callback', 1)[0]}/login", safe="")
    return RedirectResponse(
        f"{settings.authority}/oauth2/v2.0/logout?post_logout_redirect_uri={post_logout}", 302
    )
