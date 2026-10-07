# ServiceNow Playwright API

Standalone Python module for checking companies against the ServiceNow Customer
Information form. Send company names and headquarters as JSON; the API runs the
browser automation and returns results in the same HTTP response after the batch
finishes.

All Playwright automation lives in this folder, with independent settings and
dependencies. The API requires no Apollo/AI credentials, CSV files, database, or
dashboard. The original application imports this module. Old browser, country,
matching, filename, and model imports are compatibility shims with no separate
automation implementation.

## Install

Requirements: Python 3.11+, Google Chrome, and a ServiceNow account with access to
the deployment-registration customer search. Sign-in, including SSO/MFA, is manual.

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r playwright_service/requirements.txt
Copy-Item playwright_service/.env.example playwright_service/.env
```

If a virtual environment already exists, activate it and install the module
requirements. The module attaches to installed Chrome using Playwright's
[Chrome DevTools Protocol connection](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp);
there is no need to download Playwright's Chromium for this workflow.

You can also copy this folder to another machine and install it independently:

```powershell
python -m pip install ./playwright_service
servicenow-playwright --port 8001
```

From inside the copied folder, use `python -m pip install .`, then
`servicenow-playwright`. For an installed package, pass an external settings file
with `--env-file C:\path\to\playwright.env`; set an absolute writable `DEBUG_DIR`
if debug output is needed.

## Chrome and login

In a separate PowerShell terminal, start Chrome with a dedicated profile:

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\playwright-servicenow-profile" "https://partnerportal.servicenow.com/partnerhome?id=deployment_registration&spa=1"
```

Replace the executable path if Chrome is installed elsewhere. Sign in manually in
this debug-enabled window and leave **Partner Information** or **Customer
Information** open. From Partner Information, the existing automation selects
engagement manager `ritik.d`, selects **Implementation**, and clicks **Continue**.
Opening Customer Information yourself skips those steps. The module performs
customer searches without submitting a deployment registration.

Keep Chrome open during requests. Playwright disconnects after each request and
leaves the externally launched Chrome window open.

## Run only the first automation after login

After manually signing in, run this command from the repository root:

```powershell
.\.venv\Scripts\python.exe -m playwright_service --prepare-only
```

It attaches to the Chrome session on port 9222, selects engagement manager
`ritik.d`, selects **Implementation**, clicks **Continue**, and switches to
customer-name search on **Customer Information**. It prints progress and a JSON
ready response, then exits with Chrome left open. It does not need a company list
and does not start any company searches or an API server. If Customer Information
is already open, it prepares that form directly. A preparation failure exits with
code 1 and prints the reason.

The existing configured deployment category is **Implementation** (`implementation2`).

When the API server is running, the same first step is available without a body:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8001/api/prepare" -TimeoutSec 180
```

Use the command or endpoint when no other automation is using this Chrome session.
After it reports ready, submit your company/headquarters list to
`POST /api/check-companies`. That batch endpoint also runs preparation automatically
when necessary, so preparation is optional when submitting a batch directly.

## Run

From the repository root, with your virtual environment active:

```powershell
python -m playwright_service --host 127.0.0.1 --port 8001
```

Optional settings file:

```powershell
python -m playwright_service --port 8001 --env-file playwright_service/.env
```

The default port is **8001**, so the existing dashboard can use **8000**. Use the
module entry point with one worker, especially on Windows where Playwright needs
subprocess support. Do not use reload or multiple workers against the same Chrome
session. The API's lock covers only its own process, so also avoid running the
dashboard's automation against that session at the same time.

- Interactive docs: <http://127.0.0.1:8001/docs>
- Alternative docs: <http://127.0.0.1:8001/redoc>
- OpenAPI: <http://127.0.0.1:8001/openapi.json>

The service defaults to localhost and does not implement API authentication.

## Endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | API liveness and browser busy flag; does not contact Chrome. |
| GET | `/api/session` | Check Chrome and the authenticated form without navigating. |
| POST | `/api/prepare` | Select ritik.d / Implementation and continue to Customer Information; no request body or company searches. |
| POST | `/api/check-companies` | Run the supplied batch and return completed results. |

### GET /health

```json
{"status": "ok", "busy": false}
```

### GET /api/session

Successful response:

```json
{
  "browser_connected": true,
  "authenticated": true,
  "page_kind": "customer_information"
}
```

`page_kind` can also be `partner_information`. An unavailable Chrome connection
returns HTTP 503; a missing supported authenticated form returns HTTP 409.

### POST /api/check-companies

Content type: `application/json`.

```json
{
  "companies": [
    {
      "company_name": "Adobe",
      "headquarters": "San Jose, California, United States"
    },
    {
      "company_name": "Infosys",
      "headquarters": "Bengaluru, Karnataka",
      "country_code": "IN"
    }
  ]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `companies` | Yes | 1-100 company objects. Results retain input order, including duplicates. |
| `company_name` | Yes | Nonempty name, maximum 300 characters. |
| `headquarters` | Yes | Nonempty location string, maximum 1,000 characters; echoed in results. |
| `country_code` | Optional | Headquarters country, usually ISO alpha-2 (`US`, `IN`, `GB`). Common aliases such as `UK` are normalized. |
| `country` | Optional | Country name, such as `United States`. Must agree with `country_code` if both are supplied. |

Without an explicit country field, headquarters must be a country or end with a
comma-separated country, such as `San Jose, California, United States`. For
city-only headquarters such as `London`, supply `country_code` or `country`.
Explicit country fields take precedence over headquarters text. The form search
uses company name and country; city/state text is retained as context.

Blank values, invalid countries, unexpected fields, and oversized/empty batches
return HTTP 422 before touching Chrome. No headquarters enrichment is performed.

PowerShell request using the included example:

```powershell
$body = Get-Content -Raw playwright_service/examples/check_companies.json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8001/api/check-companies" -ContentType "application/json" -Body $body -TimeoutSec 600 | ConvertTo-Json -Depth 10
```

curl request:

```powershell
curl.exe --max-time 600 -X POST "http://127.0.0.1:8001/api/check-companies" -H "Content-Type: application/json" --data-binary "@playwright_service/examples/check_companies.json"
```

Illustrative response; actual outcomes depend on the live portal:

```json
{
  "success": true,
  "total": 2,
  "completed": 2,
  "manual_review": 0,
  "errors": 0,
  "results": [
    {
      "company_name": "Adobe",
      "headquarters": "San Jose, California, United States",
      "country": "United States",
      "country_code": "US",
      "servicenow_customer": "Yes",
      "servicenow_matched_name": "Adobe Inc.",
      "match_score": 100,
      "check_status": "completed",
      "returned_names": ["Adobe Inc."],
      "error_message": "",
      "checked_at": "2026-10-07T08:00:00Z"
    },
    {
      "company_name": "Infosys",
      "headquarters": "Bengaluru, Karnataka",
      "country": "India",
      "country_code": "IN",
      "servicenow_customer": "No",
      "servicenow_matched_name": "",
      "match_score": null,
      "check_status": "completed",
      "returned_names": [],
      "error_message": "",
      "checked_at": "2026-10-07T08:00:02Z"
    }
  ]
}
```

`success` is true when every row has `check_status="completed"`; a verified `No`
counts as completed. Timestamps are UTC. `match_score` is null without a matched name.

Existing search behavior is preserved: recognized returned candidates produce
`Yes`, with the best fuzzy match and all returned names exposed for review.
An explicit no-results message produces `No`. Unknown result markup produces
`Unknown` / `manual_review`. The match/review threshold settings remain for
compatibility; the current checker does not use them to reject returned candidates.
Consumers needing stricter matching can inspect `match_score` and `returned_names`.

Individual search failures return `Unknown` / `error`, and processing continues.
When the session disappears, the module tries to reconnect to a replacement form
and retry the affected company once. If recovery fails, remaining companies get
`Unknown` / `error` and a `Not searched` explanation. Earlier results remain in
the response.

This is a request/response API with no job IDs or background polling. Configure
client/proxy timeouts for your batch size; large batches can take several minutes.
Results are not persisted by this API. Save the response in your caller if needed.

## HTTP errors

| Status | Meaning |
| --- | --- |
| 200 | Batch returned; inspect counters and per-company statuses for partial failures. |
| 409 | Browser busy, missing login/form, or portal preparation could not finish. |
| 422 | Invalid JSON body or company/country validation failure. |
| 503 | Chrome unavailable or Playwright could not connect/prepare the session. |

Setup errors have `{"detail": "..."}` bodies. Validation errors have a `detail`
array identifying invalid fields. Only one browser request runs at a time;
another batch or session inspection receives 409. `/health` stays available.

## Configuration

The module reads `playwright_service/.env` by default. It does not automatically
read the parent repository's `.env`. Process variables override the module file.
Use `--env-file` to select another configuration file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHROME_CDP_URL` | `http://localhost:9222` | Chrome debugging endpoint. |
| `SEARCH_TIMEOUT_SECONDS` | `20` | Search action/result timeout; one controlled retry for temporary failure. |
| `LOGIN_TIMEOUT_SECONDS` | `30` | Wait for an authenticated supported form after Chrome connects. |
| `ACTION_TIMEOUT_SECONDS` | `60` | Portal preparation timeout. |
| `DELAY_BETWEEN_COMPANIES_SECONDS` | `2` | Pause between searches. |
| `MATCH_THRESHOLD` | `85` | Compatibility setting; see matching behavior above. |
| `REVIEW_THRESHOLD` | `70` | Compatibility setting; must be below `MATCH_THRESHOLD`. |
| `SAVE_SCREENSHOTS` | `false` | Save normal search/retry-error screenshots. |
| `DEBUG_DIR` | `debug` | Relative to the module folder, or an absolute output directory. |
| `SERVICENOW_RESULT_SELECTORS` | `[]` | JSON array overriding default result selectors. |

Unrecognized result markup always writes diagnostic HTML and a screenshot under
`debug/html` and `debug/screenshots`, even with `SAVE_SCREENSHOTS=false`. Debug
files are ignored by Git. Each selector should identify one customer name or
result row, for example:

```dotenv
SERVICENOW_RESULT_SELECTORS=["[data-testid='customer-search-results'] [data-testid='customer-name']"]
```

## Module layout

```text
playwright_service/
  api.py                 FastAPI endpoints and browser lock
  service.py             Batch orchestration and JSON results
  schemas.py             Request/response and country validation
  config.py              Independent settings
  __main__.py            python -m playwright_service entry point
  chrome.py              Chrome launcher used by the existing dashboard
  csv_runner.py          Existing application's CSV checkpoint adapter
  browser/
    connection.py        CDP connection and form discovery
    preparation.py       Partner -> Customer Information preparation
    servicenow.py        Country selection, searches, retries, result extraction
    session_monitor.py   Existing dashboard's login/session monitor
    errors.py            Preparation error type
  company_matcher.py     Name normalization and fuzzy scoring
  country_normalizer.py Country normalization and form values
  filenames.py           Debug filename helper
  models.py              Shared result/record models
  examples/             Sample JSON request
  tests/                API, session failure, and module isolation tests
  requirements.txt
  requirements-dev.txt
  pyproject.toml         Optional standalone installation
  .env.example
```

## Tests

From the repository root:

```powershell
python -m pip install -r playwright_service/requirements-dev.txt
python -m pytest playwright_service/tests -q
```

Automated tests simulate browser automation. To verify a live session, open Chrome,
sign in, request `/api/session`, then submit a one-company request through `/docs`.
