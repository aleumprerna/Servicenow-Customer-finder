# ServiceNow Customer Checker

This application enriches companies with Apollo headquarters data, attaches Playwright to an already-open and manually authenticated Chrome session, searches the ServiceNow Customer Information form, and checkpoints every result to CSV.

## Local workflow UI: people → ServiceNow → n8n

The project also includes a local web UI that adds a full workflow around the existing scraper:

1. Upload a CSV of people containing a name and LinkedIn profile URL. The LinkedIn export headings `Name`, `Profile URL`, and `Headline` are supported directly.
2. The app sends each person's LinkedIn profile URL to Apollo People Match and uses the returned current organization name, domain, and organization LinkedIn URL. It does **not** guess from a headline or scrape a logged-in LinkedIn profile.
3. Click **Run instance**. It opens the ServiceNow deployment-registration URL in a Chrome remote-debugging profile. Log in and wait until the Customer Information form is visible.
4. Click **Start collection**. The app creates a per-run CSV, runs the existing `main.py --force` pipeline, and stores all ServiceNow results in `data/workflow.db` (SQLite).
5. Every completed ServiceNow result with `servicenow_customer=No` is POSTed individually to n8n. `Yes`, `Unknown`, and technical failures are never sent.
6. **Reports** in the UI joins the original person, company resolution, ServiceNow result, and n8n result. You can download the selected run as CSV.

SQLite is used by default because it requires no server or credentials. It is a local database file; moving to MySQL later only requires replacing the `WorkflowDatabase` repository layer.

Your Apollo API key needs access to both **People Match** (to resolve the employer from the person's LinkedIn profile) and **Organization Enrichment/Search** (to obtain the organization's headquarters country).

### Configure n8n

Add these values to your existing `.env` file. Keep the existing Apollo and Chrome values too.

```dotenv
# The n8n production webhook that receives each verified ServiceNow "No" company.
N8N_WEBHOOK_URL=https://your-n8n-host/webhook/servicenow-not-found

# Address n8n can use to POST its final result back to this local Python app.
APP_BASE_URL=http://localhost:8000

# Optional protection for callbacks. If set, configure n8n to send this as X-Workflow-Token.
N8N_CALLBACK_TOKEN=
```

The outbound n8n payload contains `run_id`, `person_id`, `person_name`, `linkedin_url`, `company_name`, Apollo/ServiceNow fields, and `callback_url`. To return a final n8n result, make an HTTP POST to the provided `callback_url` with JSON such as:

```json
{
  "person_id": 123,
  "n8n_status": "completed",
  "message": "Contact created in downstream system"
}
```

The callback URL must be reachable from n8n. For n8n running in Docker on the same computer, `http://host.docker.internal:8000` is normally the correct `APP_BASE_URL`. A hosted n8n instance needs a public/tunneled HTTPS address instead of `localhost`.

### Start the UI

Install the updated requirements once, then start the server:

```powershell
cd C:\servicenow-partner-finder
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Upload your CSV, click **Run instance**, complete the ServiceNow login in the Chrome window that opens, then click **Start collection**. Keep the UI server and Chrome window open until the run completes.

The original standalone command remains available if you only want to run a company CSV directly:

```powershell
python main.py --force
```

It deliberately distinguishes a verified negative from a technical failure:

| Situation | `servicenow_customer` | `check_status` |
|---|---:|---|
| A strong matching result is returned | `Yes` | `completed` |
| Search succeeds but no reasonable match exists | `No` | `completed` |
| Best name score is ambiguous | `Unknown` | `manual_review` |
| Apollo, browser, session, or automation fails | `Unknown` | `apollo_failed` or `error` |
| Result HTML is not recognized | `Unknown` | `manual_review` |

Technical uncertainty is never converted into `No`.

## Project layout

```text
servicenow-customer-checker/
├── main.py
├── config.py
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── .gitignore
├── companies.csv
├── clients/
│   └── apollo.py
├── browser/
│   ├── connection.py
│   └── servicenow.py
├── services/
│   ├── company_matcher.py
│   ├── country_normalizer.py
│   └── csv_service.py
├── models/
│   └── company.py
├── utils/
│   └── logger.py
├── tests/
└── debug/                 # Created at runtime; ignored by Git
    ├── screenshots/
    └── html/
```

## Requirements

- Windows 10/11
- Python 3.11 or newer
- Google Chrome
- An Apollo API key with Organization Enrichment access; Organization Search access is needed for fallback matching
- Access to the ServiceNow portal and its Customer Information search form

The Apollo integration uses the official [Organization Enrichment endpoint](https://docs.apollo.io/reference/organization-enrichment), passing LinkedIn URL and name together when available. If direct enrichment cannot be trusted, it uses [Organization Search](https://docs.apollo.io/reference/organization-search), ranks returned organizations by normalized name, LinkedIn URL, and optional domain, and rejects weak or tied matches.

## Install on Windows

From PowerShell in this project directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

`playwright install chromium` installs Playwright's browser support. The normal workflow still attaches to Google Chrome; it does not launch a new login session.

If PowerShell blocks virtual-environment activation, run this once for the current terminal and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Configure the application

Copy the example file and edit the copy:

```powershell
Copy-Item .env.example .env
notepad .env
```

At minimum, set:

```dotenv
APOLLO_API_KEY=your_real_key
CHROME_CDP_URL=http://localhost:9222
INPUT_CSV=companies.csv
OUTPUT_CSV=companies_checked.csv
```

Do not commit `.env`. The optional `SERVICENOW_USERNAME` and `SERVICENOW_PASSWORD` values are reserved for a possible future login flow and are not read or logged by the current workflow. `HEADLESS` is also informational because an attached browser keeps its existing mode.

Matching thresholds default to:

```dotenv
MATCH_THRESHOLD=85
REVIEW_THRESHOLD=70
```

- Score 85–100: `Yes`
- Score 70–84: `manual_review`
- Score below 70: the other results are evaluated; if none qualify, `No`

Only legal suffixes such as `Inc`, `Corporation`, `Ltd`, and `PLC` are removed. Words such as `technology`, `solutions`, `group`, geography, and business-unit names remain significant to reduce subsidiary false positives.

## Prepare the CSV

The minimum input is:

```csv
company_name,linkedin_url
Microsoft,https://www.linkedin.com/company/microsoft
Adobe,https://www.linkedin.com/company/adobe
```

Optional input columns:

- `domain`: strengthens Apollo matching.
- `country_override`: bypasses Apollo country lookup for that row. It accepts values such as `US`, `United States`, or `US - United States`.

On first run, `OUTPUT_CSV` is created from `INPUT_CSV`. On later runs the output file is the resume source. Every state transition is written using an atomic temporary-file replacement, so completed work survives interruption.

The output contains:

```text
company_name, linkedin_url, headquarters, country, country_code,
apollo_company_name, servicenow_customer, servicenow_matched_name,
match_score, check_status, error_message, checked_at
```

Extra input columns are preserved. `checked_at` is UTC ISO 8601.

## Start Chrome with remote debugging

Use a separate Chrome profile directory. Current Chrome versions require a non-default user-data directory for remote debugging.

If `chrome.exe` is on `PATH`:

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\playwright-servicenow-profile"
```

Otherwise use the full executable path, commonly one of these:

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\playwright-servicenow-profile"
```

```powershell
& "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\playwright-servicenow-profile"
```

Workflow:

1. Close any conflicting Chrome instance already using port `9222` or the debug profile.
2. Start Chrome with one of the commands above.
3. Open the ServiceNow portal in that Chrome window.
4. Log in manually, including any SSO/MFA steps.
5. Navigate manually to the page containing the **Customer Information** heading and customer search form.
6. Keep Chrome and that tab open.
7. Run this application in a separate PowerShell window.

The application examines every context, tab, and frame and selects the one containing both the Customer Information heading and the stable `customer_name` radio input. It does not assume the first tab is correct. It also never calls `browser.close()`, so the externally managed Chrome remains open when processing finishes.

## Run

Activate the virtual environment first:

```powershell
.\.venv\Scripts\Activate.ps1
```

Test one company:

```powershell
python main.py --company "Microsoft"
```

Test the first three eligible rows:

```powershell
python main.py --limit 3
```

Process all pending rows:

```powershell
python main.py
```

Recheck every row, including completed rows:

```powershell
python main.py --force
```

Enable detailed diagnostic logging:

```powershell
python main.py --limit 1 --verbose
```

Normal mode skips only rows whose `check_status` is `completed`. Rows left at `searching`, `error`, `apollo_failed`, or `manual_review` are eligible on the next run. If the ServiceNow form disappears or the page redirects to login, processing stops immediately and all earlier checkpoints remain saved.

## How the ServiceNow interaction works

For each row, the checker:

1. Verifies the same attached page still contains the form.
2. Clears the previous customer name.
3. checks **Select By Customer Name**.
4. Selects `string:<ISO_ALPHA_2>` on `select[name="customerSearchCountry"]`.
5. Verifies the native value, visible Select2 label (when present), and enabled customer input.
6. Falls back to the visible Select2 search UI if the native change did not update Angular.
7. Fills `input[name="customerSearchText"]` only after it is enabled.
8. Clicks the role-based Search button, with `button.search-btn` as fallback.
9. Waits for a recognized result, explicit no-result message, technical error, or a stable but unknown DOM.
10. Extracts every visible candidate and applies conservative fuzzy matching.

ServiceNow browser automation gets one controlled retry for a temporary timeout/loading failure. Apollo retries only network timeouts, connection errors, HTTP 429, and HTTP 5xx responses with exponential backoff.

## Result selectors and HTML changes

The supplied form HTML did not include the actual result markup. Safe defaults are centralized in `DEFAULT_RESULT_SELECTORS` in `browser/servicenow.py`. Each selector must point to **one element per customer name**, not to the entire page or an unrelated table.

If ServiceNow changes its result HTML, the program will not invent a match. It writes:

```text
servicenow_customer=Unknown
check_status=manual_review
```

It also saves a screenshot and the active form/frame HTML under:

```text
debug/screenshots/<company>_results_unknown.png
debug/html/<company>_results_unknown.html
```

To configure selectors without changing Python:

1. Run a one-company test with `SAVE_SCREENSHOTS=true`.
2. Open Chrome DevTools on the ServiceNow results.
3. Inspect the element that contains exactly one returned customer name.
4. Prefer stable attributes such as `data-testid`, `data-customer-name`, semantic classes, or an accessible result container. Avoid generated IDs such as `s2id_autogen34` and absolute XPath.
5. Put a JSON array in `.env`, for example:

```dotenv
SERVICENOW_RESULT_SELECTORS=["[data-testid='customer-results'] [data-testid='customer-name']",".customer-search-results .customer-name"]
```

6. Rerun the single-company test and confirm the log lists the exact customer names, not whole table rows with extra fields.

Alternatively, update `DEFAULT_RESULT_SELECTORS` in `browser/servicenow.py`. Explicit no-result and technical-error phrases are centralized nearby in `NO_RESULTS_PATTERNS` and `TECHNICAL_ERROR_PATTERNS`.

## Troubleshooting

**Could not connect to Chrome**

- Confirm Chrome was started with `--remote-debugging-port=9222` and a separate `--user-data-dir`.
- Open `http://localhost:9222/json/version` in a browser on the same machine. It should return Chrome debugging metadata.
- Check that `.env` uses the same port.

**Customer Information form was not found**

- Use the debug-enabled Chrome window, not a normal Chrome window.
- Complete manual login and navigation before running Python.
- Expand/load the form if the portal lazily renders it.
- Confirm the input still has `name="customer-search-criteria"` and `value="customer_name"`.

**Country selection failed**

- Inspect the underlying option value. It should resemble `string:US`.
- Confirm the select still has `name="customerSearchCountry"`.
- Check whether Select2 was replaced by another component and adjust `_select_country_with_select2` using stable classes/roles.

**All results become manual review**

- Inspect the saved HTML and configure `SERVICENOW_RESULT_SELECTORS` as described above.
- Ensure a selector identifies individual customer-name elements.

**Apollo returns 401 or 403**

- Verify `APOLLO_API_KEY` and its endpoint scopes/plan access.
- Secrets are sent only in the `x-api-key` header and are never logged.

**Output CSV cannot be replaced**

- Close the output file in Excel; Excel may hold an exclusive lock on it.

## Tests

Install development dependencies and run:

```powershell
pip install -r requirements-dev.txt
pytest -q
```

The tests cover country formats, conservative company-name matching, and CSV checkpoint/resume behavior. Live Apollo and ServiceNow calls are intentionally not part of the automated test suite because they require credentials and a manually prepared browser session.
