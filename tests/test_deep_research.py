from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

import app as dashboard
from services.servicenow_deep_research.classifier import classify_evidence
from services.servicenow_deep_research.crawler import (
    CrawlReport,
    UnsafeResearchTarget,
    normalize_domain,
    validate_public_domain,
)
from services.servicenow_deep_research.customer_page import (
    ServiceNowCustomerPageVerifier,
    customer_story_slugs,
)
from services.servicenow_deep_research.evidence_extractor import (
    evidence_snippets,
    normalize_model_finding,
)
from services.servicenow_deep_research.research_service import (
    DeepResearchService,
    OpenAIResearchProvider,
    ResearchProviderError,
)
from services.servicenow_deep_research.schemas import ResearchClassification
from workflow.database import WorkflowDatabase


def _result_for(text: str, *, sources_checked: int = 1):
    findings = evidence_snippets(
        text,
        url="https://example.com/technology",
        page_title="Technology",
        official_domain="example.com",
    )
    return classify_evidence(
        company_name="Example Company",
        official_domain="example.com",
        findings=findings,
        sources_checked=sources_checked,
        research_depth="deep",
        model_provider="test+rules",
    )


def test_strong_official_internal_use_is_confirmed() -> None:
    result = _result_for("We use ServiceNow internally for IT service management and employee support.")

    assert result.status == ResearchClassification.CONFIRMED_CUSTOMER
    assert result.confidence >= 90
    assert len(result.customer_evidence) == 1


def test_partner_only_evidence_never_becomes_customer_evidence() -> None:
    result = _result_for(
        "We are an Elite ServiceNow Partner providing ServiceNow implementation services for our clients."
    )

    assert result.status == ResearchClassification.PARTNER_ONLY
    assert not result.customer_evidence
    assert len(result.partner_evidence) == 1


def test_partner_marketing_language_never_becomes_customer_evidence() -> None:
    result = _result_for(
        "Transformation Starts Here. We empower businesses to scale smarter using ServiceNow "
        "and innovative digital capabilities. Our expertise helps enterprises design connected "
        "experiences. ServiceNow Experts 200+. 300+ ServiceNow Implementations."
    )

    assert result.status == ResearchClassification.PARTNER_ONLY
    assert not result.customer_evidence
    assert result.partner_evidence


def test_service_provider_using_servicenow_for_clients_is_partner_only() -> None:
    result = _result_for(
        "We use ServiceNow to deliver implementation outcomes for our clients as a consulting partner."
    )

    assert result.status == ResearchClassification.PARTNER_ONLY
    assert not result.customer_evidence


def test_employee_access_to_company_servicenow_portal_is_customer_evidence() -> None:
    result = _result_for(
        "Employees from other countries should visit https://example.service-now.com/itdirect "
        "to contact the internal IT Helpdesk."
    )

    assert result.status == ResearchClassification.CONFIRMED_CUSTOMER


def test_model_cannot_promote_generic_reference_to_customer_evidence() -> None:
    finding = normalize_model_finding(
        {
            "url": "https://example.com/article",
            "page_title": "Industry article",
            "evidence": "ServiceNow is an enterprise workflow platform.",
            "evidence_type": "explicit_internal_usage",
            "strength": "strong",
            "category": "CUSTOMER_EVIDENCE",
        },
        "example.com",
    )

    assert finding is not None
    assert finding.category == "AMBIGUOUS"
    assert finding.strength == "weak"


def test_no_evidence_returns_no_official_evidence_without_guessing() -> None:
    result = _result_for("This page describes the company's products and offices.", sources_checked=8)

    assert result.status == ResearchClassification.NO_OFFICIAL_EVIDENCE
    assert result.confidence < 55
    assert result.relevant_sources == 0


def test_customer_story_slug_variants_cover_legal_and_brand_names() -> None:
    assert customer_story_slugs("SKF India Ltd.") == ["skf-india-ltd", "skf-india", "skf"]
    assert customer_story_slugs("MOL Group") == ["mol-group", "mol"]


def test_customer_page_verifier_requires_official_story_content() -> None:
    class Response:
        status_code = 200
        url = "https://www.servicenow.com/in/customers/skf.html"
        text = "<title>SKF – ServiceNow – Customer Story</title><h2>Customer Details</h2><p>Customer SKF</p>"

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    result = ServiceNowCustomerPageVerifier(session=Session()).check("SKF India Ltd.")

    assert result.found is True
    assert result.url == Response.url


def test_customer_page_verifier_rejects_generic_servicenow_page() -> None:
    class Response:
        status_code = 200
        url = "https://www.servicenow.com/in/customers.html"
        text = "<title>Customer Stories</title><p>Search all stories</p>"

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    result = ServiceNowCustomerPageVerifier(session=Session()).check("Unknown Company")

    assert result.found is False
    assert result.url == ""


def test_customer_page_verifier_does_not_report_no_when_network_is_unavailable() -> None:
    import requests

    class Session:
        def get(self, *_args, **_kwargs):
            raise requests.ConnectionError("offline")

    result = ServiceNowCustomerPageVerifier(session=Session()).check("Example Company")

    assert result.found is None


def test_private_and_unsupported_targets_are_rejected() -> None:
    with pytest.raises(UnsafeResearchTarget):
        validate_public_domain("127.0.0.1")
    with pytest.raises(UnsafeResearchTarget):
        normalize_domain("file:///etc/passwd")
    with pytest.raises(UnsafeResearchTarget):
        normalize_domain("localhost")


def test_resolved_private_address_is_rejected() -> None:
    def private_resolver(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))]

    with pytest.raises(UnsafeResearchTarget):
        validate_public_domain("example.com", resolver=private_resolver)


def test_provider_failure_is_reported_when_no_crawl_evidence_exists() -> None:
    class Crawler:
        def crawl(self, _domain):
            return CrawlReport(findings=[], sources_checked=2, discovered_urls=[])

    class Provider:
        model = "test-model"
        calls = 0

        def discover(self, **_kwargs):
            self.calls += 1
            raise ResearchProviderError("Search provider unavailable")

        def suggest_classification(self, **_kwargs):
            raise AssertionError("classification must not run without any evidence after provider failure")

    provider = Provider()
    with pytest.raises(ResearchProviderError, match="Search provider unavailable"):
        DeepResearchService(provider=provider, crawler=Crawler()).research(
            company_name="Example Company",
            official_domain="example.com",
        )
    assert provider.calls == 1


def test_verified_servicenow_story_is_added_to_research_result() -> None:
    class Crawler:
        def crawl(self, _domain):
            return CrawlReport(findings=[], sources_checked=1, discovered_urls=[])

    class Provider:
        model = "test-model"
        provider_label = "test:test-model"

        def discover(self, **_kwargs):
            return SimpleNamespace(findings=[], source_urls=set())

        def suggest_classification(self, **_kwargs):
            return None

    class Verifier:
        def check(self, _company_name, _discovered_urls):
            return SimpleNamespace(
                found=True,
                url="https://www.servicenow.com/in/customers/example.html",
                checked_urls=("https://www.servicenow.com/in/customers/example.html",),
            )

    result = DeepResearchService(
        provider=Provider(),
        crawler=Crawler(),
        customer_page_verifier=Verifier(),
    ).research(company_name="Example Company", official_domain="example.com")

    assert result.servicenow_customer_page_found is True
    assert result.servicenow_customer_page_url.endswith("/customers/example.html")
    assert result.customer_evidence[0].citation_grounded is True
    assert result.customer_evidence[0].evidence_type == "official_servicenow_customer_story"


def test_openai_discovery_is_official_domain_scoped_and_source_grounded() -> None:
    calls = []

    class Response:
        output_text = json.dumps(
            {
                "findings": [
                    {
                        "url": "https://example.com/technology",
                        "page_title": "Technology",
                        "evidence": "We use ServiceNow internally for IT operations.",
                        "evidence_type": "explicit_internal_usage",
                        "strength": "strong",
                        "category": "CUSTOMER_EVIDENCE",
                    },
                    {
                        "url": "https://invented.example.net/story",
                        "page_title": "Invented",
                        "evidence": "We use ServiceNow internally.",
                        "evidence_type": "explicit_internal_usage",
                        "strength": "strong",
                        "category": "CUSTOMER_EVIDENCE",
                    },
                ]
            }
        )

        def model_dump(self, **_kwargs):
            return {
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {"sources": [{"url": "https://example.com/technology"}]},
                    }
                ]
            }

    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return Response()

    provider = object.__new__(OpenAIResearchProvider)
    provider.model = "test-model"
    provider.supports_hosted_web_search = True
    provider.client = SimpleNamespace(responses=Responses())

    discovery = provider.discover(
        company_name="Example Company",
        official_domain="example.com",
        existing_context={},
        official_only=True,
    )

    assert calls[0]["tools"][0]["filters"] == {"allowed_domains": ["example.com"]}
    assert calls[0]["include"] == ["web_search_call.action.sources"]
    assert calls[0]["text"]["format"]["type"] == "json_schema"
    assert [finding.url for finding in discovery.findings] == ["https://example.com/technology"]
    assert discovery.findings[0].citation_grounded is True


def test_provider_rejects_model_urls_when_grounding_metadata_is_missing() -> None:
    payload = {
        "findings": [
            {
                "url": "https://example.com/plausible-but-invented-path",
                "page_title": "Invented",
                "evidence": "We use ServiceNow internally for IT operations.",
                "evidence_type": "explicit_internal_usage",
                "strength": "strong",
                "category": "CUSTOMER_EVIDENCE",
            }
        ]
    }

    findings = OpenAIResearchProvider._findings(
        payload,
        "example.com",
        official_only=True,
        source_urls=set(),
    )

    assert findings == []


def test_provider_stores_the_authoritative_grounding_url() -> None:
    payload = {
        "findings": [
            {
                "url": "https://example.com/evidence?utm_source=gemini",
                "page_title": "Evidence",
                "evidence": "We use ServiceNow internally for IT operations.",
                "evidence_type": "explicit_internal_usage",
                "strength": "strong",
                "category": "CUSTOMER_EVIDENCE",
            }
        ]
    }

    findings = OpenAIResearchProvider._findings(
        payload,
        "example.com",
        official_only=True,
        source_urls={"https://example.com/evidence"},
    )

    assert findings[0].url == "https://example.com/evidence"
    assert findings[0].citation_grounded is True


def test_research_result_is_persisted_and_reused_from_cache(tmp_path) -> None:
    database = WorkflowDatabase(tmp_path / "workflow.db")
    database.initialize()
    run_id = database.create_run(
        "customers.csv",
        [
            {
                "person_name": "Ada Example",
                "linkedin_url": "https://linkedin.com/in/ada-example",
                "company_name": "Example Company",
            }
        ],
    )
    person_id = database.people_for_run(run_id)[0]["id"]
    database.update_person_resolution(
        person_id,
        company_name="Example Company",
        status="apollo_structurally_verified",
        domain="example.com",
    )
    assert database.claim_deep_research(
        person_id=person_id,
        run_id=run_id,
        company_name="Example Company",
        official_domain="example.com",
    )
    assert not database.claim_deep_research(
        person_id=person_id,
        run_id=run_id,
        company_name="Example Company",
        official_domain="example.com",
    )

    result = _result_for("We use ServiceNow internally for IT service management.")
    database.complete_deep_research(person_id, result.as_storage_values())

    stored = database.deep_research(person_id)
    assert stored is not None
    assert stored["classification_status"] == "CONFIRMED_CUSTOMER"
    assert stored["customer_evidence"][0]["official_source"] is True
    assert database.deep_research_is_fresh(person_id, cache_days=30)
    report_row = database.report_rows(run_id)[0]
    assert json.loads(report_row["dr_customer_evidence"])[0]["strength"] == "strong"

    database.update_person_resolution(
        person_id,
        company_name="Renamed Company",
        status="manual_verified",
        domain="renamed.example.com",
    )
    assert database.deep_research(person_id) is None


def test_customer_story_result_is_persisted_and_rendered_as_servicenow_yes(tmp_path) -> None:
    database = WorkflowDatabase(tmp_path / "workflow.db")
    database.initialize()
    run_id = database.create_run(
        "customers.csv",
        [{"person_name": "Ada", "linkedin_url": "https://linkedin.com/in/ada"}],
    )
    person_id = database.people_for_run(run_id)[0]["id"]
    database.update_person_resolution(
        person_id, company_name="Example", status="verified", domain="example.com"
    )
    database.claim_deep_research(
        person_id=person_id,
        run_id=run_id,
        company_name="Example",
        official_domain="example.com",
    )
    values = _result_for("No relevant product information.").as_storage_values()
    values.update(
        {
            "servicenow_customer_page_found": True,
            "servicenow_customer_page_url": "https://www.servicenow.com/in/customers/example.html",
        }
    )
    database.complete_deep_research(person_id, values)

    stored = database.deep_research(person_id)
    row = database.report_rows(run_id)[0]
    html = dashboard._simplified_results_table([row])

    assert stored["servicenow_customer_page_found"] == 1
    assert row["dr_servicenow_customer_page_found"] == 1
    assert "ServiceNow customer" in html
    assert ">Yes<" in html
    assert "View official page" in html
    assert 'aria-label="Serial number 1">1</span>' in html


def test_start_endpoint_returns_cached_result_without_adding_task(monkeypatch) -> None:
    existing = {
        "request_status": "completed",
        "classification_status": "LIKELY_CUSTOMER",
        "confidence": 72,
    }

    class Database:
        def person(self, _person_id):
            return {"id": 10, "run_id": 3, "company_name": "Example", "company_domain": "example.com"}

        def deep_research(self, _person_id):
            return existing

        def deep_research_is_fresh(self, _person_id, _days):
            return True

    monkeypatch.setattr(dashboard, "DATABASE", Database())
    monkeypatch.setattr(
        dashboard,
        "load_settings",
        lambda: SimpleNamespace(
            llm_api_key="test-key",
            llm_provider="glm",
            llm_model="z-ai/glm-5.3",
            deep_research_cache_days=30,
        ),
    )
    tasks = BackgroundTasks()

    response = dashboard.start_deep_research(10, tasks, force=False, research_depth="deep")
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["cached"] is True
    assert payload["research"]["classification_status"] == "LIKELY_CUSTOMER"
    assert not tasks.tasks


def test_start_endpoint_reports_missing_gemini_configuration(monkeypatch) -> None:
    class Database:
        def person(self, _person_id):
            return {"id": 10, "run_id": 3, "company_name": "Example", "company_domain": "example.com"}

        def deep_research(self, _person_id):
            return None

    monkeypatch.setattr(dashboard, "DATABASE", Database())
    monkeypatch.setattr(
        dashboard,
        "load_settings",
        lambda: SimpleNamespace(llm_api_key=None, llm_provider="gemini"),
    )

    response = dashboard.start_deep_research(
        10, BackgroundTasks(), force=False, research_depth="deep"
    )
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload["success"] is False
    assert "GEMINI_API_KEY" in payload["error"]


def test_start_endpoint_resolves_missing_domain_with_ai(monkeypatch) -> None:
    updates: list[dict[str, object]] = []

    class Database:
        def person(self, _person_id):
            return {
                "id": 10,
                "run_id": 3,
                "company_name": "Maersk Oil",
                "company_domain": "",
                "company_linkedin_url": "",
                "resolution_status": "manual_verified",
                "resolution_error": "",
            }

        def deep_research(self, _person_id):
            return {"request_status": "running"} if updates else None

        def deep_research_is_fresh(self, *_args):
            return False

        def update_person_resolution(self, person_id, **values):
            updates.append({"person_id": person_id, **values})

        def upsert_check(self, person_id, run_id, values):
            updates.append({"person_id": person_id, "run_id": run_id, **values})

        def claim_deep_research(self, **values):
            updates.append(values)
            return True

    monkeypatch.setattr(dashboard, "DATABASE", Database())
    monkeypatch.setattr(dashboard, "validate_public_domain", lambda _domain: None)
    monkeypatch.setattr(
        dashboard,
        "resolve_company_headquarters",
        lambda *_args, **_kwargs: {
            "success": True,
            "company_domain": "maerskoil.com",
            "headquarters": "Copenhagen",
            "country": "Denmark",
            "country_code": "DK",
        },
    )
    monkeypatch.setattr(
        dashboard,
        "load_settings",
        lambda: SimpleNamespace(
            llm_api_key="key",
            llm_provider="gemini",
            llm_model="gemini-3-flash-preview",
            llm_base_url="https://example.test",
            deep_research_cache_days=30,
        ),
    )
    tasks = BackgroundTasks()

    response = dashboard.start_deep_research(
        10, tasks, force=False, research_depth="deep"
    )

    assert response.status_code == 202
    assert any(item.get("domain") == "maerskoil.com" for item in updates)
    assert any(item.get("headquarters") == "Copenhagen" for item in updates)
    assert any(item.get("official_domain") == "maerskoil.com" for item in updates)
    assert len(tasks.tasks) == 1


def test_deep_research_button_stays_enabled_when_only_domain_is_missing() -> None:
    html = dashboard._deep_research_cell(
        {
            "person_id": 10,
            "company_name": "Maersk Oil",
            "company_domain": "",
            "dr_request_status": "idle",
        }
    )

    assert "Deep Research" in html
    assert " disabled" not in html
    assert "Official domain will be found with AI" in html


def test_result_ui_shows_deep_research_action_and_evidence() -> None:
    row = {
        "person_id": 1,
        "person_name": "Ada Example",
        "linkedin_url": "https://linkedin.com/in/ada-example",
        "headline": "IT Director",
        "company_name": "Example Company",
        "company_domain": "example.com",
        "servicenow_customer": "No",
        "n8n_status": "",
        "n8n_response": "",
        "dr_request_status": "completed",
        "dr_classification_status": "PARTNER_ONLY",
        "dr_confidence": 91,
        "dr_summary": "Only partner evidence was found; this does not prove internal use.",
        "dr_customer_evidence": "[]",
        "dr_partner_evidence": json.dumps(
            [
                {
                    "url": "https://example.com/partners",
                    "page_title": "Our ServiceNow services",
                    "evidence": "We help clients implement ServiceNow.",
                    "strength": "strong",
                    "official_source": True,
                    "citation_grounded": True,
                }
            ]
        ),
        "dr_ambiguous_evidence": "[]",
        "dr_sources_checked": 9,
        "dr_relevant_sources": 1,
        "dr_researched_at": "2026-09-04T08:00:00+00:00",
    }

    html = dashboard._simplified_results_table([row])

    assert "Deep Research" in html
    assert "Partner evidence only" in html
    assert "91% confidence" in html
    assert "View research" in html
    assert "Run again" in html
    assert "Official" in html
    assert 'href="https://example.com/partners"' in html


def test_result_ui_does_not_link_legacy_ungrounded_citation() -> None:
    html = dashboard._deep_research_evidence_list(
        [
            {
                "url": "https://example.com/invented",
                "page_title": "Old cached citation",
                "evidence": "Legacy model output",
            }
        ],
        "No evidence",
    )

    assert 'href="https://example.com/invented"' not in html
    assert "Citation not verified; run research again." in html
