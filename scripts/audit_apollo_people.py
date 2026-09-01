from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from clients.apollo import ApolloClient, ApolloError
from config import PROJECT_ROOT, load_settings
from workflow.database import WorkflowDatabase
from workflow.person_company import PersonCompanyResolver


def value(row: dict[str, str], *names: str) -> str:
    normalized = {key.casefold().strip(): item for key, item in row.items()}
    return next((normalized[name.casefold()].strip() for name in names if name.casefold() in normalized), "")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Audit Apollo person-to-current-company matches")
    parser.add_argument("--input", type=Path, default=Path("companies.csv"))
    parser.add_argument("--run-id", type=int, help="Read people from a stored dashboard run")
    parser.add_argument(
        "--only-unresolved",
        action="store_true",
        help="With --run-id, audit only rows that are not already Apollo-verified",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--headquarters",
        action="store_true",
        help="Also enrich each resolved company and verify its headquarters",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the complete audit to a CSV file",
    )
    parser.add_argument(
        "--rows",
        help="Comma-separated one-based CSV row numbers to audit, for example 12,15",
    )
    args = parser.parse_args()
    settings = load_settings()
    client = ApolloClient(
        api_key=settings.apollo_api_key,
        base_url=settings.apollo_base_url,
        timeout_seconds=settings.apollo_timeout_seconds,
        max_retries=settings.apollo_max_retries,
        match_threshold=settings.apollo_match_threshold,
    )
    if args.run_id:
        database = WorkflowDatabase(PROJECT_ROOT / "data" / "workflow.db")
        rows = database.report_rows(args.run_id)
        if args.only_unresolved:
            rows = [
                row
                for row in rows
                if row["resolution_status"] not in {
                    "apollo_structurally_verified",
                    "manual_verified",
                    "manual_verified",
                }
            ]
    else:
        with args.input.open(newline="", encoding="utf-8-sig") as file:
            rows = list(csv.DictReader(file))
    numbered_rows = list(enumerate(rows, start=1))
    if args.rows:
        selected = {int(value.strip()) for value in args.rows.split(",") if value.strip()}
        numbered_rows = [(number, row) for number, row in numbered_rows if number in selected]
    elif args.limit:
        numbered_rows = numbered_rows[: args.limit]

    resolver = PersonCompanyResolver(client)
    audit_rows: list[dict[str, str | int]] = []
    print("row | input person | resolved company | headquarters | workflow status | details")
    for number, row in numbered_rows:
        person_name = value(row, "Name", "person_name")
        linkedin_url = value(row, "Profile URL", "linkedin_url")
        result = resolver.resolve(
            person_name=person_name,
            linkedin_url=linkedin_url,
            supplied_company_name=value(row, "Company", "company_name"),
            headline=value(row, "Headline", "headline"),
        )
        headquarters = ""
        country = ""
        country_code = ""
        headquarters_status = "not_requested"
        details = result.error
        if args.headquarters and result.company_name:
            try:
                company = client.enrich(
                    result.company_name,
                    linkedin_url=result.company_linkedin_url,
                    domain=result.domain,
                )
                headquarters = company.headquarters
                country = company.country
                country_code = company.country_code
                headquarters_status = "verified"
            except ApolloError as exc:
                headquarters_status = "unresolved"
                details = "; ".join(item for item in (details, str(exc)) if item)
        elif args.headquarters:
            headquarters_status = "skipped_no_verified_company"
        audit_rows.append(
            {
                "row": number,
                "person_name": person_name,
                "person_linkedin_url": linkedin_url,
                "company_name": result.company_name,
                "company_domain": result.domain,
                "company_linkedin_url": result.company_linkedin_url,
                "company_status": result.status,
                "headquarters": headquarters,
                "country": country,
                "country_code": country_code,
                "headquarters_status": headquarters_status,
                "details": details,
            }
        )
        print(
            f"{number} | {person_name} | {result.company_name or '<missing>'} | "
            f"{headquarters or '<missing>'} | {result.status} | {details or 'verified'}"
        )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=list(audit_rows[0]) if audit_rows else [])
            if audit_rows:
                writer.writeheader()
                writer.writerows(audit_rows)
        print(f"Audit CSV: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
