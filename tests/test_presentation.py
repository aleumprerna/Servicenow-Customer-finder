import json

from workflow.presentation import parse_n8n_evidence


def test_n8n_evidence_extracts_status_source_and_deduplicated_citations() -> None:
    citation = {
        "type": "Company verification",
        "title": "Deliveroo",
        "url": "https://linkedin.com/in/example",
    }
    response = json.dumps(
        {
            "success": True,
            "result": {
                "servicenow_user": "Likely",
                "status_source": "apollo",
                "evidence_strength": "Apollo-only signal",
                "verification_status": "No official ServiceNow page found",
                "evidence_note": "Apollo lists ServiceNow for this company.",
                "research_sources": ["Google", "Apollo"],
                "citations": [citation],
            },
            "citations": [citation],
        }
    )
    evidence = parse_n8n_evidence("sent", response)
    assert evidence.servicenow_status == "Likely"
    assert evidence.source_type == "Apollo"
    assert evidence.verification_status == "No official ServiceNow page found"
    assert len(evidence.citations) == 1
    assert evidence.citations[0].url == "https://linkedin.com/in/example"


def test_n8n_evidence_handles_truncated_json_without_printing_it() -> None:
    evidence = parse_n8n_evidence("sent", '{"result":{"keywords":["unfinished')
    assert evidence.parse_error is True
    assert "invalid JSON" in evidence.verification_status


def test_n8n_evidence_labels_no_source_and_official_servicenow() -> None:
    no_source = parse_n8n_evidence(
        "sent", json.dumps({"result": {"status_source": "none"}})
    )
    official = parse_n8n_evidence(
        "sent", json.dumps({"result": {"status_source": "official_servicenow"}})
    )
    assert no_source.source_type == "No confirming source"
    assert official.source_type == "Official ServiceNow"


def test_n8n_evidence_builds_web_and_customer_page_tags() -> None:
    evidence = parse_n8n_evidence(
        "received",
        json.dumps(
            {
                "result": {
                    "status_source": "official_servicenow",
                    "research_sources": ["Google", "Apollo"],
                    "evidence_urls": [
                        {
                            "type": "Customer",
                            "url": "https://www.servicenow.com/customers/example.html",
                        }
                    ],
                }
            }
        ),
    )
    assert evidence.source_tags == ("Web search", "ServiceNow customer page")
    assert evidence.citations[0].url.endswith("/customers/example.html")


def test_n8n_evidence_recognizes_official_partner_citation() -> None:
    evidence = parse_n8n_evidence(
        "received",
        json.dumps(
            {
                "result": {
                    "citations": [
                        {
                            "type": "Official ServiceNow Partner",
                            "title": "Example partner profile",
                            "url": "https://www.servicenow.com/partners/example.html",
                        }
                    ]
                }
            }
        ),
    )
    assert evidence.source_tags == ("ServiceNow partner page",)


def test_n8n_evidence_recognizes_explicit_partner_source() -> None:
    evidence = parse_n8n_evidence(
        "received",
        json.dumps({"result": {"status_source": "official_servicenow_partner"}}),
    )
    assert evidence.source_tags == ("ServiceNow partner page",)
