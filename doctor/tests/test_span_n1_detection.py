"""Quick test for span-level N+1 detection in signal_extractor."""

from src.ingest.signal_extractor import _detect_span_n_plus_one


def test_detect_n1_basic():
    """5 repeated SELECT under same parent should be detected."""
    traces = [
        {"span_id": "parent1", "name": "GET /api/tasks", "status": "ok",
         "duration_ms": 10, "service_name": "demo-backend"},
        {"span_id": "child1", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM comments WHERE task_id = 1",
         "duration_ms": 50, "service_name": "demo-backend", "trace_id": "trace001"},
        {"span_id": "child2", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM comments WHERE task_id = 2",
         "duration_ms": 52, "service_name": "demo-backend", "trace_id": "trace001"},
        {"span_id": "child3", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM comments WHERE task_id = 3",
         "duration_ms": 48, "service_name": "demo-backend", "trace_id": "trace001"},
        {"span_id": "child4", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM comments WHERE task_id = 4",
         "duration_ms": 51, "service_name": "demo-backend", "trace_id": "trace001"},
        {"span_id": "child5", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM comments WHERE task_id = 5",
         "duration_ms": 49, "service_name": "demo-backend", "trace_id": "trace001"},
    ]

    n1_signals = _detect_span_n_plus_one(traces)
    assert len(n1_signals) == 1, f"Expected 1 pattern, got {len(n1_signals)}"
    sig = n1_signals[0]
    assert sig.metadata["count"] == 5
    assert "parent=GET /api/tasks" in sig.summary
    assert sig.signal_type == "repeated_query"
    assert sig.source == "trace"
    assert sig.metadata["total_duration_ms"] == 250.0
    print(f"  ✓ {sig.summary}")


def test_no_n1_below_threshold():
    """Only 2 repeated queries should NOT trigger N+1."""
    traces = [
        {"span_id": "parent1", "name": "GET /api/tasks", "status": "ok",
         "duration_ms": 10, "service_name": "demo-backend"},
        {"span_id": "child1", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM comments WHERE task_id = 1",
         "duration_ms": 50, "service_name": "demo-backend", "trace_id": "trace001"},
        {"span_id": "child2", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM comments WHERE task_id = 2",
         "duration_ms": 52, "service_name": "demo-backend", "trace_id": "trace001"},
    ]

    n1_signals = _detect_span_n_plus_one(traces)
    assert len(n1_signals) == 0, f"Expected 0 patterns, got {len(n1_signals)}"
    print("  ✓ No N+1 detected for count=2 (correct)")


def test_no_n1_empty_traces():
    """Empty traces should return empty."""
    n1_signals = _detect_span_n_plus_one([])
    assert len(n1_signals) == 0
    print("  ✓ Empty traces return empty")


def test_no_n1_no_db_spans():
    """Traces without db_statement should not trigger."""
    traces = [
        {"span_id": "parent1", "name": "GET /api/tasks", "status": "ok",
         "duration_ms": 10, "service_name": "demo-backend"},
        {"span_id": "child1", "parentSpanId": "parent1", "name": "process",
         "duration_ms": 10, "service_name": "demo-backend"},
        {"span_id": "child2", "parentSpanId": "parent1", "name": "process",
         "duration_ms": 10, "service_name": "demo-backend"},
        {"span_id": "child3", "parentSpanId": "parent1", "name": "process",
         "duration_ms": 10, "service_name": "demo-backend"},
    ]

    n1_signals = _detect_span_n_plus_one(traces)
    assert len(n1_signals) == 0
    print("  ✓ No N+1 for non-DB spans (correct)")


def test_different_parents_not_grouped():
    """Same SQL under different parents should be separate patterns."""
    traces = [
        {"span_id": "parent1", "name": "GET /api/tasks", "status": "ok",
         "duration_ms": 10, "service_name": "demo-backend"},
        {"span_id": "parent2", "name": "GET /api/projects", "status": "ok",
         "duration_ms": 10, "service_name": "demo-backend"},
        # 3 children under parent1
        {"span_id": "c1", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM items", "duration_ms": 10,
         "service_name": "demo-backend"},
        {"span_id": "c2", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM items", "duration_ms": 10,
         "service_name": "demo-backend"},
        {"span_id": "c3", "parentSpanId": "parent1", "name": "SELECT",
         "db_statement": "SELECT * FROM items", "duration_ms": 10,
         "service_name": "demo-backend"},
        # 3 children under parent2 (same SQL)
        {"span_id": "c4", "parentSpanId": "parent2", "name": "SELECT",
         "db_statement": "SELECT * FROM items", "duration_ms": 10,
         "service_name": "demo-backend"},
        {"span_id": "c5", "parentSpanId": "parent2", "name": "SELECT",
         "db_statement": "SELECT * FROM items", "duration_ms": 10,
         "service_name": "demo-backend"},
        {"span_id": "c6", "parentSpanId": "parent2", "name": "SELECT",
         "db_statement": "SELECT * FROM items", "duration_ms": 10,
         "service_name": "demo-backend"},
    ]

    n1_signals = _detect_span_n_plus_one(traces)
    assert len(n1_signals) == 2, f"Expected 2 patterns, got {len(n1_signals)}"
    parents = {sig.metadata["parent_span_id"] for sig in n1_signals}
    assert parents == {"parent1", "parent2"}
    print("  ✓ Detected 2 separate N+1 patterns under different parents")


def test_normalised_sql_grouping():
    """Different parameter values should be normalized to same group."""
    traces = [
        {"span_id": "p1", "name": "GET /api/tasks", "status": "ok",
         "duration_ms": 10, "service_name": "demo-backend"},
        {"span_id": "c1", "parentSpanId": "p1", "name": "SELECT",
         "db_statement": "SELECT * FROM t WHERE id = 1 AND name = 'alice'",
         "duration_ms": 10, "service_name": "demo-backend"},
        {"span_id": "c2", "parentSpanId": "p1", "name": "SELECT",
         "db_statement": "SELECT * FROM t WHERE id = 999 AND name = 'bob'",
         "duration_ms": 10, "service_name": "demo-backend"},
        {"span_id": "c3", "parentSpanId": "p1", "name": "SELECT",
         "db_statement": "SELECT * FROM t WHERE id = 42 AND name = 'charlie'",
         "duration_ms": 10, "service_name": "demo-backend"},
    ]

    n1_signals = _detect_span_n_plus_one(traces)
    assert len(n1_signals) == 1, f"Expected 1 pattern, got {len(n1_signals)}"
    assert n1_signals[0].metadata["count"] == 3
    print("  ✓ Normalized SQL groups different parameter values together")


if __name__ == "__main__":
    print("Testing span-level N+1 detection...")
    test_detect_n1_basic()
    test_no_n1_below_threshold()
    test_no_n1_empty_traces()
    test_no_n1_no_db_spans()
    test_different_parents_not_grouped()
    test_normalised_sql_grouping()
    print("\n✓ All tests passed!")
