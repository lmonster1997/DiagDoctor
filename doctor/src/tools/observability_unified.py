"""
统一可观测性查询入口 (V3)。

将 ``query_loki_logs`` + ``search_tempo_traces`` + ``query_tempo_trace`` +
``analyze_trace`` 合并为一个统一入口 ``search_observability``。

设计原则:
- source="auto" 时: 先查 Loki → 提取 trace_id → 自动调 Tempo
- analysis="full" 时: 自动做 N+1 检测、瓶颈分析、错误 span 提取
- 时间范围安全校验: 跨度不超过 4 小时
- 返回统一的 JSON 结构: {logs: [...], traces: [...], analysis: {...}}

Usage:
    from src.tools.observability_unified import search_observability

    result = await search_observability(
        source="auto",
        query='{service_name="demo-backend"} |= "error"',
        start="2026-06-28T10:00:00Z",
        end="2026-06-28T14:00:00Z",
        analysis="full",
    )
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from src.observability.logger import get_logger
from src.observability.tracing import traced
from src.tools.observability_tools import (
    query_loki_logs,
    query_tempo_trace,
    search_tempo_traces,
)
from src.tools.trace_query import (
    SpanNode,
    build_cross_tier_tree,
    detect_n_plus_one,
    find_bottlenecks,
    find_error_spans,
    get_tree_summary,
)

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

# Maximum time range for search_observability (stricter than Loki's 24h)
MAX_QUERY_RANGE_HOURS: float = 4.0

# Maximum log entries to fetch in auto mode
AUTO_MODE_LOG_LIMIT: int = 500

# Maximum trace IDs to follow up in auto mode
AUTO_MODE_MAX_TRACE_IDS: int = 5

# Trace ID extraction regex (32-char hex string)
_TRACE_ID_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")


# ── Helper: Time range parsing & validation ─────────────────────────


def _parse_time(time_str: str | None) -> datetime | None:
    """Parse an ISO-format time string, or return None if empty/None."""
    if not time_str:
        return None
    try:
        # Handle 'Z' suffix
        normalized = time_str.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None


def _validate_time_range(start: datetime, end: datetime) -> None:
    """Raise ValueError if the time range exceeds MAX_QUERY_RANGE_HOURS."""
    diff_hours = (end - start).total_seconds() / 3600.0
    if diff_hours > MAX_QUERY_RANGE_HOURS:
        raise ValueError(
            f"Time range exceeds maximum allowed span of {MAX_QUERY_RANGE_HOURS:.0f} hours "
            f"(requested: {diff_hours:.1f}h). "
            f"Please narrow your start/end range. "
            f"start={start.isoformat()}, end={end.isoformat()}"
        )
    if start >= end:
        raise ValueError(
            f"start must be before end: start={start.isoformat()}, end={end.isoformat()}"
        )


def _default_time_range() -> tuple[datetime, datetime]:
    """Return a default time range (last 1 hour) when start/end not provided."""
    now = datetime.now(tz=UTC)
    return now - timedelta(hours=1), now


# ── Helper: Trace ID extraction from logs ────────────────────────────


def _extract_trace_ids_from_logs(logs: list[dict[str, Any]]) -> list[str]:
    """Extract unique trace_ids from log entries.

    Looks for trace_id in:
    1. Explicit trace_id field in log entry
    2. trace_id embedded in message text (32-char hex pattern)
    """
    trace_ids: list[str] = []
    seen: set[str] = set()

    for entry in logs:
        # Check explicit trace_id field
        tid = entry.get("trace_id")
        if tid and isinstance(tid, str) and len(tid) >= 32:
            clean = tid.strip()[:32].lower()
            if clean not in seen and re.match(r"^[0-9a-f]{32}$", clean):
                seen.add(clean)
                trace_ids.append(clean)
                continue

        # Search message text for trace IDs
        message = entry.get("message", "")
        matches = _TRACE_ID_RE.findall(message)
        for m in matches:
            clean = m.lower()
            if clean not in seen:
                seen.add(clean)
                trace_ids.append(clean)

    return trace_ids[:AUTO_MODE_MAX_TRACE_IDS]


# ── Helper: Parse log entries to dict ────────────────────────────────


def _log_entry_to_dict(entry: Any) -> dict[str, Any]:
    """Convert a LogEntry (Pydantic or dict) to a plain dict for JSON output."""
    if isinstance(entry, dict):
        result = dict(entry)
        # Normalise datetime objects
        for key in ("timestamp",):
            if key in result and isinstance(result[key], datetime):
                result[key] = result[key].isoformat()
        return result
    # Pydantic model
    try:
        d: dict[str, Any] = entry.model_dump()
        for key in ("timestamp",):
            if key in d and isinstance(d[key], datetime):
                d[key] = d[key].isoformat()
        return d
    except AttributeError:
        return {"raw": str(entry)}


def _span_to_dict(span: Any) -> dict[str, Any]:
    """Convert a TraceSpan (Pydantic or dict) to a plain dict for JSON output."""
    if isinstance(span, dict):
        result = dict(span)
        for key in ("start", "timestamp"):
            if key in result and isinstance(result[key], datetime):
                result[key] = result[key].isoformat()
        return result
    try:
        d: dict[str, Any] = span.model_dump()
        for key in ("start", "timestamp"):
            if key in d and isinstance(d[key], datetime):
                d[key] = d[key].isoformat()
        return d
    except AttributeError:
        return {"raw": str(span)}


# ── Helper: Anomaly detection ────────────────────────────────────────


def _normalize_error_message(msg: str) -> str:
    """Normalize an error message for clustering.

    - Strip UUIDs, hex strings, timestamps, line numbers
    - Lowercase
    - Truncate to first 120 chars as fingerprint
    """
    import re as _re

    normalized = msg.lower()
    # Remove UUIDs
    normalized = _re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "<uuid>",
        normalized,
    )
    # Remove hex strings (32-char)
    normalized = _re.sub(r"\b[0-9a-f]{32}\b", "<hex32>", normalized)
    # Remove ISO timestamps
    normalized = _re.sub(
        r"\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}[^\s]*", "<ts>", normalized
    )
    # Remove line numbers like :123 or line 42
    normalized = _re.sub(r":\d{2,5}(?:\b|\))", ":<line>", normalized)
    normalized = _re.sub(r"line \d+", "line <n>", normalized)

    return normalized[:120]


def _parse_log_time(entry: dict[str, Any]) -> datetime | None:
    """Parse timestamp from a log entry dict."""
    ts = entry.get("timestamp", "")
    if not ts:
        return None
    return _parse_time(str(ts))


def _detect_anomalies(
    logs: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Detect anomalies from logs and traces.

    检测规则：
    1. **错误突增**：某时间窗口内 ERROR 日志数 > 均值 + 2σ
    2. **延迟突增**：某 span 的 duration > 同类 span 均值 × 3
    3. **错误聚类**：相同错误消息在短时间内重复出现（burst）
    4. **级联失败**：一个 trace 内多个 span 同时报错
    5. **超时链**：span 链路上最后一个 span 耗时占比 > 80%

    Returns:
        List of anomaly dicts, each with ``type``, ``severity``, ``details``.
    """
    anomalies: list[dict[str, Any]] = []

    # ── 1. Error Burst Detection ─────────────────────────────────
    error_bursts = _detect_error_bursts(logs, start, end)
    anomalies.extend(error_bursts)

    # ── 2. Latency Spike Detection ───────────────────────────────
    latency_spikes = _detect_latency_spikes(traces)
    anomalies.extend(latency_spikes)

    # ── 3. Error Clustering (Burst) ──────────────────────────────
    error_clusters = _detect_error_clusters(logs)
    anomalies.extend(error_clusters)

    # ── 4. Cascading Failure Detection ───────────────────────────
    cascading_failures = _detect_cascading_failures(traces)
    anomalies.extend(cascading_failures)

    # ── 5. Timeout Chain Detection ───────────────────────────────
    timeout_chains = _detect_timeout_chains(traces)
    anomalies.extend(timeout_chains)

    return anomalies


# ── Sub-detectors ────────────────────────────────────────────────────


def _detect_error_bursts(
    logs: list[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Detect time windows where ERROR log count exceeds mean + 2σ."""
    anomalies: list[dict[str, Any]] = []

    # Collect only ERROR / FATAL logs with timestamps
    error_times: list[datetime] = []
    for entry in logs:
        ts = _parse_log_time(entry)
        if ts is None:
            continue
        severity = str(entry.get("severity", entry.get("level", ""))).upper()
        if severity in ("ERROR", "FATAL", "CRITICAL"):
            error_times.append(ts)

    if len(error_times) < 3:
        return anomalies  # Not enough data

    # Bucket into 1-minute windows
    total_span = (end - start).total_seconds()
    bucket_seconds = max(30, min(total_span / 20, 300))  # 30s-5min
    buckets: dict[int, int] = {}
    for t in error_times:
        bucket = int(t.timestamp() / bucket_seconds)
        buckets[bucket] = buckets.get(bucket, 0) + 1

    if not buckets:
        return anomalies

    counts = list(buckets.values())
    mean = sum(counts) / len(counts)
    if mean < 1.0:
        return anomalies  # Too sparse

    variance = sum((c - mean) ** 2 for c in counts) / len(counts)
    stddev = variance**0.5

    threshold = mean + 2 * stddev
    # Edge case: only a few buckets, or all errors in one bucket
    # Apply a minimum absolute threshold so single-bucket bursts still fire
    if threshold < 3:
        threshold = max(mean + 2, 3)
    # If we have very few buckets (< 5), relax the threshold further
    if len(buckets) < 5 and max(counts) >= 5:
        # Consider the top bucket anomalous if it's > 2x the next highest
        sorted_counts = sorted(counts, reverse=True)
        if len(sorted_counts) >= 2:
            if sorted_counts[0] > sorted_counts[1] * 2:
                # Mark only the top bucket as anomalous
                max_bucket = max(buckets, key=lambda k: buckets[k])
                window_start = datetime.fromtimestamp(
                    max_bucket * bucket_seconds, tz=UTC
                )
                window_end = datetime.fromtimestamp(
                    (max_bucket + 1) * bucket_seconds, tz=UTC
                )
                anomalies.append(
                    {
                        "type": "error_burst",
                        "severity": "high" if sorted_counts[0] >= 10 else "medium",
                        "details": {
                            "window_start": window_start.isoformat(),
                            "window_end": window_end.isoformat(),
                            "error_count": sorted_counts[0],
                            "mean_count": round(mean, 1),
                            "stddev": round(stddev, 1),
                            "threshold": round(threshold, 1),
                            "excess_pct": round(
                                (sorted_counts[0] - mean) / max(mean, 1) * 100
                            ),
                        },
                    }
                )
                return anomalies
        else:
            # Only 1 bucket — flag as burst if count is substantial (≥8)
            # relative to the time window size
            if counts[0] >= 8:
                max_bucket = max(buckets, key=lambda k: buckets[k])
                window_start = datetime.fromtimestamp(
                    max_bucket * bucket_seconds, tz=UTC
                )
                window_end = datetime.fromtimestamp(
                    (max_bucket + 1) * bucket_seconds, tz=UTC
                )
                anomalies.append(
                    {
                        "type": "error_burst",
                        "severity": "high" if counts[0] >= 10 else "medium",
                        "details": {
                            "window_start": window_start.isoformat(),
                            "window_end": window_end.isoformat(),
                            "error_count": counts[0],
                            "mean_count": round(mean, 1),
                            "stddev": round(stddev, 1),
                            "threshold": "absolute_minimum(≥8)",
                            "excess_pct": 0,
                            "note": "Single-bucket burst; insufficient baseline for σ-based threshold.",
                        },
                    }
                )
                return anomalies

    for bucket_id, count in buckets.items():
        if count > threshold:
            window_start = datetime.fromtimestamp(
                bucket_id * bucket_seconds, tz=UTC
            )
            window_end = datetime.fromtimestamp(
                (bucket_id + 1) * bucket_seconds, tz=UTC
            )
            anomalies.append(
                {
                    "type": "error_burst",
                    "severity": "high" if count > mean + 3 * stddev else "medium",
                    "details": {
                        "window_start": window_start.isoformat(),
                        "window_end": window_end.isoformat(),
                        "error_count": count,
                        "mean_count": round(mean, 1),
                        "stddev": round(stddev, 1),
                        "threshold": round(threshold, 1),
                        "excess_pct": round((count - mean) / max(mean, 1) * 100),
                    },
                }
            )

    return anomalies


def _detect_latency_spikes(
    traces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect spans where duration exceeds same-operation mean × 3."""
    anomalies: list[dict[str, Any]] = []

    # Group spans by operation name
    by_name: dict[str, list[dict[str, Any]]] = {}
    for span in traces:
        name = span.get("name", span.get("operation_name", ""))
        if not name:
            continue
        duration = span.get("duration_ms", span.get("duration", 0))
        if not duration or float(duration) <= 0:
            continue
        by_name.setdefault(name, []).append(span)

    for name, spans in by_name.items():
        if len(spans) < 3:
            continue  # Not enough for statistical comparison

        durations = [
            float(s.get("duration_ms", s.get("duration", 0))) for s in spans
        ]
        avg = sum(durations) / len(durations)
        if avg < 10:
            continue  # Too fast to care

        for span in spans:
            d = float(span.get("duration_ms", span.get("duration", 0)))
            if d > avg * 3 and d > 100:  # Also minimum 100ms threshold
                anomalies.append(
                    {
                        "type": "latency_spike",
                        "severity": "high" if d > avg * 5 else "medium",
                        "details": {
                            "operation": name,
                            "span_id": span.get("span_id", ""),
                            "trace_id": span.get("trace_id", ""),
                            "duration_ms": d,
                            "avg_duration_ms": round(avg, 1),
                            "multiplier": round(d / avg, 1),
                            "sample_count": len(spans),
                        },
                    }
                )

    return anomalies


def _detect_error_clusters(
    logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect same error message appearing repeatedly (burst) in a short window."""
    anomalies: list[dict[str, Any]] = []

    # Extract error entries with timestamps and normalized messages
    error_entries: list[tuple[datetime, str, str]] = []
    for entry in logs:
        ts = _parse_log_time(entry)
        if ts is None:
            continue
        severity = str(entry.get("severity", entry.get("level", ""))).upper()
        if severity not in ("ERROR", "FATAL", "CRITICAL"):
            continue
        msg = entry.get("message", entry.get("line_content", ""))
        if not msg:
            continue
        normalized = _normalize_error_message(str(msg))
        error_entries.append((ts, normalized, str(msg)[:200]))

    if len(error_entries) < 5:
        return anomalies

    # Sort by time
    error_entries.sort(key=lambda x: x[0])

    # Sliding window: group by fingerprint within 2-minute windows
    CLUSTER_WINDOW_SECONDS = 120
    CLUSTER_MIN_COUNT = 5

    # Use a simple sliding-window approach: for each fingerprint,
    # count occurrences in the next 2 minutes
    fingerprinted: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()

    for i, (ts, fingerprint, original) in enumerate(error_entries):
        if fingerprint in seen_fingerprints:
            continue
        # Count how many of this fingerprint in next 2 minutes
        count = 1
        samples: list[str] = [original]
        for j in range(i + 1, len(error_entries)):
            if (error_entries[j][0] - ts).total_seconds() > CLUSTER_WINDOW_SECONDS:
                break
            if error_entries[j][1] == fingerprint:
                count += 1
                if len(samples) < 3:
                    samples.append(error_entries[j][2])

        if count >= CLUSTER_MIN_COUNT:
            fingerprinted.append(
                {
                    "fingerprint": fingerprint[:80],
                    "count": count,
                    "window_start": ts.isoformat(),
                    "sample_messages": samples,
                }
            )
            seen_fingerprints.add(fingerprint)

    for cluster in fingerprinted:
        anomalies.append(
            {
                "type": "error_cluster",
                "severity": "high" if cluster["count"] >= 10 else "medium",
                "details": {
                    "fingerprint": cluster["fingerprint"],
                    "occurrence_count": cluster["count"],
                    "window_start": cluster["window_start"],
                    "sample": cluster["sample_messages"][0],
                },
            }
        )

    return anomalies


def _detect_cascading_failures(
    traces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect traces where multiple spans report errors simultaneously."""
    anomalies: list[dict[str, Any]] = []

    # Group spans by trace_id
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for span in traces:
        tid = span.get("trace_id", "")
        if not tid:
            continue
        by_trace.setdefault(tid, []).append(span)

    for tid, spans in by_trace.items():
        if len(spans) < 2:
            continue

        error_spans: list[dict[str, Any]] = []
        for span in spans:
            status = str(span.get("status", "")).lower()
            if status in ("error", "failed", "fault"):
                error_spans.append(span)

        if len(error_spans) >= 2:
            # Cascading failure: ≥2 spans in same trace errored
            anomalies.append(
                {
                    "type": "cascading_failure",
                    "severity": "high" if len(error_spans) >= 3 else "medium",
                    "details": {
                        "trace_id": tid,
                        "total_spans": len(spans),
                        "error_span_count": len(error_spans),
                        "error_span_names": [
                            s.get("name", s.get("operation_name", "?"))
                            for s in error_spans[:5]
                        ],
                        "error_messages": [
                            str(
                                s.get("attributes", {}).get(
                                    "error.message",
                                    s.get("message", ""),
                                )
                            )[:120]
                            for s in error_spans[:5]
                        ],
                    },
                }
            )

    return anomalies


def _detect_timeout_chains(
    traces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect span chains where the last span accounts for > 80% of total duration."""
    anomalies: list[dict[str, Any]] = []

    if not traces:
        return anomalies

    # Build span tree to find root-to-leaf chains
    roots = build_cross_tier_tree(traces)
    if not roots:
        return anomalies

    def _walk_chains(
        node: SpanNode,
        current_chain: list[SpanNode],
        total_duration: float,
    ) -> None:
        """Walk depth-first to find leaf nodes and check timeout chain."""
        chain = current_chain + [node]
        chain_duration = sum(n.duration_ms for n in chain)

        if not node.children and len(chain) >= 2:
            # At leaf — check if last span dominates
            last_duration = chain[-1].duration_ms
            if chain_duration > 0 and last_duration / chain_duration > 0.8:
                anomalies.append(
                    {
                        "type": "timeout_chain",
                        "severity": "medium",
                        "details": {
                            "chain": " → ".join(
                                f"{n.name}({n.duration_ms:.0f}ms)"
                                for n in chain
                            ),
                            "total_duration_ms": chain_duration,
                            "last_span_name": chain[-1].name,
                            "last_span_duration_ms": chain[-1].duration_ms,
                            "last_span_ratio_pct": round(
                                last_duration / chain_duration * 100, 1
                            ),
                            "span_count": len(chain),
                        },
                    }
                )

        for child in node.children:
            _walk_chains(child, chain, 0)  # total reset per chain is fine

    for root in roots:
        _walk_chains(root, [], 0)

    return anomalies


# ── Helper: Causal chain reconstruction ──────────────────────────────


def _derive_tier_label(node: SpanNode) -> str:
    """Derive a human-readable tier label for a span node.

    Returns one of: ``"Frontend"``, ``"Backend"``, ``"DB"``.
    """
    if node.is_frontend:
        return "Frontend"
    if node.is_db_span:
        return "DB"
    return "Backend"


def _build_causal_chains(roots: list[SpanNode]) -> list[str]:
    """Build causal chain representations from span trees.

    Post-order traverses each span tree.  For branches that contain
    error or slow spans, renders an indented text chain showing the
    causal flow from root → intermediate → leaf, including tier,
    operation name, duration, and status.

    Example output::

        Frontend fetch /api/tasks (error)
          → Backend GET /api/tasks (error)
            → DB SELECT tasks (slow, 1200ms)
            → DB SELECT comments (50ms, ×5 N+1 pattern)

    Args:
        roots: Root nodes from ``build_cross_tier_tree()``.

    Returns:
        List of indented text strings, one per problematic root node.
    """
    chains: list[str] = []

    def _has_problems(n: SpanNode) -> bool:
        """Check if node or any descendant is error or slow."""
        if n.is_error or n.is_slow():
            return True
        return any(_has_problems(c) for c in n.children)

    def _render_branch(node: SpanNode, indent: int = 0) -> str:
        """Render a single branch as indented text."""
        prefix = "→ " if indent > 0 else ""
        padding = "  " * indent

        tier = _derive_tier_label(node)
        name = node.name or "(unnamed)"

        # Build status annotation
        if node.is_error:
            annotation = "error"
        elif node.is_slow():
            annotation = f"slow, {node.duration_ms:.0f}ms"
        else:
            annotation = f"{node.duration_ms:.0f}ms"

        line = f"{padding}{prefix}{tier} {name} ({annotation})"

        # Collect problem children
        problem_children = [
            c for c in node.children if _has_problems(c)
        ]

        if not problem_children:
            return line

        child_lines = [_render_branch(c, indent + 1) for c in problem_children]
        return "\n".join([line] + child_lines)

    for root in roots:
        if _has_problems(root):
            chains.append(_render_branch(root))

    return chains


# ── Helper: Insight generation ───────────────────────────────────────


def _generate_insights(analysis_result: dict[str, Any]) -> str:
    """Generate a human-readable insight summary from analysis data.

    Pure rule engine — no LLM call.  Translates structured analysis
    output (anomalies, bottlenecks, causal chains, etc.) into a concise
    "## 自动洞察" Markdown section with actionable next-step suggestions.

    Args:
        analysis_result: The ``analysis`` dict produced by
            ``_run_trace_analysis`` + anomaly detection.

    Returns:
        Markdown-formatted insight text, or empty string if nothing to report.
    """
    parts: list[str] = []

    # ── 1. Error patterns from anomaly clusters ─────────────────
    clusters = [
        a["details"]
        for a in analysis_result.get("anomalies", [])
        if a.get("type") == "error_cluster"
    ]
    if clusters:
        top = clusters[0]
        n = top.get("occurrence_count", 0)
        pattern = top.get("fingerprint", "unknown")
        window = top.get("window_start", "?")
        parts.append(
            f"1. **错误模式**：检测到 {n} 个相同错误 "
            f'"{pattern[:60]}"，集中在 {window[:19]}'
        )

    # ── 2. Performance bottlenecks ──────────────────────────────
    bottlenecks = analysis_result.get("bottlenecks", [])
    if bottlenecks:
        durations = [float(b.get("duration_ms", 0)) for b in bottlenecks]
        if durations:
            p95 = sorted(durations)[int(len(durations) * 0.95)]
            slowest = max(bottlenecks, key=lambda b: float(b.get("duration_ms", 0)))
            parts.append(
                f"2. **性能瓶颈**：P95 延迟 {p95:.0f}ms，"
                f"最慢端点 {slowest.get('name', '?')}"
                f"（{slowest.get('duration_ms', 0):.0f}ms）"
            )

    # ── 3. Causal chains ────────────────────────────────────────
    chains = analysis_result.get("causal_chains", [])
    if chains:
        chain_text = "\n   ".join(chains[:3])  # top 3 chains
        parts.append(f"3. **因果链**：\n   {chain_text}")

    # ── 4. Next-step suggestions ────────────────────────────────
    suggestions: list[str] = []

    # From error spans → suggest code search
    error_spans = analysis_result.get("error_spans", [])
    if error_spans:
        key_func = error_spans[0].get("name", "")
        if key_func:
            suggestions.append(f'用 code_search 搜索 "{key_func}" 的实现')

    # From N+1 → suggest get_file_content
    n1 = analysis_result.get("n_plus_one", [])
    if n1:
        parent_span = n1[0].get("parent_span", n1[0].get("span_name", ""))
        suggestions.append(f"用 get_file_content 查看 {parent_span} 相关代码")
    elif error_spans:
        file_hint = error_spans[0].get("name", "suspected_file")
        suggestions.append(f"用 get_file_content 查看包含 {file_hint} 的文件")

    # From cascading failures → suggest db_query
    cascades = [
        a["details"]
        for a in analysis_result.get("anomalies", [])
        if a.get("type") == "cascading_failure"
    ]
    if cascades:
        suggestions.append(
            "用 db_query 验证数据库状态（检测到级联失败，"
            "可能涉及数据完整性问题）"
        )

    # General fallback suggestion
    if not suggestions:
        suggestions.append(
            "用 search_observability 缩小时间窗口，获取更精确的线索"
        )

    parts.append(
        f"4. **建议下一步**：\n   - " + "\n   - ".join(suggestions)
    )

    if not parts:
        return ""

    return "## 自动洞察\n\n" + "\n\n".join(parts)


# ── Helper: Run trace analysis ───────────────────────────────────────


def _run_trace_analysis(
    traces: list[dict[str, Any]],
    analysis: Literal["raw", "n_plus_one", "bottlenecks", "errors", "full"],
) -> dict[str, Any]:
    """Run the requested analysis on trace data.

    Args:
        traces: Flat list of trace span dicts.
        analysis: What analysis to run.

    Returns:
        Analysis result dict, possibly with sub-keys like
        n_plus_one, bottlenecks, error_spans, critical_path.
    """
    if not traces:
        return {"note": "No trace data to analyze"}

    roots: list[SpanNode] = build_cross_tier_tree(traces)

    result: dict[str, Any] = {"span_count": len(traces), "root_count": len(roots)}

    if analysis in ("n_plus_one", "full"):
        result["n_plus_one"] = detect_n_plus_one(roots)

    if analysis in ("bottlenecks", "full"):
        result["bottlenecks"] = find_bottlenecks(roots)

    if analysis in ("errors", "full"):
        result["error_spans"] = find_error_spans(roots)

    if analysis == "full":
        summary = get_tree_summary(roots)
        result["summary"] = summary
        # Causal chain reconstruction (Task 1.13)
        chains = _build_causal_chains(roots)
        if chains:
            result["causal_chains"] = chains

    return result


# ── Public API ──────────────────────────────────────────────────────


@traced("observability.search_observability")
async def search_observability(
    source: Literal["loki", "tempo", "auto"],
    query: str,
    start: str | None = None,
    end: str | None = None,
    analysis: Literal["raw", "n_plus_one", "bottlenecks", "errors", "full"] = "full",
    limit: int = 20,
    include_frontend: bool = False,
) -> str:
    """统一可观测性查询入口。

    根据 ``source`` 参数决定查询策略:
    - ``source="loki"``: 仅查询 Loki 日志
    - ``source="tempo"``: 查询 Tempo trace（query 为 trace_id 时直接查 trace，
      否则按服务名搜索 traces）
    - ``source="auto"``: 先查 Loki 获取日志和 trace_id，再自动关联查 Tempo

    根据 ``analysis`` 参数决定分析深度:
    - ``"raw"``: 仅返回原始数据
    - ``"n_plus_one"``: 仅做 N+1 检测
    - ``"bottlenecks"``: 仅做瓶颈分析
    - ``"errors"``: 仅提取错误 span
    - ``"full"``: 全部分析

    Args:
        source: 数据源 — "loki" | "tempo" | "auto"
        query: 查询字符串。source="loki" 时为 LogQL；
               source="tempo" 时为 trace_id 或服务名；
               source="auto" 时为 LogQL
        start: ISO 格式起始时间（可选，默认为 1 小时前）
        end: ISO 格式结束时间（可选，默认为当前时间）
        analysis: 分析深度（仅在使用 trace 数据时生效）
        limit: 最大返回条数
        include_frontend: 是否同时查询前端服务（demo-frontend）的错误。
            当设为 True 时，额外查询前端 Loki 日志和 Tempo 中的
            client_error span，提取到 frontend_errors 字段。
            适用于前端白屏/崩溃类 Bug 诊断。

    Returns:
        JSON 字符串，结构为:
        {
            "source": str,
            "query": str,
            "time_range": {"start": "...", "end": "..."},
            "logs": [...],
            "traces": [...],
            "analysis": {...},
            "metadata": {"auto_trace_ids": [...], ...},
            "frontend_errors": [{"name": "client_error", "message": "...", ...}, ...]
        }
    """
    # ── Parse and validate time range ──
    parsed_start = _parse_time(start)
    parsed_end = _parse_time(end)

    if parsed_start is None or parsed_end is None:
        parsed_start, parsed_end = _default_time_range()
        logger.info(
            "search_observability_default_time_range",
            start=parsed_start.isoformat(),
            end=parsed_end.isoformat(),
        )
    else:
        _validate_time_range(parsed_start, parsed_end)

    # ── Execute queries based on source ──
    logs: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}

    if source in ("loki", "auto"):
        # Query Loki logs
        try:
            raw_logs = await query_loki_logs(
                logql=query,
                start=parsed_start,
                end=parsed_end,
                limit=AUTO_MODE_LOG_LIMIT if source == "auto" else limit * 10,
            )
            logs = [_log_entry_to_dict(e) for e in raw_logs]
            logger.info(
                "search_observability_loki_done",
                source=source,
                log_count=len(logs),
            )
        except Exception as exc:
            logger.error("search_observability_loki_failed", error=str(exc))
            logs = []

        # Auto mode: extract trace_ids and query Tempo
        if source == "auto" and logs:
            trace_ids = _extract_trace_ids_from_logs(logs)
            metadata["auto_trace_ids"] = trace_ids
            logger.info(
                "search_observability_auto_trace_ids",
                count=len(trace_ids),
                trace_ids=trace_ids,
            )

            for tid in trace_ids:
                try:
                    raw_spans = await query_tempo_trace(tid)
                    spans = [_span_to_dict(s) for s in raw_spans]
                    traces.extend(spans)
                    logger.info(
                        "search_observability_auto_tempo_done",
                        trace_id=tid,
                        span_count=len(spans),
                    )
                except Exception as exc:
                    logger.warning(
                        "search_observability_auto_tempo_failed",
                        trace_id=tid,
                        error=str(exc),
                    )
                    continue

    elif source == "tempo":
        # Determine if query is a trace_id (32-char hex) or service name
        if re.match(r"^[0-9a-fA-F]{32}$", query.strip()):
            # Direct trace lookup
            try:
                raw_spans = await query_tempo_trace(query.strip())
                traces = [_span_to_dict(s) for s in raw_spans]
                metadata["trace_id"] = query.strip()
                logger.info(
                    "search_observability_tempo_trace_done",
                    trace_id=query.strip(),
                    span_count=len(traces),
                )
            except Exception as exc:
                logger.error(
                    "search_observability_tempo_trace_failed",
                    trace_id=query.strip(),
                    error=str(exc),
                )
                traces = []
        else:
            # Search by service name
            try:
                trace_summaries = await search_tempo_traces(
                    service=query.strip(),
                    start=parsed_start,
                    end=parsed_end,
                )
                metadata["tempo_search_results"] = trace_summaries
                logger.info(
                    "search_observability_tempo_search_done",
                    service=query,
                    result_count=len(trace_summaries),
                )
            except Exception as exc:
                logger.error(
                    "search_observability_tempo_search_failed",
                    service=query,
                    error=str(exc),
                )
                metadata["tempo_search_results"] = []

    # ── Run analysis on trace data ──
    analysis_result: dict[str, Any] = {}
    if traces and analysis != "raw":
        try:
            analysis_result = _run_trace_analysis(traces, analysis)
            logger.info(
                "search_observability_analysis_done",
                analysis=analysis,
                keys=list(analysis_result.keys()),
            )
        except Exception as exc:
            logger.error("search_observability_analysis_failed", error=str(exc))
            analysis_result = {"error": str(exc)}

    # ── Anomaly detection (logs + traces) ──────────────────────────
    if logs or traces:
        try:
            anomalies = _detect_anomalies(logs, traces, parsed_start, parsed_end)
            if anomalies:
                analysis_result["anomalies"] = anomalies
                logger.info(
                    "search_observability_anomalies_detected",
                    count=len(anomalies),
                    types=[a["type"] for a in anomalies],
                )
        except Exception as exc:
            logger.warning(
                "search_observability_anomaly_detection_failed", error=str(exc)
            )

    # ── Include frontend errors (optional) ────────────────────────
    frontend_errors: list[dict[str, Any]] = []
    if include_frontend:
        try:
            fe_logql = '{service_name=~"demo-frontend"}'
            fe_logs_raw = await query_loki_logs(
                logql=fe_logql,
                start=parsed_start,
                end=parsed_end,
                limit=AUTO_MODE_LOG_LIMIT,
            )
            fe_logs = [_log_entry_to_dict(e) for e in fe_logs_raw]
            fe_trace_ids = _extract_trace_ids_from_logs(fe_logs)

            for tid in fe_trace_ids[:AUTO_MODE_MAX_TRACE_IDS]:
                try:
                    raw_spans = await query_tempo_trace(tid)
                    fe_spans = [_span_to_dict(s) for s in raw_spans]
                    for span in fe_spans:
                        name = span.get("name", "")
                        if "client_error" in name or span.get("status") == "error":
                            attrs = span.get("attributes", {})
                            frontend_errors.append(
                                {
                                    "span_name": name,
                                    "trace_id": tid,
                                    "span_id": span.get("span_id", ""),
                                    "duration_ms": span.get("duration_ms", 0),
                                    "error_message": (
                                        attrs.get("error.message") or attrs.get("error", "")
                                    ),
                                    "error_stack": attrs.get("error.stack", ""),
                                    "component_stack": attrs.get("component_stack", ""),
                                    "timestamp": span.get("start", ""),
                                }
                            )
                    # Also add all frontend spans to the main traces
                    traces.extend(fe_spans)
                except Exception as fe_exc:
                    logger.debug(
                        "frontend_error_trace_fetch_failed",
                        trace_id=tid,
                        error=str(fe_exc),
                    )

            # Add frontend logs to main logs
            logs.extend(fe_logs)
            logger.info(
                "search_observability_frontend_errors",
                count=len(frontend_errors),
                log_count=len(fe_logs),
            )
        except Exception as fe_exc:
            logger.warning("search_observability_frontend_failed", error=str(fe_exc))

    # ── Generate insights (Task 1.14) ────────────────────────────
    insights_text = ""
    if analysis != "raw":
        try:
            insights_text = _generate_insights(analysis_result)
            if insights_text:
                logger.info("search_observability_insights_generated")
        except Exception as exc:
            logger.warning(
                "search_observability_insights_failed", error=str(exc)
            )

    # ── Assemble response ──
    response = {
        "source": source,
        "query": query,
        "time_range": {
            "start": parsed_start.isoformat(),
            "end": parsed_end.isoformat(),
        },
        "logs": logs[:limit],
        "traces": traces[:limit],
        "analysis": analysis_result,
        "metadata": metadata,
        "frontend_errors": frontend_errors[:limit],
        "insights": insights_text,
    }

    # Truncate large payloads for LLM context
    result_json = json.dumps(response, ensure_ascii=False, indent=2, default=str)
    original_json = result_json  # keep reference for accurate comparison

    if len(result_json) > 8000:
        # Truncate logs and traces arrays, keep analysis (including anomalies)
        truncated: dict[str, Any] = dict(response)
        truncated["logs"] = logs[:5]
        truncated["traces"] = traces[:5]
        truncated["_truncated"] = True
        truncated["_original_counts"] = {
            "logs": len(logs),
            "traces": len(traces),
            "anomalies": len(analysis_result.get("anomalies", [])),
        }
        result_json = json.dumps(truncated, ensure_ascii=False, indent=2, default=str)
        logger.warning(
            "search_observability_truncated",
            original_size=len(original_json),
            truncated_size=len(result_json),
            reduction_pct=round((1 - len(result_json) / len(original_json)) * 100, 1),
        )

    return result_json


# ── LangChain StructuredTool ─────────────────────────────────────────


def _build_search_observability_tool() -> Any:
    """Build the LangChain StructuredTool for search_observability."""
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        coroutine=search_observability,
        name="search_observability",
        description=(
            "统一可观测性查询入口 ⭐ 优先使用。\n"
            "支持三种模式：\n"
            "1. 查日志: search_observability(source='loki', "
            'query=\'{service_name="demo-backend"} |= "error"\', '
            "start='...', end='...')\n"
            "2. 查Trace: search_observability(source='tempo', "
            "query='<trace_id>')\n"
            "3. 自动关联: search_observability(source='auto', "
            "query='<LogQL>', analysis='full') — 先查Loki获取日志和trace_id，"
            "再自动关联查Tempo，并进行N+1/瓶颈/错误分析\n"
            "analysis 参数: 'raw'(仅原始数据), 'n_plus_one'(N+1检测), "
            "'bottlenecks'(瓶颈分析), 'errors'(错误span), 'full'(全部分析)\n"
            "include_frontend=True: 同时查询前端 client_error span 和日志，"
            "适用于前端白屏/崩溃类 Bug\n"
            "时间范围跨度不能超过4小时。返回JSON: "
            "{logs:..., traces:..., analysis:..., frontend_errors:...}"
        ),
    )


# Deferred construction: built on first access via __init__.py
_search_obs_tool_cache: Any = None


def get_search_observability_tool() -> Any:
    """Get or create the cached SEARCH_OBSERVABILITY_TOOL."""
    global _search_obs_tool_cache
    if _search_obs_tool_cache is None:
        _search_obs_tool_cache = _build_search_observability_tool()
    return _search_obs_tool_cache


# ── Public API ──────────────────────────────────────────────────────

__all__ = [
    "search_observability",
    "get_search_observability_tool",
]
