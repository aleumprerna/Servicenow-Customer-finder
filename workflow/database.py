from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkflowDatabase:
    """Small SQLite repository. SQLite keeps the local app zero-configuration."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY,
                    source_file TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'uploaded',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    collection_log TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS people (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    row_number INTEGER NOT NULL,
                    person_name TEXT NOT NULL,
                    linkedin_url TEXT NOT NULL,
                    headline TEXT NOT NULL DEFAULT '',
                    supplied_company_name TEXT NOT NULL DEFAULT '',
                    company_name TEXT NOT NULL DEFAULT '',
                    company_domain TEXT NOT NULL DEFAULT '',
                    company_linkedin_url TEXT NOT NULL DEFAULT '',
                    resolution_status TEXT NOT NULL DEFAULT 'pending',
                    resolution_error TEXT NOT NULL DEFAULT '',
                    raw_input TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS company_checks (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    person_id INTEGER NOT NULL UNIQUE REFERENCES people(id) ON DELETE CASCADE,
                    company_name TEXT NOT NULL,
                    servicenow_customer TEXT NOT NULL DEFAULT '',
                    servicenow_matched_name TEXT NOT NULL DEFAULT '',
                    screenshot_path TEXT NOT NULL DEFAULT '',
                    match_score TEXT NOT NULL DEFAULT '',
                    check_status TEXT NOT NULL DEFAULT '',
                    headquarters TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    country_code TEXT NOT NULL DEFAULT '',
                    apollo_company_name TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    checked_at TEXT NOT NULL DEFAULT '',
                    n8n_status TEXT NOT NULL DEFAULT 'not_sent',
                    n8n_response TEXT NOT NULL DEFAULT '',
                    n8n_sent_at TEXT NOT NULL DEFAULT '',
                    n8n_received_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS deep_research_results (
                    id INTEGER PRIMARY KEY,
                    person_id INTEGER NOT NULL UNIQUE REFERENCES people(id) ON DELETE CASCADE,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    company_name TEXT NOT NULL,
                    official_domain TEXT NOT NULL,
                    request_status TEXT NOT NULL DEFAULT 'idle',
                    classification_status TEXT NOT NULL DEFAULT '',
                    confidence INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    customer_evidence TEXT NOT NULL DEFAULT '[]',
                    partner_evidence TEXT NOT NULL DEFAULT '[]',
                    ambiguous_evidence TEXT NOT NULL DEFAULT '[]',
                    visited_urls TEXT NOT NULL DEFAULT '[]',
                    sources_checked INTEGER NOT NULL DEFAULT 0,
                    relevant_sources INTEGER NOT NULL DEFAULT 0,
                    research_depth TEXT NOT NULL DEFAULT 'deep',
                    model_provider TEXT NOT NULL DEFAULT '',
                    servicenow_customer_page_found INTEGER NOT NULL DEFAULT -1,
                    servicenow_customer_page_url TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    researched_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_people_run ON people(run_id);
                CREATE INDEX IF NOT EXISTS idx_checks_run ON company_checks(run_id);
                CREATE INDEX IF NOT EXISTS idx_deep_research_run ON deep_research_results(run_id);
                """
            )
            self._ensure_column(conn, "people", "company_domain", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "people", "company_linkedin_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "company_checks", "screenshot_path", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                conn, "deep_research_results", "servicenow_customer_page_found",
                "INTEGER NOT NULL DEFAULT -1",
            )
            self._ensure_column(
                conn, "deep_research_results", "servicenow_customer_page_url",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn, "deep_research_results", "visited_urls",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            stale_cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
            conn.execute(
                """UPDATE deep_research_results
                SET request_status = 'failed', last_error = 'Research was interrupted; run it again.',
                    updated_at = ?
                WHERE request_status = 'running' AND started_at < ?""",
                (now(), stale_cutoff),
            )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_run(self, source_file: str, people: Iterable[dict[str, Any]]) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (source_file, created_at) VALUES (?, ?)", (source_file, now())
            )
            run_id = int(cursor.lastrowid)
            for row_number, person in enumerate(people, start=2):
                conn.execute(
                    """INSERT INTO people (
                        run_id, row_number, person_name, linkedin_url, headline,
                        supplied_company_name, company_name, resolution_status, raw_input
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        row_number,
                        person["person_name"],
                        person["linkedin_url"],
                        person.get("headline", ""),
                        person.get("company_name", ""),
                        person.get("company_name", ""),
                        "csv_supplied" if person.get("company_name", "").strip() else "pending",
                        json.dumps(person.get("raw_input", {}), ensure_ascii=False),
                    ),
                )
            return run_id

    def run(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            item = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return dict(item) if item else None

    def update_run(self, run_id: int, **values: str) -> None:
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*values.values(), run_id))

    def person(self, person_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
            return dict(row) if row else None

    def people_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM people WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
            return [dict(row) for row in rows]

    def update_person_resolution(
        self, person_id: int, *, company_name: str, status: str, error: str = "",
        domain: str = "", company_linkedin_url: str = "",
    ) -> None:
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT company_name, company_domain FROM people WHERE id = ?", (person_id,)
            ).fetchone()
            conn.execute(
                """UPDATE people SET company_name = ?, company_domain = ?, company_linkedin_url = ?,
                resolution_status = ?, resolution_error = ?
                WHERE id = ?""",
                (company_name, domain, company_linkedin_url, status, error, person_id),
            )
            if previous and (
                str(previous["company_name"]) != company_name
                or str(previous["company_domain"]) != domain
            ):
                conn.execute("DELETE FROM deep_research_results WHERE person_id = ?", (person_id,))

    def reset_check_for_company_change(
        self,
        person_id: int,
        run_id: int,
        company_name: str,
        *,
        headquarters: str = "",
        country: str = "",
        country_code: str = "",
    ) -> None:
        """Queue only a corrected company for fresh enrichment and automation."""

        self.upsert_check(
            person_id,
            run_id,
            {
                "company_name": company_name,
                "servicenow_customer": "",
                "servicenow_matched_name": "",
                "screenshot_path": "",
                "match_score": "",
                "check_status": "pending",
                "headquarters": headquarters,
                "country": country,
                "country_code": country_code,
                "apollo_company_name": "",
                "error_message": "",
                "checked_at": "",
                "n8n_status": "not_sent",
                "n8n_response": "",
                "n8n_sent_at": "",
                "n8n_received_at": "",
            },
        )
        with self.connect() as conn:
            conn.execute("DELETE FROM deep_research_results WHERE person_id = ?", (person_id,))

    def upsert_check(self, person_id: int, run_id: int, values: dict[str, str]) -> None:
        columns = ["person_id", "run_id", *values.keys()]
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{key} = excluded.{key}" for key in values)
        with self.connect() as conn:
            conn.execute(
                f"""INSERT INTO company_checks ({", ".join(columns)}) VALUES ({placeholders})
                ON CONFLICT(person_id) DO UPDATE SET {assignments}""",
                (person_id, run_id, *values.values()),
            )

    def unsent_negative_checks(self, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT c.*, p.person_name, p.linkedin_url, p.headline
                FROM company_checks c JOIN people p ON p.id = c.person_id
                WHERE c.run_id = ? AND lower(c.servicenow_customer) = 'no'
                    AND c.check_status = 'completed'
                    AND c.n8n_status IN ('not_sent', 'not_configured', 'failed')
                ORDER BY c.id""",
                (run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_n8n_result(
        self, person_id: int, *, status: str, response: str, sent: bool = False, received: bool = False
    ) -> None:
        values: dict[str, str] = {"n8n_status": status, "n8n_response": response}
        if sent:
            values["n8n_sent_at"] = now()
        if received:
            values["n8n_received_at"] = now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE company_checks SET {assignments} WHERE person_id = ?",
                (*values.values(), person_id),
            )

    def mark_n8n_for_retry(self, person_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE company_checks SET n8n_status = 'failed' WHERE person_id = ?",
                (person_id,),
            )

    def report_rows(self, run_id: int | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT r.id AS run_id, r.status AS run_status, r.created_at,
                   p.id AS person_id, p.row_number, p.person_name, p.linkedin_url, p.headline,
                   p.company_name, p.company_domain, p.company_linkedin_url,
                   p.resolution_status, p.resolution_error,
                   c.company_name AS check_company_name,
                   c.servicenow_customer, c.servicenow_matched_name, c.match_score,
                   c.screenshot_path,
                   c.check_status, c.headquarters, c.country, c.country_code,
                   c.apollo_company_name, c.error_message, c.checked_at,
                   c.n8n_status, c.n8n_response, c.n8n_sent_at, c.n8n_received_at,
                   d.request_status AS dr_request_status,
                   d.classification_status AS dr_classification_status,
                   d.confidence AS dr_confidence,
                   d.summary AS dr_summary,
                    d.customer_evidence AS dr_customer_evidence,
                    d.partner_evidence AS dr_partner_evidence,
                    d.ambiguous_evidence AS dr_ambiguous_evidence,
                    d.visited_urls AS dr_visited_urls,
                    d.sources_checked AS dr_sources_checked,
                   d.relevant_sources AS dr_relevant_sources,
                   d.research_depth AS dr_research_depth,
                   d.model_provider AS dr_model_provider,
                   d.servicenow_customer_page_found AS dr_servicenow_customer_page_found,
                   d.servicenow_customer_page_url AS dr_servicenow_customer_page_url,
                   d.started_at AS dr_started_at,
                   d.researched_at AS dr_researched_at,
                   d.last_error AS dr_last_error
            FROM people p JOIN runs r ON r.id = p.run_id
            LEFT JOIN company_checks c ON c.person_id = p.id
            LEFT JOIN deep_research_results d ON d.person_id = p.id
        """
        parameters: tuple[Any, ...] = ()
        if run_id is not None:
            query += " WHERE p.run_id = ?"
            parameters = (run_id,)
        query += " ORDER BY r.id DESC, p.id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, parameters).fetchall()]

    @staticmethod
    def _research_record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        for key in ("customer_evidence", "partner_evidence", "ambiguous_evidence", "visited_urls"):
            try:
                parsed = json.loads(record.get(key) or "[]")
                record[key] = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                record[key] = []
        return record

    def deep_research(self, person_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM deep_research_results WHERE person_id = ?", (person_id,)
            ).fetchone()
        return self._research_record(row)

    def begin_deep_research(
        self,
        *,
        person_id: int,
        run_id: int,
        company_name: str,
        official_domain: str,
        research_depth: str = "deep",
        model_provider: str = "",
    ) -> dict[str, Any]:
        self.claim_deep_research(
            person_id=person_id,
            run_id=run_id,
            company_name=company_name,
            official_domain=official_domain,
            research_depth=research_depth,
            model_provider=model_provider,
        )
        return self.deep_research(person_id) or {}

    def claim_deep_research(
        self,
        *,
        person_id: int,
        run_id: int,
        company_name: str,
        official_domain: str,
        research_depth: str = "deep",
        model_provider: str = "",
    ) -> bool:
        """Atomically claim a research job, returning False if one is already running."""

        timestamp = now()
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO deep_research_results (
                    person_id, run_id, company_name, official_domain, request_status,
                    research_depth, model_provider, started_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, '')
                ON CONFLICT(person_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    company_name = excluded.company_name,
                    official_domain = excluded.official_domain,
                    request_status = 'running',
                    research_depth = excluded.research_depth,
                    model_provider = excluded.model_provider,
                    servicenow_customer_page_found = -1,
                    servicenow_customer_page_url = '',
                    started_at = excluded.started_at,
                    updated_at = excluded.updated_at,
                    last_error = ''
                WHERE deep_research_results.request_status <> 'running'""",
                (
                    person_id,
                    run_id,
                    company_name,
                    official_domain,
                    research_depth,
                    model_provider,
                    timestamp,
                    timestamp,
                ),
            )
            return cursor.rowcount > 0

    def complete_deep_research(self, person_id: int, values: dict[str, Any]) -> None:
        timestamp = now()
        with self.connect() as conn:
            conn.execute(
                """UPDATE deep_research_results SET
                    request_status = 'completed', classification_status = ?, confidence = ?,
                    summary = ?, customer_evidence = ?, partner_evidence = ?,
                    ambiguous_evidence = ?, visited_urls = ?, sources_checked = ?, relevant_sources = ?,
                    research_depth = ?, model_provider = ?,
                    servicenow_customer_page_found = ?, servicenow_customer_page_url = ?,
                    researched_at = ?,
                    updated_at = ?, last_error = ''
                WHERE person_id = ?""",
                (
                    str(values.get("classification_status") or ""),
                    int(values.get("confidence") or 0),
                    str(values.get("summary") or ""),
                    json.dumps(values.get("customer_evidence") or [], ensure_ascii=False),
                    json.dumps(values.get("partner_evidence") or [], ensure_ascii=False),
                    json.dumps(values.get("ambiguous_evidence") or [], ensure_ascii=False),
                    json.dumps(values.get("visited_urls") or [], ensure_ascii=False),
                    int(values.get("sources_checked") or 0),
                    int(values.get("relevant_sources") or 0),
                    str(values.get("research_depth") or "deep"),
                    str(values.get("model_provider") or ""),
                    (
                        -1
                        if values.get("servicenow_customer_page_found") is None
                        else int(bool(values.get("servicenow_customer_page_found")))
                    ),
                    str(values.get("servicenow_customer_page_url") or ""),
                    str(values.get("researched_at") or timestamp),
                    timestamp,
                    person_id,
                ),
            )

    def fail_deep_research(self, person_id: int, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE deep_research_results
                SET request_status = 'failed', last_error = ?, updated_at = ?
                WHERE person_id = ?""",
                (str(error)[:1000], now(), person_id),
            )

    def deep_research_is_fresh(self, person_id: int, cache_days: int) -> bool:
        record = self.deep_research(person_id)
        if not record or record.get("request_status") != "completed":
            return False
        researched_at = str(record.get("researched_at") or "")
        try:
            researched = datetime.fromisoformat(researched_at)
            if researched.tzinfo is None:
                researched = researched.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        age_seconds = (datetime.now(timezone.utc) - researched).total_seconds()
        return 0 <= age_seconds <= int(cache_days) * 86400

    def summary(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.*, COUNT(p.id) AS people_count,
                    SUM(CASE WHEN lower(c.servicenow_customer) = 'yes' THEN 1 ELSE 0 END) AS yes_count,
                    SUM(CASE WHEN lower(c.servicenow_customer) = 'no' THEN 1 ELSE 0 END) AS no_count
                FROM runs r LEFT JOIN people p ON p.run_id = r.id
                LEFT JOIN company_checks c ON c.person_id = p.id
                GROUP BY r.id ORDER BY r.id DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    def clear_all(self) -> None:
        """Remove workflow data while retaining the SQLite schema and configuration."""

        with self.connect() as conn:
            conn.execute("DELETE FROM deep_research_results")
            conn.execute("DELETE FROM company_checks")
            conn.execute("DELETE FROM people")
            conn.execute("DELETE FROM runs")
