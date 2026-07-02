"""Smoke test for anomaly detection in observability_unified.py."""
from datetime import UTC, datetime, timedelta

from src.tools.observability_unified import _detect_anomalies, _normalize_error_message


def test_normalize():
    """Test error message normalization."""
    msg = "No row was found for one() at line 42 in /api/tasks/999"
    norm = _normalize_error_message(msg)
    print(f"Normalized: {norm}")
    assert "line <n>" in norm
    assert "/api/tasks" in norm


def test_error_burst():
    """Simulate BE-020 style error burst."""
    now = datetime.now(UTC)
    logs = []
    # 15 errors in a 2-minute window (burst)
    for i in range(15):
        logs.append({
            "timestamp": (now - timedelta(seconds=i * 10)).isoformat(),
            "severity": "ERROR",
            "message": f"No row was found for one() at /api/tasks/999",
        })
    # 5 sparse INFO logs far away
    for i in range(5):
        logs.append({
            "timestamp": (now - timedelta(seconds=i * 60 + 3600)).isoformat(),
            "severity": "INFO",
            "message": "Request processed",
        })

    traces = []
    start = now - timedelta(hours=2)
    end = now + timedelta(minutes=1)

    anomalies = _detect_anomalies(logs, traces, start, end)
    types = {a["type"] for a in anomalies}
    print(f"Test error_burst — anomalies: {len(anomalies)}, types: {types}")
    assert "error_burst" in types, f"Expected error_burst, got {types}"
    assert "error_cluster" in types, f"Expected error_cluster, got {types}"


def test_cascading_failure():
    """Test cascading failure detection."""
    now = datetime.now(UTC)
    logs: list[dict] = []
    traces = []
    tid1 = "a" * 32
    for i in range(4):
        traces.append({
            "span_id": f"span_{i}",
            "trace_id": tid1,
            "name": f"operation_{i}",
            "parent_span_id": "span_0" if i > 0 else "",
            "status": "error",
            "duration_ms": 100 + i * 50,
            "attributes": {"error.message": f"SQL error {i}"},
        })

    start = now - timedelta(hours=2)
    end = now + timedelta(minutes=1)

    anomalies = _detect_anomalies(logs, traces, start, end)
    types = {a["type"] for a in anomalies}
    print(f"Test cascading_failure — anomalies: {len(anomalies)}, types: {types}")
    assert "cascading_failure" in types, f"Expected cascading_failure, got {types}"


def test_latency_spike():
    """Test latency spike detection."""
    now = datetime.now(UTC)
    logs: list[dict] = []
    traces = []
    tid = "b" * 32
    # 5 normal spans (~100ms)
    for i in range(5):
        traces.append({
            "span_id": f"span_ok_{i}",
            "trace_id": tid,
            "name": "db_query",
            "duration_ms": 100,
            "status": "ok",
        })
    # 1 extreme outlier (3000ms = 30x slower)
    traces.append({
        "span_id": "span_slow",
        "trace_id": tid,
        "name": "db_query",
        "duration_ms": 3000,
        "status": "ok",
    })

    start = now - timedelta(hours=2)
    end = now + timedelta(minutes=1)

    anomalies = _detect_anomalies(logs, traces, start, end)
    types = {a["type"] for a in anomalies}
    print(f"Test latency_spike — anomalies: {len(anomalies)}, types: {types}")
    assert "latency_spike" in types, f"Expected latency_spike, got {types}"


def test_timeout_chain():
    """Test timeout chain detection."""
    now = datetime.now(UTC)
    logs: list[dict] = []
    traces = []
    tid = "c" * 32
    # Root span 100ms
    traces.append({
        "span_id": "root",
        "trace_id": tid,
        "name": "GET /api/tasks",
        "parent_span_id": "",
        "duration_ms": 100,
        "status": "ok",
    })
    # Child span 900ms (90% of total)
    traces.append({
        "span_id": "child",
        "trace_id": tid,
        "name": "SELECT tasks",
        "parent_span_id": "root",
        "duration_ms": 900,
        "status": "ok",
    })

    start = now - timedelta(hours=2)
    end = now + timedelta(minutes=1)

    anomalies = _detect_anomalies(logs, traces, start, end)
    types = {a["type"] for a in anomalies}
    print(f"Test timeout_chain — anomalies: {len(anomalies)}, types: {types}")
    assert "timeout_chain" in types, f"Expected timeout_chain, got {types}"


if __name__ == "__main__":
    test_normalize()
    test_error_burst()
    test_cascading_failure()
    test_latency_spike()
    test_timeout_chain()
    print("\n✅ All anomaly detection tests passed!")
