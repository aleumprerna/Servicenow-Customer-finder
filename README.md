# ServiceNow Customer Checker

For a complete step-by-step explanation of every workflow source, evidence rule, URL safeguard, module signal, and confidence calculation, see [SOURCES_AND_WORKFLOW.md](SOURCES_AND_WORKFLOW.md).

This application resolves companies with Apollo, enriches headquarters data, researches public ServiceNow evidence, and checkpoints results to CSV.

## Local workflow UI: people → company enrichment → research

The project includes a local web UI for company enrichment and research:

1. Upload a CSV of people containing a name and LinkedIn profile URL. The LinkedIn export headings `Name`, `Profile URL`, `Headline`, and `Headline / Current Role` are supported directly.
2. The app sends each person's normalized LinkedIn profile URL to Apollo People Match and resolves companies only from Apollo's organization and employment-history response. CSV headline/company values remain report context. A primary organization is structurally verified when a named, dated current job matches it. If the primary organization name is null, the latest named employment-history organization is shown with a manual-review warning. The app does **not** scrape a logged-in LinkedIn profile.
3. Click **Review Companies**. The app resolves confirmed employers and runs Apollo organization enrichment.
4. Open **Results** to use Deep Research and review source evidence. Enrichment leads directly to the results page.
5. Download the selected run as CSV. Workflow state is stored in `data/workflow.db` (SQLite); historical portal results and screenshots are preserved.

SQLite is used by default because it requires no server or credentials. It is a local database file; moving to MySQL later only requires replacing the `WorkflowDatabase` repository layer.

Your Apollo API key needs access to both **People Match** (to resolve the employer from the person's LinkedIn profile) and **Organization Enrichment/Search** (to obtain the organization's headquarters country).

### Start the UI

Install the updated requirements once, then start the server:



```powershell
cd C:\servicenow-partner-finder
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Upload your CSV and review the company matches. After enrichment, the app displays results with Deep Research actions and CSV export.

The original standalone command remains available if you only want to run a company CSV directly:

```powershell
python main.py --force
```

Company enrichment records `apollo_success`, `ai_success`, or `apollo_failed`.
It does not assign a portal customer status. Deep Research evaluates public
evidence separately, and historical portal results remain available in reports.

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
├── workflow/
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
- An Apollo API key with Organization Enrichment access; Organization Search access is needed for fallback matching

The Apollo integration uses the official [Organization Enrichment endpoint](https://docs.apollo.io/reference/organization-enrichment), passing LinkedIn URL and name together when available. If direct enrichment cannot be trusted, it uses [Organization Search](https://docs.apollo.io/reference/organization-search), ranks returned organizations by normalized name, LinkedIn URL, and optional domain, and rejects weak or tied matches.

## Install on Windows

From PowerShell in this project directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

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
LLM_PROVIDER=openai
OPENAI_API_KEY=your_openai_api_key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-5.6-luna
INPUT_CSV=companies.csv
OUTPUT_CSV=companies_checked.csv
```

`LLM_PROVIDER=openai` sends all active model calls to OpenAI's Responses API with `gpt-5.6-luna` and hosted web search. KIE, Gemini, and GLM remain configured alternatives and can be restored by changing `LLM_PROVIDER` and supplying that provider's API key.

### AI usage monitoring

Every model request is recorded in the local SQLite workflow database. The dashboard shows total
AI calls, input/output token totals, summed model latency, and estimated cost for the selected CSV
run. Open **Advanced options → AI usage by record** for the per-contact breakdown, or request
`GET /api/runs/{run_id}/ai-metrics` for the complete call ledger and aggregates.

Token and latency monitoring works without additional configuration. Cost calculation is opt-in
because prices differ by provider and model; set the current USD rates in `.env`:

```dotenv
LLM_INPUT_COST_PER_MILLION=
LLM_CACHED_INPUT_COST_PER_MILLION=
LLM_OUTPUT_COST_PER_MILLION=
LLM_WEB_SEARCH_COST_PER_CALL=
```

Update these values when changing models or when provider pricing changes. The telemetry ledger
stores metadata and usage totals only; it does not store prompts, model responses, or API keys.

Do not commit `.env`. Keep API keys private.

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

Refresh every row, including previously completed rows:

```powershell
python main.py --force
```

Enable detailed diagnostic logging:

```powershell
python main.py --limit 1 --verbose
```

The CLI runs company enrichment by default. `--enrich-only` remains available as an alias. CSV checkpoints are preserved between runs; `--force` refreshes previously processed rows.

## Troubleshooting

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

The tests cover country formats, conservative company-name matching, and CSV checkpoint/resume behavior. External API calls are mocked in the automated test suite.
