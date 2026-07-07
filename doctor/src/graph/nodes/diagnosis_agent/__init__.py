"""DiagnosisAgent LangGraph node — wraps the V3 ReAct agent as a graph node.

Connects the DiagnosisAgent subgraph into the main DiagDoctor graph.
Formats normalized evidence from DoctorState, invokes the ReAct agent,
and parses the result into DiagnosisReport + Findings.

This package was split out of a single 998-line ``diagnosis_agent.py`` for
readability. The public API below is re-exported for backward compatibility —
all existing import paths (``main_graph.py``, ``nodes/__init__.py``, the
``dump_session_json_status.py`` script, and the active
``test_forced_final_json_call.py`` test) keep working unchanged.

Re-exported public API:
    - ``diagnosis_agent_node`` — the LangGraph node itself
    - Evidence: ``format_evidence_for_agent``
    - Parsing: ``parse_diagnosis_report``, ``extract_findings``,
      ``_extract_json_from_text``, ``_extract_json_by_depth``, ``_ensure_str_list``
    - Budget: ``update_budget``, ``is_budget_exceeded``, ``estimate_tokens``
    - Failure: ``handle_agent_failure``
    - Iteration 1 forced call: ``_forced_final_json_call``,
      ``_maybe_forced_final_json_call``, ``_last_ai_has_json``,
      ``_last_ai_is_natural_stop``
    - Constants: ``MAX_TOOL_CALLS``, ``MAX_TOKENS_BUDGET``, ``MAX_TIME_SECONDS``,
      ``BUDGET_WARNING_THRESHOLD``

Usage (in main_graph.py)::

    from src.graph.nodes.diagnosis_agent import diagnosis_agent_node

    g.add_node("diagnosis_agent", diagnosis_agent_node)
"""

from __future__ import annotations

from src.graph.nodes.diagnosis_agent.budget import is_budget_exceeded, update_budget
from src.graph.nodes.diagnosis_agent.constants import (
    BUDGET_WARNING_THRESHOLD,
    MAX_TIME_SECONDS,
    MAX_TOKENS_BUDGET,
    MAX_TOOL_CALLS,
    estimate_tokens,
)
from src.graph.nodes.diagnosis_agent.evidence import format_evidence_for_agent
from src.graph.nodes.diagnosis_agent.failure import handle_agent_failure
from src.graph.nodes.diagnosis_agent.forced_call import (
    ForcedDiagnosisReport,
    _forced_final_json_call,
    _last_ai_has_json,
    _last_ai_is_natural_stop,
    _maybe_forced_final_json_call,
)
from src.graph.nodes.diagnosis_agent.node import diagnosis_agent_node
from src.graph.nodes.diagnosis_agent.parsing import (
    _ensure_str_list,
    _extract_json_by_depth,
    _extract_json_from_text,
    extract_findings,
    parse_diagnosis_report,
)

__all__ = [
    # Node
    "diagnosis_agent_node",
    # Evidence
    "format_evidence_for_agent",
    # Parsing
    "parse_diagnosis_report",
    "extract_findings",
    "_extract_json_from_text",
    "_extract_json_by_depth",
    "_ensure_str_list",
    # Budget
    "update_budget",
    "is_budget_exceeded",
    "estimate_tokens",
    # Failure
    "handle_agent_failure",
    # Iteration 1 forced final JSON call
    "_forced_final_json_call",
    "_maybe_forced_final_json_call",
    "_last_ai_has_json",
    "_last_ai_is_natural_stop",
    "ForcedDiagnosisReport",
    # Constants
    "MAX_TOOL_CALLS",
    "MAX_TOKENS_BUDGET",
    "MAX_TIME_SECONDS",
    "BUDGET_WARNING_THRESHOLD",
]
