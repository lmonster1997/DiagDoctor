"""Unit tests for sql_guard — SQL read-only validation."""

from __future__ import annotations

import pytest

from src.security.sql_guard import UnsafeSQLError, assert_readonly


class TestAssertReadonly:
    """Valid SELECT statements must pass without exception."""

    def test_simple_select(self) -> None:
        assert_readonly("SELECT * FROM tasks")

    def test_select_with_join(self) -> None:
        assert_readonly("SELECT t.id, c.body FROM tasks t JOIN comments c ON c.task_id = t.id")

    def test_select_with_subquery(self) -> None:
        assert_readonly(
            "SELECT * FROM tasks WHERE project_id IN (SELECT id FROM projects WHERE name = 'demo')"
        )

    def test_select_with_cte(self) -> None:
        assert_readonly(
            "WITH recent AS (SELECT * FROM tasks ORDER BY created_at DESC LIMIT 10) "
            "SELECT * FROM recent"
        )

    def test_select_with_where_and_order(self) -> None:
        assert_readonly(
            "SELECT id, title, status FROM tasks "
            "WHERE assignee_id = $1 ORDER BY priority DESC LIMIT 50"
        )

    def test_select_count(self) -> None:
        assert_readonly("SELECT COUNT(*) FROM tasks")

    def test_empty_sql_raises(self) -> None:
        with pytest.raises(UnsafeSQLError, match="empty"):
            assert_readonly("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(UnsafeSQLError, match="empty"):
            assert_readonly("   \n\t  ")


class TestDangerousStatements:
    """INSERT / UPDATE / DELETE / DDL must raise UnsafeSQLError."""

    def test_drop_table(self) -> None:
        with pytest.raises(UnsafeSQLError, match="DROP"):
            assert_readonly("DROP TABLE tasks")

    def test_truncate(self) -> None:
        with pytest.raises(UnsafeSQLError, match="TRUNCATE"):
            assert_readonly("TRUNCATE TABLE tasks")

    def test_alter_table(self) -> None:
        with pytest.raises(UnsafeSQLError, match="ALTER"):
            assert_readonly("ALTER TABLE tasks ADD COLUMN foo TEXT")

    def test_create_table(self) -> None:
        with pytest.raises(UnsafeSQLError, match="CREATE"):
            assert_readonly("CREATE TABLE foo (id INT)")

    def test_insert(self) -> None:
        with pytest.raises(UnsafeSQLError, match="INSERT"):
            assert_readonly("INSERT INTO tasks (title) VALUES ('x')")

    def test_update(self) -> None:
        with pytest.raises(UnsafeSQLError, match="UPDATE"):
            assert_readonly("UPDATE tasks SET title = 'x' WHERE id = 1")

    def test_delete(self) -> None:
        with pytest.raises(UnsafeSQLError, match="DELETE"):
            assert_readonly("DELETE FROM tasks WHERE id = 1")

    def test_grant(self) -> None:
        with pytest.raises(UnsafeSQLError, match="GRANT"):
            assert_readonly("GRANT SELECT ON tasks TO alice")

    def test_exec(self) -> None:
        with pytest.raises(UnsafeSQLError, match="EXEC"):
            assert_readonly("EXEC sp_configure 'show advanced options', 1")


class TestMultiStatement:
    """Multi-statement injection must be blocked."""

    def test_select_then_drop(self) -> None:
        with pytest.raises(UnsafeSQLError, match="Multiple"):
            assert_readonly("SELECT 1; DROP TABLE tasks")

    def test_select_then_update(self) -> None:
        with pytest.raises(UnsafeSQLError, match="Multiple"):
            assert_readonly("SELECT * FROM tasks; UPDATE tasks SET title = 'hacked'")

    def test_drop_then_select(self) -> None:
        with pytest.raises(UnsafeSQLError, match="Multiple"):
            assert_readonly("DROP TABLE tasks; SELECT 1")

    def test_three_statements(self) -> None:
        with pytest.raises(UnsafeSQLError, match="Multiple"):
            assert_readonly("SELECT 1; SELECT 2; SELECT 3")


class TestEdgeCases:
    """Boundary / edge-case inputs."""

    def test_select_with_leading_comment(self) -> None:
        assert_readonly("-- get all tasks\nSELECT * FROM tasks")

    def test_select_with_block_comment(self) -> None:
        assert_readonly("/* find tasks */ SELECT * FROM tasks WHERE status = 'todo'")

    def test_not_a_sql_statement(self) -> None:
        with pytest.raises(UnsafeSQLError):
            assert_readonly("hello world")

    def test_only_comment_raises(self) -> None:
        with pytest.raises(UnsafeSQLError):
            assert_readonly("-- just a comment")
