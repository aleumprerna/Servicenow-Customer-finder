# Microsoft Identity authentication library

This package provides Microsoft sign-in, encrypted MSAL cache storage, silent
token refresh, and an optional FastAPI integration with the existing account
and email pages. It can be imported without starting the ServiceNow app.

## Install in another project

Install from this repository using the Python environment of the other project:

```powershell
python -m pip install "C:\servicenow-partner-finder[web]"
```

For the authentication service without FastAPI, omit `[web]`.
Only the `microsoft_identity` package is installed. Project files, credentials,
SQLite databases, and the project-specific `microsoft_auth.py` adapter are excluded.

## Use the authentication service

```python
from pathlib import Path
from microsoft_identity import (
    EmailStore,
    MicrosoftAuthenticator,
    MicrosoftAuthSettings,
)

settings = MicrosoftAuthSettings(
    client_id="your-client-id",
    client_secret="your-client-secret",
    tenant_id="your-tenant-id",
    redirect_uri="http://localhost:8000/auth/callback",
    token_key="a-stable-secret-used-for-token-encryption",
)
store = EmailStore(Path("data/auth.db"), settings=settings)
auth = MicrosoftAuthenticator(settings, store)

# Redirect the browser to this URL to start Microsoft sign-in:
login_url = auth.begin_login()

# In your callback, pass Microsoft's form fields or query parameters:
# result = auth.complete_login(callback_fields)
# account = store.account(result.account_id)
# access_token = auth.access_token(account)
```

The service raises `AuthenticationError` for expired or invalid authorization.
OAuth state is stored encrypted and consumed once. Use the same settings for the
store and authenticator. Keep `token_key` stable to decrypt previously saved tokens.
If it is omitted, encryption uses the client secret, matching the current app.
Microsoft permissions remain `User.Read` and `Mail.Send`.

You can implement the exported `AuthStore` protocol to supply another repository.
`EmailStore` retains the current one-sender policy and stores OAuth flows, templates,
delivery records, and account data in the selected SQLite file.

## Attach the existing FastAPI pages

```python
import os
from pathlib import Path
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from microsoft_identity import EmailStore, MicrosoftAuthSettings
from microsoft_identity.web import create_router

settings = MicrosoftAuthSettings.from_env()
store = EmailStore(Path("data/auth.db"), settings=settings)
app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SESSION_SECRET"],
    same_site="lax",
    https_only=False,  # Use True when serving over HTTPS.
)
app.include_router(create_router(store=store, settings=settings))
```

Each `create_router` call creates independent routes using its supplied store.
The routes retain `/login`, `/auth/microsoft`, `/auth/callback`, `/logout`,
account removal/account-data APIs, and the `/email` workspace. Register the exact
callback URI in Microsoft Entra. Load your environment file in the host application
before calling `from_env()`; the library does not load files automatically.

## PostgreSQL token storage

```python
from microsoft_identity import PostgresTokenSettings

postgres = PostgresTokenSettings(
    host="127.0.0.1",
    port=5433,
    dbname="outreachgpt",
    user="outreachgpt",
    password="your-database-password",
    schema="sales_agent",
    user_id=123,  # Must already exist in sales_agent.users.
)
store = EmailStore(Path("data/auth.db"), settings=settings, postgres=postgres)
```

Only token accounts use PostgreSQL's existing `sales_agent.msal_tokens` table;
the library does not create or migrate remote tables. The SSH tunnel must be
running if connecting through a forwarded port. Optional `ssh_key`, `ssh_host`,
and `container` fields retrieve the database password from an EC2 Docker container
when `password` is blank.

The current project uses `microsoft_auth.py` as a small compatibility adapter with
its existing `data/workflow.db` path and environment-based PostgreSQL settings.
Existing imports and app routes continue to work.
