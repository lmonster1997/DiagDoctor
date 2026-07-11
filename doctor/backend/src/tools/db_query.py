"""
DB query tool — read-only SQL execution with safety guard.

Provides a LangChain StructuredTool for ReAct agents to run read-only
SELECT queries against the demo-app database for data verification.

Security: three-layer defence:
1. App layer: ``sql_guard.assert_readonly()`` validates single SELECT via sqlparse
2. Connection layer: psycopg async → Docker exec fallback (cross-platform)
3. Audit: all executed SQL is logged (sanitised) via structlog

Connection strategy (resilient across Docker Desktop / WSL / Linux):
1. Try psycopg 3 async direct connection
2. Fallback to ``docker exec postgres psql`` subprocess (works even when
   Docker Desktop port forwarding is broken on Windows)
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys

from langchain_core.tools import StructuredTool

from src.config import settings
from src.observability.logger import get_logger
from src.observability.tracing import traced
from src.security.sql_guard import assert_readonly

logger = get_logger(__name__)

# ── Windows event loop fix (psycopg 3 requires SelectorEventLoop) ────
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass  # Already set by caller


def _get_db_url() -> str:
    """Resolve the database URL from settings."""
    if hasattr(settings, "demo_db_ro_url") and settings.demo_db_ro_url:
        return settings.demo_db_ro_url
    # Sensible default for docker-compose dev environment
    return "postgresql://postgres:postgres@localhost:5432/taskflow"


async def _query_via_psycopg(sql: str) -> dict:
    """Execute SQL via psycopg 3 async connection. Returns result dict or raises."""
    import psycopg

    db_url = _get_db_url()
    # Convert SQLAlchemy-style URL to psycopg-style if needed
    conn_str = db_url
    if conn_str.startswith("postgresql+asyncpg://"):
        conn_str = conn_str.replace("postgresql+asyncpg://", "postgresql://")
    elif conn_str.startswith("postgresql+"):
        conn_str = re.sub(r"^postgresql\+[^:]+://", "postgresql://", conn_str)

    conn = await psycopg.AsyncConnection.connect(conn_str, autocommit=True)
    try:
        cursor = await conn.execute(sql)
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []

        logger.info(
            "db_query_via_psycopg",
            sql_snippet=sql[:200],
            row_count=len(rows),
        )
        return {
            "columns": columns,
            "row_count": len(rows),
            "rows": [_serialize_row(zip(columns, row)) for row in rows],
            "status": "ok",
        }
    finally:
        await conn.close()


def _query_via_docker_exec(sql: str) -> dict:
    """Execute SQL via ``docker exec postgres psql`` subprocess.

    Fallback for Docker Desktop on Windows where port forwarding of
    PostgreSQL's wire protocol is unreliable.
    """
    container = "diagdoctor-postgres"
    db_name = "taskflow"
    user = "postgres"

    # Pipe SQL via stdin to avoid shell escaping issues
    cmd = [
        "docker", "exec", "-i", container,
        "psql", "-U", user, "-d", db_name,
        "-X",           # No .psqlrc
        "-q",           # Quiet
        "-t",           # Tuples only (no headers)
        "-A",           # Unaligned output
        "-F", "|",      # Field separator
    ]

    result = subprocess.run(
        cmd,
        input=sql.strip().rstrip(";") + ";\n",
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
        env={**__import__("os").environ, "PGPASSWORD": "postgres"},
    )

    if result.returncode != 0:
        raise RuntimeError(f"docker exec psql failed: {result.stderr.strip()[:500]}")

    # Parse psql tabular output
    output = result.stdout.strip()
    if not output:
        return {"columns": [], "row_count": 0, "rows": [], "status": "ok"}

    lines = output.split("\n")
    rows = [line.split("|") for line in lines]

    # Infer column names from SQL SELECT clause
    columns = _infer_columns(sql)

    logger.info(
        "db_query_via_docker_exec",
        sql_snippet=sql[:200],
        row_count=len(rows),
    )
    return {
        "columns": columns,
        "row_count": len(rows),
        "rows": [_serialize_docker_row(row, columns) for row in rows],
        "status": "ok",
    }


def _infer_columns(sql: str) -> list[str]:
    """Rudimentary column name inference from SELECT statement.

    For a robust approach, we'd parse the SQL. This heuristically
    extracts column aliases and simple column references.
    """
    # Try to extract from SELECT ... FROM
    match = re.search(r"SELECT\s+(.+?)\s+FROM", sql, re.IGNORECASE | re.DOTALL)
    if not match:
        return [f"col_{i}" for i in range(20)]  # fallback

    cols_str = match.group(1)
    columns: list[str] = []
    for part in cols_str.split(","):
        part = part.strip()
        # Check for "expr AS alias" or "expr alias"
        as_match = re.search(r"(?:AS\s+)?(\w+)\s*$", part, re.IGNORECASE)
        if as_match:
            columns.append(as_match.group(1))
        else:
            # Use the whole expression as column name
            col = part.split(".")[-1] if "." in part else part
            columns.append(col.strip("`\"' "))
    return columns


def _serialize_row(row_items) -> dict:
    """Serialize a psycopg result row to JSON-safe dict."""
    result = {}
    for col, val in row_items:
        result[str(col)] = _serialize_value(val)
    return result


def _serialize_docker_row(row: list[str], columns: list[str]) -> dict:
    """Serialize a docker exec psql result row to dict."""
    result = {}
    for i, col in enumerate(columns):
        if i < len(row):
            result[col] = row[i].strip()
        else:
            result[col] = None
    return result


def _serialize_value(value: object) -> object:
    """Serialize DB values to JSON-safe types."""
    from datetime import date, datetime, time, timedelta
    from decimal import Decimal
    from uuid import UUID

    if value is None:
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return value


@traced()
async def db_query(sql: str) -> str:
    """
    Execute a **read-only** SQL query against the demo-app database.

    Only SELECT statements are allowed. The query runs via a dedicated
    read-only database connection.

    Args:
        sql: A read-only SQL SELECT statement.

    Returns:
        JSON string with column names, row count, and data rows (max 100 rows).
    """
    # ── 1. SQL guard: validate read-only ─────────────────────────
    try:
        assert_readonly(sql)
    except Exception as exc:
        msg = f"SQL rejected by guard: {exc}"
        logger.warning("db_query_rejected", error=str(exc), sql_snippet=sql[:200])
        return json.dumps({"error": msg, "status": "rejected"}, ensure_ascii=False)

    # ── 2. Auto-append LIMIT 100 ──────────────────────────────────
    safe_sql = sql.strip().rstrip(";")
    if "limit" not in safe_sql.lower():
        safe_sql += " LIMIT 100"

    # ── 3. Execute (psycopg → docker exec fallback) ──────────────
    try:
        result = await _query_via_psycopg(safe_sql)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as psycopg_err:
        logger.debug(
            "db_query_psycopg_failed",
            error=str(psycopg_err),
            hint="Falling back to docker exec",
        )

    try:
        result = _query_via_docker_exec(safe_sql)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as docker_err:
        logger.error(
            "db_query_all_methods_failed",
            psycopg_error=str(psycopg_err) if "psycopg_err" in dir() else "",
            docker_error=str(docker_err),
        )
        return json.dumps(
            {
                "error": "无法连接到 demo-app 数据库。",
                "hint": "请确认 docker compose up -d postgres 已运行",
                "detail": str(docker_err)[:300],
                "status": "not_connected",
            },
            ensure_ascii=False,
        )


# ── LangChain StructuredTool wrapper ─────────────────────────────────

DB_QUERY_TOOL = StructuredTool.from_function(
    coroutine=db_query,
    name="db_query",
    description=(
        "Execute a read-only SQL SELECT query against the demo-app database. "
        "Use this ONLY to verify data state (check if rows exist, inspect values, "
        "validate constraints). The query must be a single SELECT statement — "
        "INSERT, UPDATE, DELETE, DDL are blocked. "
        "Example: sql='SELECT id, status, assignee_id FROM tasks WHERE id = 1'"
    ),
)
