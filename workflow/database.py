from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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
                CREATE INDEX IF NOT EXISTS idx_people_run ON people(run_id);
                CREATE INDEX IF NOT EXISTS idx_checks_run ON company_checks(run_id);
                """
            )
            self._ensure_column(conn, "people", "company_domain", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "people", "company_linkedin_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "company_checks", "screenshot_path", "TEXT NOT NULL DEFAULT ''")

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
                        supplied_company_name, raw_input
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        row_number,
                        person["person_name"],
                        person["linkedin_url"],
                        person.get("headline", ""),
                        person.get("company_name", ""),
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
            conn.execute(
                """UPDATE people SET company_name = ?, company_domain = ?, company_linkedin_url = ?,
                resolution_status = ?, resolution_error = ?
                WHERE id = ?""",
                (company_name, domain, company_linkedin_url, status, error, person_id),
            )

    def reset_check_for_company_change(
        self, person_id: int, run_id: int, company_name: str
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
                "headquarters": "",
                "country": "",
                "country_code": "",
                "apollo_company_name": "",
                "error_message": "",
                "checked_at": "",
                "n8n_status": "not_sent",
                "n8n_response": "",
                "n8n_sent_at": "",
                "n8n_received_at": "",
            },
        )

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
                   c.n8n_status, c.n8n_response, c.n8n_sent_at, c.n8n_received_at
            FROM people p JOIN runs r ON r.id = p.run_id
            LEFT JOIN company_checks c ON c.person_id = p.id
        """
        parameters: tuple[Any, ...] = ()
        if run_id is not None:
            query += " WHERE p.run_id = ?"
            parameters = (run_id,)
        query += " ORDER BY r.id DESC, p.id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, parameters).fetchall()]

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
            conn.execute("DELETE FROM company_checks")
            conn.execute("DELETE FROM people")
            conn.execute("DELETE FROM runs")
