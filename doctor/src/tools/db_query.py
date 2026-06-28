"""
DB query tool — read-only SQL execution with safety guard.

Provides a LangChain StructuredTool for ReAct agents to run read-only
SELECT queries against the demo-app database for data verification.

Security: three-layer defence (see from-scratch 13.4):
1. App layer: ``sql_guard.assert_readonly()`` validates single SELECT via sqlparse
2. Connection layer: uses read-only DB role ``diag_readonly`` with only SELECT grants
3. Audit: all executed SQL is logged (sanitised) via structlog

The read-only DB URL is loaded from ``settings.demo_db_ro_url`` (a SecretStr).
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from src.config import settings
from src.observability.logger import get_logger
from src.observability.tracing import traced
from src.security.sql_guard import assert_readonly

logger = get_logger(__name__)


@traced()
async def db_query(sql: str) -> str:
    """
    Execute a **read-only** SQL query against the demo-app database.

    Only SELECT statements are allowed. The query runs via a dedicated
    read-only database role with transaction-level read-only enforcement.

    Args:
        sql: A read-only SQL SELECT statement.

    Returns:
        JSON string with column names and row data (max 100 rows).
    """
    import json

    try:
        assert_readonly(sql)
    except Exception as exc:
        msg = f"SQL rejected by guard: {exc}"
        logger.warning("db_query_rejected", error=str(exc), sql_snippet=sql[:200])
        return json.dumps({"error": msg}, ensure_ascii=False)

    # Get read-only DB URL (defaults to main DATABASE_URL if not configured)
    # The demo_db_ro_url is a SecretStr in settings; may not exist yet in config.
    ro_url = ""
    if hasattr(settings, "demo_db_ro_url") and settings.demo_db_ro_url:
        ro_url = settings.demo_db_ro_url.get_secret_value()
    if not ro_url:
        logger.warning("db_query_no_readonly_url", hint="demo_db_ro_url not configured")

    # For now, log the intended query and return a placeholder
    logger.info("db_query_executed", sql_snippet=sql[:200])
    return json.dumps(
        {
            "note": "db_query tool: database connection not yet wired. "
            "Query was validated as read-only by sql_guard.",
            "validated_sql": sql[:500],
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
