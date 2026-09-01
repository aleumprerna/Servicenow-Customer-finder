from __future__ import annotations

import argparse
import sys

from clients.apollo import ApolloClient, normalize_url
from config import PROJECT_ROOT, load_settings
from workflow.database import WorkflowDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Apollo People Search candidates")
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    settings = load_settings()
    client = ApolloClient(
        api_key=settings.apollo_api_key,
        base_url=settings.apollo_base_url,
        timeout_seconds=settings.apollo_timeout_seconds,
        max_retries=settings.apollo_max_retries,
        match_threshold=settings.apollo_match_threshold,
    )
    database = WorkflowDatabase(PROJECT_ROOT / "data" / "workflow.db")
    rows = [
        row
        for row in database.report_rows(args.run_id)
        if row["resolution_status"] not in {
            "apollo_structurally_verified",
            "manual_verified",
            "manual_verified",
        }
    ]
    for row in rows:
        payload = client._request(
            "POST",
            "/mixed_people/api_search",
            params={"q_keywords": row["person_name"], "page": 1, "per_page": 10},
        )
        people = payload.get("people") if isinstance(payload, dict) else []
        print(f"\n{row['person_name']} | input={normalize_url(row['linkedin_url'])}")
        for person in people if isinstance(people, list) else []:
            if not isinstance(person, dict):
                continue
            organization = person.get("organization")
            organization_name = (
                organization.get("name", "") if isinstance(organization, dict) else ""
            )
            print(
                "  "
                f"name={person.get('name') or ''} | "
                f"linkedin={normalize_url(str(person.get('linkedin_url') or '')) or '<hidden>'} | "
                f"title={person.get('title') or ''} | organization={organization_name or '<missing>'} | "
                f"id={person.get('id') or person.get('person_id') or ''}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
