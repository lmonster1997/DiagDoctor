"""
Trace query tools — span tree construction & analysis.

Provides shared utilities for building cross-tier span trees and
analysing trace data. Used by:

1. **Ingest layer**: ``build_cross_tier_tree`` normalises flat span lists
   into hierarchical trees for signal extraction.
2. **Specialist agents**: functions like ``detect_n_plus_one``,
   ``find_bottlenecks`` are called as shared tools via ReAct agents.

Key design:
    - Frontend fetch spans are parents of backend server spans
      (same trace_id, parent_span_id chain)
    - The tree supports cross-tier queries: "what backend spans
      were triggered by this frontend click?"
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


# ── Span tree data structures ───────────────────────────────────────


@dataclass
class SpanNode:
    """A node in the span tree."""

    span_id: str
    parent_span_id: str = ""
    name: str = ""
    service_name: str = ""
    service_tier: str = "backend"  # "frontend" | "backend"
    duration_ms: float = 0.0
    status: str = "unset"  # "ok" | "error" | "unset"
    db_statement: str = ""
    start: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    children: list[SpanNode] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_frontend(self) -> bool:
        return self.service_tier == "frontend"

    @property
    def is_backend(self) -> bool:
        return self.service_tier == "backend"

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    def is_slow(self, threshold_ms: float = 200.0) -> bool:
        return self.duration_ms >= threshold_ms

    @property
    def is_db_span(self) -> bool:
        return bool(self.db_statement) or "db" in self.name.lower()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict (subset of fields for JSON/tool output)."""
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "service_name": self.service_name,
            "service_tier": self.service_tier,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "db_statement": self.db_statement,
            "start": self.start,
            "children_count": len(self.children),
        }


# ── Tree construction ───────────────────────────────────────────────


def _derive_service_tier(span: dict[str, Any]) -> str:
    """Derive service tier from span metadata (config-driven)."""
    from src.config import settings

    svc = str(span.get("service_name", span.get("service", ""))).lower()
    # Config-driven: match against configured service names
    if settings.frontend_service_name.lower() in svc:
        return "frontend"
    if settings.backend_service_name.lower() in svc:
        return "backend"
    # Generic fallback
    if "frontend" in svc:
        return "frontend"
    # Heuristic: OTel standard span name patterns
    name = str(span.get("name", "")).lower()
    frontend_patterns = [
        "fetch",
        "xhr",
        "click",
        "navigate",
        "documentload",
        "user_interaction",
        "route_change",
        "component",
    ]
    for p in frontend_patterns:
        if p in name:
            return "frontend"
    return "backend"


def _normalize_span(raw: dict[str, Any]) -> SpanNode:
    """Convert a raw span dict to a SpanNode."""
    svc = str(raw.get("service_name", raw.get("service", "")))
    attrs = raw.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}
    # db_statement may live at top-level (OTLP native) or inside attributes
    # (bug-factory TraceSpan schema).  Check both locations.
    db_stmt = str(
        raw.get("db_statement")
        or raw.get("dbStatement")
        or attrs.get("db.statement")
        or attrs.get("dbStatement")
        or ""
    )
    return SpanNode(
        span_id=str(raw.get("span_id", raw.get("spanId", ""))),
        parent_span_id=str(raw.get("parent_span_id", raw.get("parentSpanId", ""))),
        name=str(raw.get("name", "")),
        service_name=svc,
        service_tier=_derive_service_tier(raw),
        duration_ms=float(raw.get("duration_ms", raw.get("durationMs", 0) or 0)),
        status=str(raw.get("status", "unset")).lower(),
        db_statement=db_stmt,
        start=str(raw.get("start", raw.get("timestamp", ""))),
        attributes=attrs,
        raw=raw,
    )


def build_cross_tier_tree(
    traces: list[dict[str, Any]],
) -> list[SpanNode]:
    """
    Build cross-tier span trees from a flat list of trace spans.

    Links frontend fetch spans as parents of backend server spans
    that share the same trace context.  Returns a list of root nodes
    (spans with no parent in this trace), each forming a tree.

    The tree enables:
    - Tracing a frontend error down to the backend DB query that caused it
    - Identifying N+1 patterns (repeated DB spans under one parent)
    - Computing critical paths across tiers

    Args:
        traces: Flat list of trace span dicts.

    Returns:
        List of root SpanNodes forming the tree(s).
    """
    if not traces:
        return []

    # Convert to nodes and index by span_id
    nodes: dict[str, SpanNode] = {}
    for raw in traces:
        node = _normalize_span(raw)
        sid = node.span_id
        if sid and sid not in nodes:
            nodes[sid] = node

    # Build parent-child relationships
    roots: list[SpanNode] = []
    for node in nodes.values():
        parent_id = node.parent_span_id
        if parent_id and parent_id in nodes:
            nodes[parent_id].children.append(node)
        else:
            roots.append(node)

    return roots


def flatten_tree(roots: list[SpanNode]) -> list[SpanNode]:
    """Flatten a tree back to a list (DFS order)."""
    result: list[SpanNode] = []

    def _walk(node: SpanNode) -> None:
        result.append(node)
        for child in node.children:
            _walk(child)

    for root in roots:
        _walk(root)
    return result


# ── Tree analysis functions (shared tools for specialists) ──────────


def find_critical_path(roots: list[SpanNode]) -> list[SpanNode]:
    """
    Find the longest (most time-consuming) path through the span tree.

    Returns the list of SpanNodes on the critical path (root→leaf max duration).
    """
    best_path: list[SpanNode] = []
    best_duration: float = 0.0

    def _dfs(node: SpanNode, path: list[SpanNode], cum_duration: float) -> None:
        nonlocal best_path, best_duration
        path.append(node)
        cum_duration += node.duration_ms
        if not node.children:
            if cum_duration > best_duration:
                best_duration = cum_duration
                best_path = list(path)
        else:
            for child in node.children:
                _dfs(child, path, cum_duration)
        path.pop()

    for root in roots:
        _dfs(root, [], 0.0)

    return best_path


def find_bottlenecks(
    roots: list[SpanNode],
    threshold_ms: float = 200.0,
) -> list[dict[str, Any]]:
    """
    Find slow spans (bottlenecks) in the tree.

    Args:
        roots: Root nodes of span trees.
        threshold_ms: Spans slower than this are bottleneck candidates.

    Returns:
        List of dicts with span info sorted by duration descending.
    """
    bottlenecks: list[dict[str, Any]] = []
    for node in flatten_tree(roots):
        if node.duration_ms >= threshold_ms:
            bottlenecks.append(
                {
                    "span_id": node.span_id,
                    "name": node.name,
                    "service_name": node.service_name,
                    "service_tier": node.service_tier,
                    "duration_ms": node.duration_ms,
                    "db_statement": node.db_statement,
                }
            )
    bottlenecks.sort(key=lambda b: -b["duration_ms"])
    return bottlenecks


def find_error_spans(roots: list[SpanNode]) -> list[dict[str, Any]]:
    """
    Find all spans with status=error in the tree.

    Returns:
        List of dicts with error span info.
    """
    errors: list[dict[str, Any]] = []
    for node in flatten_tree(roots):
        if node.is_error:
            errors.append(
                {
                    "span_id": node.span_id,
                    "name": node.name,
                    "service_name": node.service_name,
                    "service_tier": node.service_tier,
                    "duration_ms": node.duration_ms,
                    "attributes": node.attributes,
                }
            )
    return errors


def detect_n_plus_one(roots: list[SpanNode]) -> list[dict[str, Any]]:
    """
    Detect N+1 query patterns in the span tree.

    An N+1 pattern is identified when one parent span has many child spans
    with the same (or very similar) db_statement, indicating repeated
    individual queries instead of a single batched query.

    Heuristic: same db_statement under same parent, count >= 3.

    Returns:
        List of dicts describing each N+1 pattern found.
    """
    patterns: list[dict[str, Any]] = []

    for node in flatten_tree(roots):
        if not node.children:
            continue

        # Group children by db_statement
        stmt_groups: dict[str, list[SpanNode]] = defaultdict(list)
        for child in node.children:
            stmt = child.db_statement.strip()
            if stmt:
                # Normalise: collapse parameter values
                norm = _normalise_sql(stmt)
                stmt_groups[norm].append(child)

        for norm_stmt, children in stmt_groups.items():
            if len(children) >= 3:
                # Use the first child's original statement as representative
                sample_stmt = children[0].db_statement
                patterns.append(
                    {
                        "pattern_id": f"nplus1-{_short_id()}",
                        "parent_span_id": node.span_id,
                        "parent_span_name": node.name,
                        "db_statement": sample_stmt,
                        "normalised_statement": norm_stmt,
                        "count": len(children),
                        "avg_duration_ms": sum(c.duration_ms for c in children) / len(children),
                        "total_duration_ms": sum(c.duration_ms for c in children),
                        "child_span_ids": [c.span_id for c in children[:10]],  # cap at 10
                    }
                )
    return patterns


def _normalise_sql(sql: str) -> str:
    """Normalise a SQL statement for comparison (collapse values)."""
    import re

    # Collapse quoted strings
    sql = re.sub(r"'[^']*'", "?", sql)
    # Collapse numbers
    sql = re.sub(r"\b\d+(\.\d+)?\b", "#", sql)
    # Collapse whitespace
    sql = re.sub(r"\s+", " ", sql)
    return sql.strip().lower()


def get_tree_summary(roots: list[SpanNode]) -> dict[str, Any]:
    """
    Get a high-level summary of the span tree for prompt context.

    Returns a compact dict suitable for feeding into LLM prompts.
    """
    all_nodes = flatten_tree(roots)
    if not all_nodes:
        return {
            "node_count": 0,
            "frontend_count": 0,
            "backend_count": 0,
            "error_count": 0,
            "n_plus_one_patterns": 0,
        }

    frontend = [n for n in all_nodes if n.is_frontend]
    backend = [n for n in all_nodes if n.is_backend]
    errors = [n for n in all_nodes if n.is_error]
    n_plus_ones = detect_n_plus_one(roots)
    bottlenecks = find_bottlenecks(roots)
    critical = find_critical_path(roots)

    return {
        "node_count": len(all_nodes),
        "frontend_count": len(frontend),
        "backend_count": len(backend),
        "error_count": len(errors),
        "error_span_names": [e.name for e in errors[:5]],
        "bottleneck_count": len(bottlenecks),
        "top_bottlenecks": bottlenecks[:5],
        "n_plus_one_patterns": len(n_plus_ones),
        "n_plus_one_details": n_plus_ones[:3],
        "critical_path_length": len(critical),
        "critical_path_duration_ms": sum(n.duration_ms for n in critical),
    }


# ── Tool registration ───────────────────────────────────────────────

# These are the shareable tool functions that specialist ReAct agents
# can call via LangChain StructuredTool wrappers.

__all__ = [
    "SpanNode",
    "build_cross_tier_tree",
    "flatten_tree",
    "find_critical_path",
    "find_bottlenecks",
    "find_error_spans",
    "detect_n_plus_one",
    "get_tree_summary",
]
