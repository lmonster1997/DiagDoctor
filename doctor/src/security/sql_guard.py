"""SQL read-only guard — validates that SQL statements are safe SELECT-only.

Used by the Doctor agent's ``db_query`` tool (D27) to enforce a
defence-in-depth read-only policy before any SQL reaches the
demo-app PostgreSQL database.

Security model (three layers, see from-scratch §13.4):
1. **Application layer** — this module: parse + validate before execution.
2. **Connection layer** — separate read-only PG role (``diag_readonly``)
   with ``SET TRANSACTION READ ONLY`` + ``statement_timeout``.
3. **Audit** — all executed SQL is logged (sanitised) via structlog.
"""

from __future__ import annotations

import re

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DDL, DML, Keyword

# ── Dangerous keyword set (case-insensitive match) ──────────────────
# Any SQL containing these keywords at the top level is rejected.
_DANGEROUS_KEYWORDS: frozenset[str] = frozenset(
    {
        "DROP",
        "TRUNCATE",
        "ALTER",
        "CREATE",
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "REPLACE",
        "GRANT",
        "REVOKE",
        "EXEC",
        "EXECUTE",
        "CALL",
        "COMMIT",
        "ROLLBACK",
        "BEGIN",
        "START",
        "LOCK",
        "UNLOCK",
    }
)

# Regex that matches any dangerous keyword as a whole word.
# Used as a fallback when sqlparse does not classify the keyword
# into a token type we can detect (e.g. GRANT, EXEC in some dialects).
_DANGEROUS_RE = re.compile(
    r"\b(?:" + "|".join(_DANGEROUS_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Regex to extract the first meaningful keyword from raw SQL
# (strips leading comments / whitespace).  Used because sqlparse
# sometimes misclassifies CTE WITH / DCL GRANT as non-Keyword tokens.
_FIRST_KW_RE = re.compile(
    r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)*(\w+)",
    re.IGNORECASE | re.DOTALL,
)


class UnsafeSQLError(Exception):
    """Raised when a SQL statement is not a single read-only SELECT."""

    def __init__(self, reason: str, sql: str = "") -> None:
        self.reason = reason
        self.sql = sql[:200]
        super().__init__(f"Unsafe SQL: {reason}")


def assert_readonly(sql: str) -> None:
    """Validate that *sql* is a **single, read-only SELECT** statement.

    Uses ``sqlparse`` to parse and check:

    1. **Exactly one statement** — multi-statement (``SELECT 1; DROP``)
       is rejected.
    2. **Statement type is SELECT** — INSERT / UPDATE / DELETE / DDL / DCL
       are rejected.
    3. **No dangerous keywords** — DROP, TRUNCATE, ALTER, EXEC, etc.
       are rejected.

    Args:
        sql: The raw SQL string to validate.

    Raises:
        UnsafeSQLError: If *sql* fails any of the above checks.

    Examples:
        >>> assert_readonly("SELECT * FROM tasks")
        >>> assert_readonly("SELECT t.id, c.body FROM tasks t JOIN comments c ...")
        >>> assert_readonly("DROP TABLE tasks")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        UnsafeSQLError: ...
        >>> assert_readonly("SELECT 1; DROP TABLE tasks")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        UnsafeSQLError: ...
    """
    stripped = sql.strip()
    if not stripped:
        raise UnsafeSQLError("SQL string is empty")

    # ── Parse ────────────────────────────────────────────────────
    # sqlparse.parse() returns tuple[Statement, ...]; use list() for mutable.
    statements = list(sqlparse.parse(stripped))
    # sqlparse returns trailing empty statements — filter them out.
    real_statements = [s for s in statements if s.tokens and str(s).strip()]

    # 1. Exactly one statement
    if len(real_statements) == 0:
        raise UnsafeSQLError("No valid SQL statement found")
    if len(real_statements) > 1:
        raise UnsafeSQLError(
            f"Multiple statements detected ({len(real_statements)}). "
            "Only a single SELECT is allowed.",
            sql=stripped,
        )

    # 1. Raw-string dangerous keyword check (catches GRANT / EXEC
    #    etc. that sqlparse may not classify as Keyword tokens).
    _check_dangerous_raw(stripped)

    stmt = real_statements[0]

    # 2. Token-level dangerous keyword check (catches most DML/DDL).
    _check_dangerous_tokens(stmt, stripped)

    # 3. Must be SELECT or a CTE (WITH ... SELECT ...).
    #    sqlparse returns 'UNKNOWN' for DDL / DCL statements.
    stmt_type: str = stmt.get_type()  # type: ignore[no-untyped-call]
    if stmt_type not in ("SELECT", "UNKNOWN"):
        raise UnsafeSQLError(
            f"Statement type is '{stmt_type}', not SELECT. "
            "Only read-only SELECT statements are allowed.",
            sql=stripped,
        )

    # 4. Extra safety: the first meaningful keyword must be SELECT or WITH.
    #    Uses raw regex because sqlparse token classification is unreliable
    #    for CTE WITH and some DCL keywords.
    first_match = _FIRST_KW_RE.match(stripped)
    first_kw = first_match.group(1) if first_match else None
    if first_kw is None:
        raise UnsafeSQLError(
            "Statement must start with SELECT (or WITH for CTEs), got nothing",
            sql=stripped,
        )
    if first_kw.upper() not in ("SELECT", "WITH"):
        raise UnsafeSQLError(
            f"Statement must start with SELECT (or WITH for CTEs), got '{first_kw}'",
            sql=stripped,
        )


# ── Internal helpers ────────────────────────────────────────────────


def _check_dangerous_tokens(stmt: Statement, original_sql: str) -> None:
    """Recursively scan *stmt* tokens for dangerous keywords.

    Also catches multi-statement injection within a single parsed
    statement (e.g. sub-select containing dangerous tokens).
    """
    for token in stmt.flatten():  # type: ignore[no-untyped-call]
        if token.ttype in (Keyword, DML, DDL):
            val_upper = token.value.upper()
            if val_upper in _DANGEROUS_KEYWORDS:
                raise UnsafeSQLError(
                    f"Dangerous keyword '{token.value}' found in SQL. "
                    "Only read-only SELECT is allowed.",
                    sql=original_sql,
                )


def _check_dangerous_raw(sql: str) -> None:
    """Regex-based dangerous keyword check (fallback for sqlparse blind spots).

    sqlparse may not classify GRANT / EXEC / CALL as Keyword tokens in
    some SQL dialects.  This raw-string scan catches them.
    """
    match = _DANGEROUS_RE.search(sql)
    if match:
        kw = match.group(0)
        raise UnsafeSQLError(
            f"Dangerous keyword '{kw}' found in SQL. Only read-only SELECT is allowed.",
            sql=sql,
        )
