"""Unit tests for src/tools/trace_query.py — span tree construction & analysis."""

from __future__ import annotations

from src.tools.trace_query import (
    SpanNode,
    build_cross_tier_tree,
    detect_n_plus_one,
    find_bottlenecks,
    find_critical_path,
    find_error_spans,
    flatten_tree,
    get_tree_summary,
)

# ── Helpers ────────────────────────────────────────────────────────


def _make_span(
    span_id: str,
    parent_span_id: str = "",
    name: str = "",
    service_name: str = "demo-backend",
    duration_ms: float = 10.0,
    status: str = "ok",
    db_statement: str = "",
    start: str = "2026-06-27T12:00:00Z",
    trace_id: str = "abc123",
) -> dict:
    """Helper to create a raw span dict."""
    return {
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "service_name": service_name,
        "duration_ms": duration_ms,
        "status": status,
        "db_statement": db_statement,
        "start": start,
        "trace_id": trace_id,
    }


# ── build_cross_tier_tree tests ────────────────────────────────────


class TestBuildCrossTierTree:
    """Tests for build_cross_tier_tree."""

    def test_empty_traces(self) -> None:
        """Should return empty list for no traces."""
        result = build_cross_tier_tree([])
        assert result == []

    def test_single_root(self) -> None:
        """Single span with no parent → one root node."""
        spans = [_make_span("span-1", name="GET /tasks")]
        roots = build_cross_tier_tree(spans)
        assert len(roots) == 1
        assert roots[0].span_id == "span-1"
        assert roots[0].name == "GET /tasks"

    def test_parent_child_tree(self) -> None:
        """Parent-child relationship should build a tree."""
        spans = [
            _make_span("span-1", name="fetch /api/tasks", service_name="demo-frontend"),
            _make_span("span-2", parent_span_id="span-1", name="GET /api/tasks"),
            _make_span("span-3", parent_span_id="span-2", name="SELECT tasks"),
        ]
        roots = build_cross_tier_tree(spans)
        assert len(roots) == 1
        root = roots[0]
        assert root.span_id == "span-1"
        assert len(root.children) == 1
        assert root.children[0].span_id == "span-2"
        assert len(root.children[0].children) == 1
        assert root.children[0].children[0].span_id == "span-3"

    def test_cross_tier_tree(self) -> None:
        """Frontend fetch span should be parent of backend server span."""
        spans = [
            _make_span(
                "fe-1", name="fetch /api/tasks", service_name="demo-frontend", duration_ms=500
            ),
            _make_span(
                "be-1",
                parent_span_id="fe-1",
                name="GET /api/tasks",
                service_name="demo-backend",
                duration_ms=450,
            ),
            _make_span(
                "db-1",
                parent_span_id="be-1",
                name="SELECT tasks",
                service_name="demo-backend",
                duration_ms=200,
                db_statement="SELECT * FROM tasks",
            ),
        ]
        roots = build_cross_tier_tree(spans)
        assert len(roots) == 1
        root = roots[0]
        assert root.is_frontend
        assert root.service_tier == "frontend"
        assert root.children[0].is_backend
        assert root.children[0].children[0].is_db_span

    def test_multiple_roots(self) -> None:
        """Spans without common parent → multiple roots."""
        spans = [
            _make_span("span-1", name="fetch A"),
            _make_span("span-2", name="fetch B"),
        ]
        roots = build_cross_tier_tree(spans)
        assert len(roots) == 2

    def test_service_tier_derivation(self) -> None:
        """Service tier should be derived from span name patterns."""
        spans = [
            _make_span("fe", name="click", service_name=""),
            _make_span("be", name="POST /api/data", service_name=""),
        ]
        roots = build_cross_tier_tree(spans)
        frontend_span = [r for r in roots if r.span_id == "fe"][0]
        backend_span = [r for r in roots if r.span_id == "be"][0]
        assert frontend_span.is_frontend
        assert backend_span.is_backend


# ── flatten_tree tests ──────────────────────────────────────────────


class TestFlattenTree:
    """Tests for flatten_tree."""

    def test_flattens_all_nodes(self) -> None:
        """Should return all nodes in DFS order."""
        spans = [
            _make_span("root", name="root"),
            _make_span("c1", parent_span_id="root", name="child1"),
            _make_span("c2", parent_span_id="root", name="child2"),
        ]
        roots = build_cross_tier_tree(spans)
        flat = flatten_tree(roots)
        assert len(flat) == 3
        ids = [n.span_id for n in flat]
        assert "root" in ids
        assert "c1" in ids
        assert "c2" in ids


# ── detect_n_plus_one tests ─────────────────────────────────────────


class TestDetectNPlusOne:
    """Tests for detect_n_plus_one."""

    def test_no_n_plus_one(self) -> None:
        """Normal tree with distinct queries → no N+1 detected."""
        spans = [
            _make_span("root", name="GET /tasks"),
            _make_span(
                "db1", parent_span_id="root", name="query", db_statement="SELECT * FROM tasks"
            ),
        ]
        roots = build_cross_tier_tree(spans)
        patterns = detect_n_plus_one(roots)
        assert len(patterns) == 0

    def test_detects_n_plus_one(self) -> None:
        """Repeated same SQL under one parent → N+1 detected."""
        spans = [
            _make_span("root", name="GET /tasks"),
            _make_span(
                "db1",
                parent_span_id="root",
                name="query",
                db_statement="SELECT comments WHERE task_id=$1",
                duration_ms=15,
            ),
            _make_span(
                "db2",
                parent_span_id="root",
                name="query",
                db_statement="SELECT comments WHERE task_id=$2",
                duration_ms=16,
            ),
            _make_span(
                "db3",
                parent_span_id="root",
                name="query",
                db_statement="SELECT comments WHERE task_id=$3",
                duration_ms=14,
            ),
            _make_span(
                "db4",
                parent_span_id="root",
                name="query",
                db_statement="SELECT comments WHERE task_id=$4",
                duration_ms=17,
            ),
        ]
        roots = build_cross_tier_tree(spans)
        patterns = detect_n_plus_one(roots)
        assert len(patterns) == 1
        assert patterns[0]["count"] == 4
        assert "comments" in patterns[0]["db_statement"].lower()
        assert patterns[0]["parent_span_name"] == "GET /tasks"

    def test_different_queries_not_n_plus_one(self) -> None:
        """Distinct SQL statements under same parent → NOT N+1."""
        spans = [
            _make_span("root", name="GET /tasks"),
            _make_span("db1", parent_span_id="root", db_statement="SELECT tasks"),
            _make_span("db2", parent_span_id="root", db_statement="SELECT users"),
        ]
        roots = build_cross_tier_tree(spans)
        patterns = detect_n_plus_one(roots)
        assert len(patterns) == 0


# ── find_bottlenecks tests ──────────────────────────────────────────


class TestFindBottlenecks:
    """Tests for find_bottlenecks."""

    def test_finds_slow_spans(self) -> None:
        """Spans above threshold should be returned."""
        spans = [
            _make_span("fast", name="fast", duration_ms=50),
            _make_span("slow", name="slow", duration_ms=500),
        ]
        roots = build_cross_tier_tree(spans)
        bottlenecks = find_bottlenecks(roots, threshold_ms=200)
        assert len(bottlenecks) == 1
        assert bottlenecks[0]["name"] == "slow"

    def test_sorted_by_duration(self) -> None:
        """Results should be sorted by duration descending."""
        spans = [
            _make_span("a", name="medium", duration_ms=300),
            _make_span("b", name="slowest", duration_ms=800),
            _make_span("c", name="fast", duration_ms=250),
        ]
        roots = build_cross_tier_tree(spans)
        bottlenecks = find_bottlenecks(roots, threshold_ms=200)
        assert bottlenecks[0]["name"] == "slowest"
        assert bottlenecks[0]["duration_ms"] == 800


# ── find_error_spans tests ──────────────────────────────────────────


class TestFindErrorSpans:
    """Tests for find_error_spans."""

    def test_finds_error_spans(self) -> None:
        """Should return only spans with status=error."""
        spans = [
            _make_span("ok", name="ok", status="ok"),
            _make_span("err", name="error span", status="error"),
            _make_span("unset", name="unset", status="unset"),
        ]
        roots = build_cross_tier_tree(spans)
        errors = find_error_spans(roots)
        assert len(errors) == 1
        assert errors[0]["name"] == "error span"

    def test_no_errors(self) -> None:
        """No error spans → empty list."""
        spans = [
            _make_span("ok1", name="ok1", status="ok"),
            _make_span("ok2", name="ok2", status="ok"),
        ]
        roots = build_cross_tier_tree(spans)
        errors = find_error_spans(roots)
        assert len(errors) == 0


# ── find_critical_path tests ────────────────────────────────────────


class TestFindCriticalPath:
    """Tests for find_critical_path."""

    def test_longest_path(self) -> None:
        """Should find the longest cumulative-duration path."""
        spans = [
            _make_span("root", name="root", duration_ms=10),
            _make_span("c1", parent_span_id="root", name="fast", duration_ms=20),
            _make_span("c2", parent_span_id="root", name="slow", duration_ms=500),
        ]
        roots = build_cross_tier_tree(spans)
        path = find_critical_path(roots)
        path_ids = [n.span_id for n in path]
        assert "root" in path_ids
        assert "c2" in path_ids  # slower branch


# ── get_tree_summary tests ──────────────────────────────────────────


class TestGetTreeSummary:
    """Tests for get_tree_summary."""

    def test_empty_tree(self) -> None:
        """Empty tree → minimal summary."""
        summary = get_tree_summary([])
        assert summary["node_count"] == 0

    def test_full_summary(self) -> None:
        """Tree with errors and N+1 → full summary."""
        spans = [
            _make_span("root", name="GET /tasks"),
            _make_span(
                "err", parent_span_id="root", name="error_span", status="error", duration_ms=500
            ),
            _make_span(
                "db1",
                parent_span_id="root",
                db_statement="SELECT * FROM comments WHERE id=1",
                duration_ms=10,
            ),
            _make_span(
                "db2",
                parent_span_id="root",
                db_statement="SELECT * FROM comments WHERE id=2",
                duration_ms=10,
            ),
            _make_span(
                "db3",
                parent_span_id="root",
                db_statement="SELECT * FROM comments WHERE id=3",
                duration_ms=10,
            ),
        ]
        roots = build_cross_tier_tree(spans)
        summary = get_tree_summary(roots)
        assert summary["node_count"] == 5
        assert summary["error_count"] == 1
        assert summary["n_plus_one_patterns"] == 1
        assert "top_bottlenecks" in summary
        assert "critical_path_length" in summary


# ── SpanNode tests ──────────────────────────────────────────────────


class TestSpanNode:
    """Tests for SpanNode properties."""

    def test_is_frontend(self) -> None:
        node = SpanNode(span_id="1", service_tier="frontend")
        assert node.is_frontend
        assert not node.is_backend

    def test_is_error(self) -> None:
        node = SpanNode(span_id="1", status="error")
        assert node.is_error

    def test_is_slow(self) -> None:
        node = SpanNode(span_id="1", duration_ms=500)
        assert node.is_slow(threshold_ms=200)
        assert not node.is_slow(threshold_ms=600)

    def test_is_db_span(self) -> None:
        db_node = SpanNode(span_id="1", db_statement="SELECT 1")
        named_node = SpanNode(span_id="2", name="db_query")
        assert db_node.is_db_span
        assert named_node.is_db_span

    def test_to_dict(self) -> None:
        node = SpanNode(
            span_id="sp1",
            parent_span_id="p1",
            name="test",
            service_name="demo-backend",
            service_tier="backend",
            duration_ms=100,
            status="ok",
        )
        d = node.to_dict()
        assert d["span_id"] == "sp1"
        assert d["parent_span_id"] == "p1"
        assert d["duration_ms"] == 100
        assert d["children_count"] == 0
