"""
DB query tool - read-only SQL execution with safety guard.

Provides a LangChain StructuredTool for ReAct agents to run read-only
SELECT queries against the demo-app database for data verification.

Security: two-layer defence:
1. App layer: ``sql_guard.assert_readonly()`` validates single SELECT via sqlparse
2. Audit: all executed SQL is logged (sanitised) via structlog

Connection: psycopg 3 **sync** API, offloaded to a worker thread via
``asyncio.to_thread``. The sync API is used deliberately -- psycopg's async
mode requires a ``SelectorEventLoop``, but the doctor process (uvicorn / IDE
debugger) runs on Windows' default ``ProactorEventLoop``, where
``AsyncConnection`` raises ``InterfaceError``. ``to_thread`` sidesteps the
event-loop constraint and works on any loop. There is **no fallback** to a
different database: a previous ``docker exec`` fallback silently read a seed
database that diverged from the app's real data, misleading the agent. On
connection failure ``db_query`` returns a clear ``not_connected`` error so the
agent knows data verification is unavailable rather than acting on wrong data.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.tools import StructuredTool

from src.config import settings
from src.observability.logger import get_logger
from src.observability.tracing import traced
from src.security.sql_guard import assert_readonly

logger = get_logger(__name__)


def _get_db_url() -> str:
    """Resolve the database URL from settings."""
    if hasattr(settings, "demo_db_ro_url") and settings.demo_db_ro_url:
        return settings.demo_db_ro_url
    # Default: the demo-app's own Postgres (host 127.0.0.1 to stay deterministic
    # and match the demo-app's .env, avoiding IPv6/localhost routing surprises).
    return "postgresql://postgres:DiagDoctor@127.0.0.1:5432/demo_taskflow"


def _psycopg_conn_str() -> str:
    """Convert the configured URL to a plain psycopg DSN (strip SQLAlchemy driver)."""
    conn_str = _get_db_url()
    if conn_str.startswith("postgresql+asyncpg://"):
        conn_str = conn_str.replace("postgresql+asyncpg://", "postgresql://")
    elif conn_str.startswith("postgresql+"):
        conn_str = re.sub(r"^postgresql\+[^:]+://", "postgresql://", conn_str)
    return conn_str


def _execute_sync(sql: str) -> dict[str, Any]:
    """Run the SELECT synchronously via psycopg (called from a worker thread).

    Sync ``psycopg.connect`` uses libpq blocking I/O, which has no event-loop
    affinity -- safe to run under any loop when offloaded with ``to_thread``.
    """
    import psycopg

    with psycopg.connect(_psycopg_conn_str(), autocommit=True) as conn:
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []

    return {
        "columns": columns,
        "row_count": len(rows),
        "rows": [_serialize_row(zip(columns, row, strict=False)) for row in rows],
        "status": "ok",
    }


async def _query_via_psycopg(sql: str) -> dict[str, Any]:
    """Execute SQL via psycopg, offloaded to a worker thread (sync API)."""
    result = await asyncio.to_thread(_execute_sync, sql)
    logger.info("db_query_via_psycopg", sql_snippet=sql[:200], row_count=result["row_count"])
    return result


def _serialize_row(row_items: Any) -> dict[str, object]:
    """Serialize a psycopg result row to JSON-safe dict."""
    result: dict[str, object] = {}
    for col, val in row_items:
        result[str(col)] = _serialize_value(val)
    return result


def _serialize_value(value: object) -> object:
    """Serialize DB values to JSON-safe types.

    SQL NULL is preserved as ``None`` (-> JSON ``null``), distinct from an
    empty string ``""`` -- conflation of the two previously misled the agent
    into thinking a NULL ``assignee_id`` was a safe-to-slice empty string.
    """
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
    read-only database connection to the **same** Postgres the demo-app uses.

    Args:
        sql: A read-only SQL SELECT statement.

    Returns:
        JSON string with column names, row count, and data rows (max 100 rows).
        On connection failure, returns a ``not_connected`` error so the agent
        knows data verification is unavailable (no silent fallback to another DB).
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

    # ── 3. Execute via psycopg (no fallback: fail loud, never read a different DB) ──
    try:
        result = await _query_via_psycopg(safe_sql)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.error(
            "db_query_failed",
            error=str(exc),
            sql_snippet=sql[:200],
            hint="psycopg could not reach the demo-app Postgres; no fallback (would read a different DB)",
        )
        return json.dumps(
            {
                "error": "无法连接到 demo-app 数据库。",
                "hint": "请确认 demo-app 的 Postgres 正在运行且 demo_db_ro_url 指向它（与 demo-app 同库）",
                "detail": str(exc)[:300],
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
        "validate constraints). The query must be a single SELECT statement - "
        "INSERT, UPDATE, DELETE, DDL are blocked. "
        "Example: sql='SELECT id, status, assignee_id FROM tasks WHERE id = 1'"
    ),
)
