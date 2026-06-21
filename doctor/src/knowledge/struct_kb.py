"""
Structured knowledge base backed by SQLite.

Stores three kinds of structured knowledge:
- http_status_codes: HTTP status code -> category, description
- error_patterns: regex pattern -> bug category, description
- framework_practices: framework best practices by version

Supports sync operations with aiqlite for async compatibility.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── Table DDL ───────────────────────────────────────────────────────

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS http_status_codes (
        code        INTEGER PRIMARY KEY,
        category    TEXT NOT NULL,
        description TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS error_patterns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern     TEXT NOT NULL UNIQUE,
        category    TEXT NOT NULL,
        description TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS framework_practices (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        framework     TEXT NOT NULL,
        version       TEXT NOT NULL DEFAULT '',
        practice_type TEXT NOT NULL,
        description   TEXT NOT NULL
    )
    """,
]

# Pre-built index DDL
INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_ep_category ON error_patterns(category)",
    "CREATE INDEX IF NOT EXISTS idx_fp_framework ON framework_practices(framework)",
    "CREATE INDEX IF NOT EXISTS idx_fp_practice_type ON framework_practices(practice_type)",
]


class StructKnowledgeBase:
    """
    Structured knowledge base backed by SQLite.

    Usage::

        kb = StructKnowledgeBase("data/struct_kb.db")
        kb.add_http_status(404, "client_error", "Not Found")
        results = kb.query_http_status(500)
    """

    def __init__(self, db_path: str | Path = "data/struct_kb.db"):
        """
        Initialize the structured knowledge base.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ── Connection management ───────────────────────────────────────

    @property
    def conn(self) -> sqlite3.Connection:
        """Get or create the SQLite connection (lazy, per-thread)."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Initialization ──────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create tables and indexes if they don't exist."""
        conn = self.conn
        for ddl in DDL_STATEMENTS:
            conn.execute(ddl)
        for idx in INDEX_DDL:
            conn.execute(idx)
        conn.commit()
        logger.info("StructKB database initialized at %s", self.db_path)

    # ── HTTP Status Codes ───────────────────────────────────────────

    def add_http_status(self, code: int, category: str, description: str) -> None:
        """Insert or replace an HTTP status code entry."""
        self.conn.execute(
            "INSERT OR REPLACE INTO http_status_codes"
            " (code, category, description) VALUES (?, ?, ?)",
            (code, category, description),
        )
        self.conn.commit()

    def query_http_status(self, code: int) -> dict[str, Any] | None:
        """Look up an HTTP status code. Returns None if not found."""
        row = self.conn.execute(
            "SELECT code, category, description FROM http_status_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def query_http_by_category(self, category: str) -> list[dict[str, Any]]:
        """Get all HTTP status codes in a category (e.g. 'client_error')."""
        rows = self.conn.execute(
            "SELECT code, category, description FROM http_status_codes"
            " WHERE category = ? ORDER BY code",
            (category,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Error Patterns ──────────────────────────────────────────────

    def add_error_pattern(self, pattern: str, category: str, description: str) -> None:
        """Insert an error pattern (regex)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO error_patterns"
            " (pattern, category, description) VALUES (?, ?, ?)",
            (pattern, category, description),
        )
        self.conn.commit()

    def query_error_patterns(self, category: str | None = None) -> list[dict[str, Any]]:
        """Get error patterns, optionally filtered by category."""
        if category:
            rows = self.conn.execute(
                "SELECT id, pattern, category, description FROM error_patterns"
                " WHERE category = ? ORDER BY id",
                (category,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, pattern, category, description FROM error_patterns ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def match_error_pattern(self, error_message: str) -> dict[str, Any] | None:
        """
        Try to match an error message against all stored patterns.

        Returns the first matching pattern dict, or None.
        """
        rows = self.conn.execute(
            "SELECT id, pattern, category, description FROM error_patterns ORDER BY id"
        ).fetchall()

        for row in rows:
            try:
                if re.search(row["pattern"], error_message, re.IGNORECASE):
                    return dict(row)
            except re.error:
                logger.warning("Invalid regex pattern in DB: %s", row["pattern"])
                continue

        return None

    # ── Framework Practices ─────────────────────────────────────────

    def add_framework_practice(
        self,
        framework: str,
        practice_type: str,
        description: str,
        version: str = "",
    ) -> None:
        """Insert a framework best practice."""
        self.conn.execute(
            "INSERT INTO framework_practices"
            " (framework, version, practice_type, description) VALUES (?, ?, ?, ?)",
            (framework, version, practice_type, description),
        )
        self.conn.commit()

    def query_framework_practices(
        self,
        framework: str | None = None,
        practice_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query framework practices with optional filters.

        Args:
            framework: Filter by framework name (e.g. 'FastAPI').
            practice_type: Filter by practice type (e.g. 'performance').

        Returns:
            List of matching practice dicts.
        """
        query = (
            "SELECT id, framework, version, practice_type, description"
            " FROM framework_practices WHERE 1=1"
        )
        params: list[Any] = []

        if framework:
            query += " AND framework = ?"
            params.append(framework)
        if practice_type:
            query += " AND practice_type = ?"
            params.append(practice_type)

        query += " ORDER BY id"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Bulk Load ───────────────────────────────────────────────────

    def bulk_load_from_yaml(self, file_path: str | Path) -> dict[str, int]:
        """
        Load initial knowledge from a YAML file.

        Expected YAML structure::

            http_status_codes:
              - code: 400
                category: client_error
                description: Bad Request
              - ...

            error_patterns:
              - pattern: "TypeError"
                category: frontend_crash
                description: JavaScript type error
              - ...

            framework_practices:
              - framework: FastAPI
                version: "0.115"
                practice_type: async
                description: Use async def for endpoints
              - ...

        Args:
            file_path: Path to the YAML file.

        Returns:
            Dict with counts: {"http": N, "patterns": N, "practices": N}.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            logger.warning("Bulk load file not found: %s", file_path)
            return {"http": 0, "patterns": 0, "practices": 0}

        with open(file_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        counts: dict[str, int] = {"http": 0, "patterns": 0, "practices": 0}

        # HTTP status codes
        for entry in data.get("http_status_codes", []):
            self.add_http_status(
                code=int(entry["code"]),
                category=str(entry["category"]),
                description=str(entry["description"]),
            )
            counts["http"] += 1

        # Error patterns
        for entry in data.get("error_patterns", []):
            self.add_error_pattern(
                pattern=str(entry["pattern"]),
                category=str(entry["category"]),
                description=str(entry["description"]),
            )
            counts["patterns"] += 1

        # Framework practices
        for entry in data.get("framework_practices", []):
            self.add_framework_practice(
                framework=str(entry["framework"]),
                version=str(entry.get("version", "")),
                practice_type=str(entry["practice_type"]),
                description=str(entry["description"]),
            )
            counts["practices"] += 1

        logger.info(
            "Bulk loaded from %s: %d HTTP, %d patterns, %d practices",
            file_path,
            counts["http"],
            counts["patterns"],
            counts["practices"],
        )
        return counts

    def export_to_yaml(self, file_path: str | Path) -> None:
        """
        Export all structured knowledge to a YAML file.

        Args:
            file_path: Destination YAML file path.
        """
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # HTTP status codes
        http_rows = self.conn.execute(
            "SELECT code, category, description FROM http_status_codes ORDER BY code"
        ).fetchall()
        http_data = [dict(r) for r in http_rows]

        # Error patterns
        ep_rows = self.conn.execute(
            "SELECT pattern, category, description FROM error_patterns ORDER BY id"
        ).fetchall()
        ep_data = [dict(r) for r in ep_rows]

        # Framework practices
        fp_rows = self.conn.execute(
            "SELECT framework, version, practice_type, description"
            " FROM framework_practices ORDER BY id"
        ).fetchall()
        fp_data = [dict(r) for r in fp_rows]

        output: dict[str, list[dict[str, Any]]] = {
            "http_status_codes": http_data,
            "error_patterns": ep_data,
            "framework_practices": fp_data,
        }

        with open(file_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(output, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

        logger.info("Exported StructKB data to %s", file_path)
