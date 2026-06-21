"""Unit tests for StructKnowledgeBase."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.knowledge.struct_kb import StructKnowledgeBase


@pytest.fixture
def skb() -> StructKnowledgeBase:
    """Create a StructKnowledgeBase backed by a temp file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_struct.db"
        skb = StructKnowledgeBase(db_path=str(db_path))
        yield skb
        skb.close()


class TestHttpStatusCodes:
    """Tests for HTTP status code CRUD."""

    def test_add_and_query(self, skb: StructKnowledgeBase) -> None:
        """Should add and query an HTTP status code."""
        skb.add_http_status(404, "client_error", "Not Found")

        result = skb.query_http_status(404)
        assert result is not None
        assert result["code"] == 404
        assert result["category"] == "client_error"
        assert result["description"] == "Not Found"

    def test_add_replaces_existing(self, skb: StructKnowledgeBase) -> None:
        """Should replace an existing HTTP status code."""
        skb.add_http_status(500, "server_error", "Old")
        skb.add_http_status(500, "server_error", "New description")

        result = skb.query_http_status(500)
        assert result is not None
        assert result["description"] == "New description"

    def test_query_nonexistent(self, skb: StructKnowledgeBase) -> None:
        """Should return None for non-existent code."""
        result = skb.query_http_status(999)
        assert result is None

    def test_query_by_category(self, skb: StructKnowledgeBase) -> None:
        """Should filter by category."""
        skb.add_http_status(400, "client_error", "Bad Request")
        skb.add_http_status(404, "client_error", "Not Found")
        skb.add_http_status(500, "server_error", "Internal Server Error")

        results = skb.query_http_by_category("client_error")
        assert len(results) == 2
        codes = {r["code"] for r in results}
        assert codes == {400, 404}


class TestErrorPatterns:
    """Tests for error pattern CRUD and matching."""

    def test_add_and_query(self, skb: StructKnowledgeBase) -> None:
        """Should add and query error patterns."""
        skb.add_error_pattern("TypeError", "frontend_crash", "JS type error")

        results = skb.query_error_patterns()
        assert len(results) == 1
        assert results[0]["pattern"] == "TypeError"
        assert results[0]["category"] == "frontend_crash"

    def test_query_by_category(self, skb: StructKnowledgeBase) -> None:
        """Should filter patterns by category."""
        skb.add_error_pattern("TypeError", "frontend_crash", "JS")
        skb.add_error_pattern("500", "backend_error", "Server")

        results = skb.query_error_patterns(category="backend_error")
        assert len(results) == 1
        assert results[0]["pattern"] == "500"

    def test_match_error_pattern_found(self, skb: StructKnowledgeBase) -> None:
        """Should match an error message against stored patterns."""
        skb.add_error_pattern(r"TypeError.*Cannot read", "frontend_crash", "null access")
        skb.add_error_pattern(r"IntegrityError", "backend_error", "duplicate")

        result = skb.match_error_pattern("TypeError: Cannot read property 'name' of null")
        assert result is not None
        assert result["category"] == "frontend_crash"

    def test_match_error_pattern_not_found(self, skb: StructKnowledgeBase) -> None:
        """Should return None when no pattern matches."""
        skb.add_error_pattern("SpecificError", "backend_error", "specific")

        result = skb.match_error_pattern("Something completely unrelated happened")
        assert result is None

    def test_match_error_pattern_case_insensitive(self, skb: StructKnowledgeBase) -> None:
        """Should match case-insensitively."""
        skb.add_error_pattern("typeerror", "frontend_crash", "type error")

        result = skb.match_error_pattern("TypeError: something")
        assert result is not None
        assert result["category"] == "frontend_crash"

    def test_match_error_pattern_regex_special_chars(self, skb: StructKnowledgeBase) -> None:
        """Should handle regex special characters correctly."""
        skb.add_error_pattern(r"500 Internal Server Error", "backend_error", "500")

        result = skb.match_error_pattern("Got a 500 Internal Server Error at line 42")
        assert result is not None

    def test_match_invalid_regex_skipped(self, skb: StructKnowledgeBase) -> None:
        """Should skip invalid regex patterns without crashing."""
        # Insert an invalid regex directly via sqlite (bypassing python validation)
        skb.conn.execute(
            "INSERT INTO error_patterns (pattern, category, description) VALUES (?, ?, ?)",
            (r"[invalid(unclosed", "test", "invalid"),
        )
        skb.conn.commit()

        skb.add_error_pattern("valid", "test", "valid")
        result = skb.match_error_pattern("this is valid")
        assert result is not None
        assert result["pattern"] == "valid"


class TestFrameworkPractices:
    """Tests for framework practice CRUD."""

    def test_add_and_query(self, skb: StructKnowledgeBase) -> None:
        """Should add and query framework practices."""
        skb.add_framework_practice(
            "FastAPI", "performance", "Use async def", version="0.115"
        )

        results = skb.query_framework_practices()
        assert len(results) == 1
        assert results[0]["framework"] == "FastAPI"
        assert results[0]["practice_type"] == "performance"

    def test_query_by_framework(self, skb: StructKnowledgeBase) -> None:
        """Should filter by framework."""
        skb.add_framework_practice("FastAPI", "async", "Use async")
        skb.add_framework_practice("React", "error", "Use ErrorBoundary")

        results = skb.query_framework_practices(framework="React")
        assert len(results) == 1
        assert results[0]["practice_type"] == "error"

    def test_query_by_practice_type(self, skb: StructKnowledgeBase) -> None:
        """Should filter by practice_type."""
        skb.add_framework_practice("FastAPI", "async", "A")
        skb.add_framework_practice("FastAPI", "performance", "B")
        skb.add_framework_practice("React", "performance", "C")

        results = skb.query_framework_practices(practice_type="performance")
        assert len(results) == 2

    def test_query_by_both(self, skb: StructKnowledgeBase) -> None:
        """Should filter by both framework and practice_type."""
        skb.add_framework_practice("FastAPI", "async", "A")
        skb.add_framework_practice("FastAPI", "performance", "B")

        results = skb.query_framework_practices(
            framework="FastAPI", practice_type="async"
        )
        assert len(results) == 1
        assert results[0]["description"] == "A"


class TestBulkLoad:
    """Tests for bulk_load_from_yaml."""

    def test_bulk_load_from_yaml(self, skb: StructKnowledgeBase) -> None:
        """Should load all three types from a YAML file."""
        import yaml

        data = {
            "http_status_codes": [
                {"code": 200, "category": "success", "description": "OK"},
                {"code": 404, "category": "client_error", "description": "Not Found"},
            ],
            "error_patterns": [
                {"pattern": "TypeError", "category": "frontend_crash", "description": "JS error"},
            ],
            "framework_practices": [
                {
                    "framework": "FastAPI",
                    "version": "0.115",
                    "practice_type": "async",
                    "description": "Use async def",
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "test_knowledge.yaml"
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f)

            counts = skb.bulk_load_from_yaml(yaml_path)

            assert counts["http"] == 2
            assert counts["patterns"] == 1
            assert counts["practices"] == 1

            # Verify data was actually loaded
            assert skb.query_http_status(200) is not None
            assert len(skb.query_error_patterns()) == 1

    def test_bulk_load_missing_file(self, skb: StructKnowledgeBase) -> None:
        """Should return zero counts for missing file."""
        counts = skb.bulk_load_from_yaml("/nonexistent/path.yaml")
        assert counts == {"http": 0, "patterns": 0, "practices": 0}

    def test_bulk_load_empty_yaml(self, skb: StructKnowledgeBase) -> None:
        """Should handle empty YAML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "empty.yaml"
            yaml_path.write_text("{}", encoding="utf-8")

            counts = skb.bulk_load_from_yaml(yaml_path)
            assert counts == {"http": 0, "patterns": 0, "practices": 0}


class TestExportToYaml:
    """Tests for export_to_yaml."""

    def test_export_roundtrip(self, skb: StructKnowledgeBase) -> None:
        """Should produce valid YAML that can be re-imported."""
        skb.add_http_status(200, "success", "OK")
        skb.add_error_pattern("Error", "backend_error", "desc")
        skb.add_framework_practice("FastAPI", "async", "async def")

        with tempfile.TemporaryDirectory() as tmpdir:
            export_path = Path(tmpdir) / "exported.yaml"
            skb.export_to_yaml(export_path)

            assert export_path.exists()

            # Load back
            skb2 = StructKnowledgeBase(db_path=str(Path(tmpdir) / "imported.db"))
            counts = skb2.bulk_load_from_yaml(export_path)
            assert counts["http"] == 1
            assert counts["patterns"] == 1
            assert counts["practices"] == 1
            skb2.close()
