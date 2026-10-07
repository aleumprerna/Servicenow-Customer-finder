# Project workflow and data sources

This document explains the complete ServiceNow Partner Finder workflow, the source used at each step, what each source can prove, and how evidence is validated.

## End-to-end flow

```text
CSV upload
  -> company resolution
  -> Apollo company enrichment
  -> authenticated ServiceNow portal check
  -> Deep Research
  -> deterministic evidence classification
  -> report, evidence links, screenshots, and CSV export
```

The local SQLite database at `data/workflow.db` stores workflow state, results, evidence JSON, and AI usage metrics. It is storage, not an external evidence source.

## Step 1: CSV input

**Source:** the user-provided CSV file.

Supported input includes the person's name, LinkedIn profile URL, headline/current role, and an optional company name. Extra input columns are preserved. A `technographic_servicenow=true` column is passed into Deep Research as an additional weak signal.

The CSV may provide context, but user-supplied values are not independently verified evidence.

## Step 2: Resolve the person's current company

### Primary source: Apollo People Match

The normalized person LinkedIn URL is sent to Apollo. The project uses Apollo's returned person, primary organization, and employment history to identify the current employer.

The project does **not** scrape a logged-in LinkedIn profile. The LinkedIn URL from the CSV is an identity input used to match the Apollo person record.

An employer is accepted automatically when Apollo provides a named, dated current employment that agrees with the primary organization. Missing, conflicting, weak, or historical employer information is sent for manual review.

### Optional AI web resolution

When the user invokes AI company resolution, the configured web-search-capable model can search for the person's current company and headquarters. With the default configuration this uses the OpenAI Responses API and hosted web search through `OPENAI_API_KEY`; it does not use Serper.

Preferred sources are official company pages, filings, investor material, and verified company LinkedIn pages. Returned source URLs are validated as HTTP/HTTPS URLs before display.

## Step 3: Enrich the resolved company

**Sources:** Apollo Organization Enrichment and Apollo Organization Search.

The enrichment step obtains:

- official or primary company domain;
- company LinkedIn URL;
- headquarters city/region;
- country and normalized ISO country code;
- Apollo organization name.

Direct organization enrichment is used when a domain or company LinkedIn URL is available. Organization Search is the fallback. Candidates are ranked using company-name similarity, LinkedIn URL, and domain. Weak matches and unresolved ties are rejected instead of silently selecting a company.

## Step 4: Authenticated ServiceNow portal verification

**Source:** the ServiceNow Customer Information form opened in the user's authenticated Chrome session.

The application attaches to Chrome through the remote-debugging connection and searches the resolved company name. Results are compared using normalized company-name similarity.

| Result | Meaning |
|---|---|
| `Yes` | A strong matching ServiceNow portal result was returned. |
| `No` | The search completed successfully and no reasonable match was found. |
| `Unknown` | The result was ambiguous, the page was not recognized, or a technical failure occurred. |

When a positive match is found, the application can save a screenshot as visual evidence. A technical failure is never converted into `No`.

The portal result is separate from Deep Research. In particular, a match against the generic ServiceNow customer directory is not accepted as a company-specific Deep Research citation.

## Step 5: Deep Research

Deep Research combines a bounded crawl of the official company site, OpenAI hosted web search, and direct verification of possible ServiceNow customer-story pages. The default configuration uses `OPENAI_API_KEY`; no Serper API is used.

### 5.1 Official company website crawl

**Source:** the resolved company's public official domain and its subdomains.

The crawler starts with the home page, `robots.txt`, `sitemap.xml`, and the `www` variant. It prioritizes URLs containing terms such as ServiceNow, careers, jobs, technology, platform, press, reports, procurement, suppliers, and case studies.

Safety and scope controls include:

- only HTTP/HTTPS public targets;
- same-site URLs and redirects only;
- private/local IP targets rejected;
- page, time, redirect, and response-size limits;
- only HTML, text, and XML content analyzed.

Pages fetched directly by the crawler are citation-grounded because the application observed the URL and content itself.

### 6.2 OpenAI hosted web search

**Source mechanism:** OpenAI Responses API `web_search` tool using `OPENAI_API_KEY`.

The search covers these source groups:

1. Exact company name plus `ServiceNow`.
2. Module-specific searches, including ITSM, ITOM, CSM, HRSD, SPM, PPM, SecOps, IRM/GRC, App Engine, CMDB, Discovery, SAM, and HAM.
3. Official ServiceNow customer stories, case studies, and partner pages on `servicenow.com`.
4. Official company alliance, technology, implementation, careers, job, and certification pages.
5. Implementation-partner pages, press releases, contracts, procurement records, and technical documents.
6. LinkedIn posts from the target company, ServiceNow, or an implementation partner that mention an implementation, collaboration, go-live, rollout, migration, or named module.
7. Indexed DNS and certificate-transparency records, including `crt.sh`, for relevant company subdomains.
8. Public pages with ServiceNow fingerprints such as `sysparm_`, `GlideRecord`, or ServiceNow redirects.
9. Public GitHub results containing signals such as `api/now/table`, `sysparm_query`, `GlideRecord`, or `SNOW_INSTANCE`.
10. CSV technographic data when `technographic_servicenow=true`, used only as an additional weak signal.

DNS, certificate-transparency, GitHub, and public fingerprint sources are currently discovered through hosted web search. The project does not currently run a separate GitHub API client, `crt.sh` API client, or broad active subdomain scanner.

Excluded as final proof are unsourced directories, social forums, aggregators, Apollo technographics, ZoomInfo, BuiltWith, and 6sense. They may help discovery, but they do not independently establish internal ServiceNow use.

### 6.3 LinkedIn implementation and go-live posts

A specific, grounded LinkedIn post can be retained when it explicitly names the target company and describes a ServiceNow implementation, go-live, rollout, migration, collaboration, or module.

The exact individual post URL is required. A LinkedIn profile, company feed, search-results page, or model-invented URL is not accepted.

Examples:

- “We provide ServiceNow implementation services” is **partner evidence**.
- “We implemented ServiceNow SPM for MOL Group” is **customer evidence for MOL Group**, even when the publisher is the implementation partner.
- An implementation for an unnamed or different client remains **partner evidence**.

Third-party implementation evidence normally supports `LIKELY_CUSTOMER`; it does not receive the same authority as an exact official customer story or an explicit statement on the target company's own domain.

### 6.4 ServiceNow customer-story verification

**Source:** a specific page under `servicenow.com/.../customers/<company>.html`.

The verifier checks search-discovered URLs and a small set of deterministic company-name slug variants. A page is accepted only when:

- it returns HTTP 200;
- the final URL is a specific ServiceNow customer-story URL;
- the page contains customer-story markers; and
- the page content matches the target company's distinctive name.

The generic URL `https://www.servicenow.com/customers.html` is a discovery page only. It is never retained as company evidence, never counted as a relevant source, and can never produce 100% confidence. Directory name-match scores are also not Deep Research evidence.

## Step 7: Evidence URL validation

Every model-produced finding must match a URL contained in the search provider's grounding metadata. The application fails closed: if grounding URLs are absent, model-written URLs are discarded.

Additional URL behavior:

- URLs must use HTTP or HTTPS and contain a hostname.
- Tracking parameters beginning with `utm_` are ignored when matching equivalent URLs.
- Known ServiceNow locale variants such as `/in/customers/adobe.html` and `/customers/adobe.html` are treated as the same story path.
- Google grounding redirect URLs used by Gemini are resolved to their publisher URL before matching.
- The provider-grounded URL is stored, not a reconstructed URL written by the model.
- Ungrounded legacy evidence is displayed without a clickable link and with a verification warning.

Each retained evidence item stores:

- exact URL and page title;
- evidence excerpt or close factual paraphrase;
- reason explaining how the evidence affects the conclusion;
- source type;
- explicitly named ServiceNow modules;
- evidence type and strength;
- customer, partner, or ambiguous category;
- official-source and citation-grounded flags.

## Step 8: Module detection

Modules are collected only when the cited evidence explicitly names them. The deterministic extractor recognizes:

- ITSM and IT Service Management;
- ITOM and IT Operations Management;
- CSM and Customer Service Management;
- HRSD and HR Service Delivery;
- SPM and Strategic Portfolio Management;
- PPM and Project Portfolio Management;
- SecOps and Security Operations;
- IRM/GRC and their expanded names;
- App Engine;
- Service Portal;
- IntegrationHub;
- CMDB;
- ServiceNow Discovery;
- SAM and Software Asset Management;
- HAM and Hardware Asset Management.

The evidence card displays the modules next to the supporting excerpt and reason. A generic reference to “ServiceNow” produces an empty module list rather than a guessed module.

## Step 9: Evidence categories

### Customer evidence

Evidence that the target company internally uses, operates, owns, deploys, or receives a named ServiceNow implementation. Examples include:

- explicit internal-use statements on the company domain;
- employee access to a company ServiceNow portal;
- current company jobs managing its ServiceNow platform;
- exact official ServiceNow customer stories;
- grounded third-party posts naming the target as the implementation/go-live customer.

### Partner evidence

Evidence about consulting, reselling, implementation services, certifications, or client delivery that does not identify the target company as the end customer. A company being a ServiceNow partner does not prove that it uses ServiceNow internally.

### Ambiguous evidence

Generic ServiceNow mentions that do not establish internal use, customer status, or a specific implementation relationship.

## Step 10: Classification and confidence

Final classification is rule-controlled. The model may suggest wording and confidence only within the classification boundary established by retained evidence.

| Retained evidence | Result and default confidence behavior |
|---|---|
| Exact grounded official ServiceNow customer story | `CONFIRMED_CUSTOMER`, 100% |
| Strong explicit evidence on the target company's official domain | `CONFIRMED_CUSTOMER`, normally 92–98% |
| Two or more medium official signals | `LIKELY_CUSTOMER`, normally 76–84% |
| One medium official signal | `LIKELY_CUSTOMER`, 64% |
| Strong external customer evidence, such as a named partner go-live | `LIKELY_CUSTOMER`, normally 76–82% |
| Partner evidence with no customer evidence | `PARTNER_ONLY` |
| Generic or unclear references | `INCONCLUSIVE` |
| No relevant retained evidence | `NO_OFFICIAL_EVIDENCE`, 0% |

The generic ServiceNow customer directory is filtered out even from older cached evidence. Search volume does not raise confidence: confidence is based on retained relevant evidence, not the number of pages checked.

## Step 11: Results and audit trail

The UI shows separate sections for customer evidence, partner evidence, and ambiguous references. Each card can show its source link, source type, strength, evidence, named modules, and reason. The research dialog also shows:

- sources checked and relevant sources;
- official company domain;
- specific ServiceNow customer-story verification status;
- research timestamp;
- visited-URL logs;
- AI usage for the action and record.

Results are stored in SQLite and can be exported with the joined workflow report. Model usage metrics include calls, latency, tokens, hosted web-search calls, and optional estimated cost. Prompts, model responses, and API keys are not stored in the AI metric ledger.

## Source reliability summary

| Source | Typical reliability | How it is used |
|---|---|---|
| Exact official ServiceNow customer story | Authoritative | Can confirm a customer at 100%. |
| Explicit target-company page | Strong | Can confirm internal use when direct and unambiguous. |
| Named partner/LinkedIn implementation go-live | Strong external | Supports likely-customer status for the named target. |
| Current target-company ServiceNow job | Medium | Supports likely internal platform ownership. |
| Contract or procurement document | Medium to strong | Depends on whether it names the target and actual deployment/use. |
| Public portal or technical fingerprint | Supporting | Must be tied safely to the exact company. |
| GitHub public code | Supporting | Must establish company ownership and real ServiceNow use. |
| DNS or certificate-transparency record | Supporting | Shows infrastructure clues, not customer status by itself. |
| CSV technographic flag | Weak | Additional signal only, never standalone proof. |
| Generic ServiceNow customer directory | Not evidence | Discovery only; never retained or scored. |
| Generic partner marketing | Partner evidence | Does not prove customer usage. |

## Important limitations

- Public evidence can be incomplete or outdated; absence of evidence is not proof of non-use.
- Search engines may not index every LinkedIn post, job page, document, certificate record, or GitHub file.
- LinkedIn posts are not scraped through a logged-in session; only grounded public search results can be used.
- DNS, certificate-transparency, GitHub, and HTTP fingerprint discovery currently depend on hosted web-search visibility.
- Parent companies, subsidiaries, regional entities, renamed businesses, and similarly named companies require careful identity matching.
- Deep Research reports evidence available at research time. It does not guarantee the company's current licensing or production status.

