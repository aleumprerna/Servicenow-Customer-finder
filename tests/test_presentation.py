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
