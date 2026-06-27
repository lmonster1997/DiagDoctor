"""
Debug script: load a bug-factory output case → run through the Doctor graph.

Usage:
    cd d:\Work\LearnAI\DiagDoctor
    .venv\Scripts\python.exe scripts\debug_case.py             # default: FE-020
    .venv\Scripts\python.exe scripts\debug_case.py BE-020      # specific case
    .venv\Scripts\python.exe scripts\debug_case.py PERF-020    # N+1 case

The script:
1. Loads evidence files (logs.json, traces.json, browser_errors.json)
2. Builds the DoctorState
3. Runs ingest → triage → reporter nodes, printing output at each step
4. Can be used with pdb/breakpoint() for interactive debugging

Set env LLM_API_KEY / LLM_BASE_URL / LLM_MODEL or a .env file in doctor/ for LLM calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


# ── Ensure doctor/src is importable ────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR_SRC = REPO_ROOT / "doctor" / "src"
if str(DOCTOR_SRC) not in sys.path:
    sys.path.insert(0, str(DOCTOR_SRC))


# ── Helpers ──────────────────────────────────────────────────────────


def load_case(case_id: str) -> dict:
    """Load evidence files for a bug-factory case."""
    case_dir = REPO_ROOT / "bug-factory" / "output" / case_id
    evidence_dir = case_dir / "evidence"

    if not case_dir.exists():
        print(f"[ERROR] Case dir not found: {case_dir}")
        sys.exit(1)

    # Load case YAML (simple key: value format)
    import yaml

    case_yaml = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    user_report = case_yaml.get("input", {}).get("user_report", "")
    expected = case_yaml.get("expected", {})

    # Load evidence files
    logs = json.loads((evidence_dir / "logs.json").read_text(encoding="utf-8"))
    traces = json.loads((evidence_dir / "traces.json").read_text(encoding="utf-8"))
    browser_path = evidence_dir / "browser_errors.json"
    browser_errors = (
        json.loads(browser_path.read_text(encoding="utf-8")) if browser_path.exists() else []
    )

    return {
        "case_id": case_id,
        "user_report": user_report,
        "logs": logs,
        "traces": traces,
        "browser_errors": browser_errors,
        "expected": expected,
    }


def print_section(title: str, char: str = "=") -> None:
    """Print a clearly visible section header."""
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── Main debug flow ─────────────────────────────────────────────────


async def run_case(case_id: str, skip_llm: bool = False) -> None:
    """Load a case and run through the Doctor graph."""
    from src.config import settings
    from src.graph.main_graph import generate_thread_id, get_graph
    from src.graph.state import BrowserError, DoctorState, Evidence, LogEntry, TraceSpan

    # ── Step 1: Load case ────────────────────────────────────────
    print_section(f"Step 1: Loading case {case_id}")
    data = load_case(case_id)
    print(f"  user_report: {data['user_report'][:120]}...")
    print(f"  logs: {len(data['logs'])} entries")
    print(f"  traces: {len(data['traces'])} spans")
    print(f"  browser_errors: {len(data['browser_errors'])} entries")
    expected = data["expected"]
    print(f"  expected.categories: {expected.get('categories', [])}")
    print(f"  expected.cross_layer: {expected.get('cross_layer', False)}")
    print(f"  expected.root_cause_tier: {expected.get('root_cause_tier', '?')}")

    # Quick raw data inspection for debugging
    print_section("Raw data inspection")
    log_trace_ids = {log.get("trace_id") for log in data["logs"] if log.get("trace_id")}
    span_trace_ids = {span.get("trace_id") for span in data["traces"] if span.get("trace_id")}
    span_service_names = {
        span.get("service_name", span.get("service", "")) for span in data["traces"]
    }
    log_service_names = {log.get("service_name", log.get("service", "")) for log in data["logs"]}
    print(f"  log entries with trace_id: {len(log_trace_ids)}/{len(data['logs'])}")
    print(f"  span entries with trace_id: {len(span_trace_ids)}/{len(data['traces'])}")
    print(f"  unique trace_ids (logs): {log_trace_ids}")
    print(f"  unique trace_ids (spans): {len(span_trace_ids)} unique")
    print(f"  log service_names: {log_service_names}")
    print(f"  span service_names: {span_service_names}")
    # Check if browser_errors have trace_id
    be_trace_ids = {be.get("trace_id") for be in data["browser_errors"] if be.get("trace_id")}
    print(f"  browser_error trace_ids: {be_trace_ids}")
    # Check span status distribution
    span_statuses = {}
    for span in data["traces"]:
        s = span.get("status", "unset")
        span_statuses[s] = span_statuses.get(s, 0) + 1
    print(f"  span status distribution: {span_statuses}")
    # Check for db.statement attributes
    db_stmt_spans = sum(1 for span in data["traces"] if span.get("db_statement", ""))
    print(f"  spans with db_statement: {db_stmt_spans}/{len(data['traces'])}")
    # Log level distribution
    log_levels = {}
    for log in data["logs"]:
        lvl = log.get("level", log.get("detected_level", "INFO"))
        log_levels[lvl] = log_levels.get(lvl, 0) + 1
    print(f"  log level distribution: {log_levels}")

    # ── Step 2: Build State ──────────────────────────────────────
    print_section("Step 2: Building DoctorState")
    thread_id = generate_thread_id()

    # Convert raw dicts to Pydantic models (Evidence / DoctorState)
    # Handle None / type mismatches / extra fields from bug-factory JSON
    def _sanitize(d: dict, model_cls: type) -> dict:
        """Keep only fields known to the Pydantic model, fix types."""
        known_fields = set(model_cls.model_fields.keys())
        clean: dict = {}
        for k, v in d.items():
            if k not in known_fields:
                # Map bug-factory field names → doctor field names
                if k == "operation_name" and "name" in known_fields:
                    k = "name"
                elif k == "start_time" and "start" in known_fields:
                    k = "start"
                elif k == "line" and "message" in known_fields:
                    k = "message"
                else:
                    continue  # drop extra fields like 'type', 'url', 'line_number'
            if v is None:
                field_info = model_cls.model_fields.get(k)
                if field_info and field_info.annotation is not None:
                    origin = getattr(field_info.annotation, "__origin__", None)
                    if origin is list:
                        clean[k] = []
                    else:
                        clean[k] = ""
                else:
                    clean[k] = ""
            elif isinstance(v, list):
                clean[k] = [item if isinstance(item, dict) else {"raw": str(item)} for item in v]
            else:
                clean[k] = v
        return clean

    log_entries = [LogEntry(**_sanitize(log, LogEntry)) for log in data["logs"]]
    trace_spans = [TraceSpan(**_sanitize(span, TraceSpan)) for span in data["traces"]]
    browser_errs = [BrowserError(**_sanitize(err, BrowserError)) for err in data["browser_errors"]]

    evidence = Evidence(
        user_report=data["user_report"],
        logs=log_entries,
        traces=trace_spans,
        browser_errors=browser_errs,
    )

    state = DoctorState(raw_evidence=evidence, case_id=case_id)
    state_dict = {
        "raw_evidence": evidence,
        "case_id": case_id,
    }
    print(f"  LogEntry count: {len(log_entries)}")
    print(f"  TraceSpan count: {len(trace_spans)}")
    print(f"  BrowserError count: {len(browser_errs)}")
    print(f"  thread_id: {thread_id}")

    # ── Step 3: Run graph (or just ingest) ────────────────────────
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}

    if skip_llm:
        # Ingest-only mode (no LLM keys needed)
        print_section("Step 3: Ingest only (skip_llm=True)")
        from src.graph.nodes.ingest import ingest_node

        ingest_result = await ingest_node(state)
        normalized = ingest_result["evidence"]

        print(f"\n  golden_signals ({len(normalized.golden_signals)}):")
        for sig in normalized.golden_signals:
            tier_emoji = "🖥" if sig.service_tier == "frontend" else "🖧"
            sev = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(sig.severity, "•")
            print(f"    {sev} [{tier_emoji}] [{sig.signal_type}] id={sig.signal_id}")
            print(f"       summary: {sig.summary[:160]}")
            print(f"       evidence_ref: {sig.evidence_ref}")
            # Show key metadata
            meta_keys = list(sig.metadata.keys())
            if meta_keys:
                print(f"       metadata keys: {meta_keys[:5]}")
                if "stack" in sig.metadata and sig.metadata["stack"]:
                    stack = sig.metadata["stack"]
                    idx = stack.find("TaskBoard") if "TaskBoard" in stack else stack.find("at ")
                    if idx >= 0:
                        print(f"       stack excerpt: {stack[max(0, idx - 30) : idx + 60]}")
                if "service" in sig.metadata:
                    print(f"       service: {sig.metadata['service']}")
                if "span_name" in sig.metadata:
                    print(f"       span_name: {sig.metadata['span_name']}")
                if "level" in sig.metadata:
                    print(f"       level: {sig.metadata['level']}")

        print(f"\n  correlations ({len(normalized.correlations)}):")
        if not normalized.correlations:
            # Diagnose why no correlations found
            print("    ⚠️  No correlations! Checking trace_id coverage...")
            fe_sig_ids = [
                s.signal_id for s in normalized.golden_signals if s.service_tier == "frontend"
            ]
            be_sig_ids = [
                s.signal_id for s in normalized.golden_signals if s.service_tier == "backend"
            ]
            print(f"    frontend signal IDs: {fe_sig_ids}")
            print(f"    backend signal IDs:  {be_sig_ids}")
            # Check raw_refs for trace_id stats
            counts = normalized.raw_refs.get("counts", {})
            print(
                f"    raw_logs: {counts.get('raw_logs', '?')}, denoised: {counts.get('denoised_logs', '?')}"
            )
            # Quick check: how many raw traces have trace_id?
            trace_ids_in_spans = set()
            for span in data["traces"]:
                tid = span.get("trace_id", "")
                if tid:
                    trace_ids_in_spans.add(tid)
            print(f"    unique trace_ids in raw traces: {len(trace_ids_in_spans)}")
        for c in normalized.correlations:
            print(f"    [{c.correlation_id}] trace={c.trace_id} conf={c.confidence:.2f}")
            print(f"      frontend_signals: {c.frontend_signals}")
            print(f"      backend_signals:  {c.backend_signals}")
            print(f"      db_signals:       {c.db_signals}")

        print(f"\n  timeline ({len(normalized.timeline)} events, first 10):")
        for t in normalized.timeline[:10]:
            print(f"    [{t.service_tier}] [{t.source}] trace_id={t.trace_id}")
            print(f"       {t.description[:130]}")

        print(f"\n  noise_ratio: {normalized.noise_ratio:.2%}")
        print(f"  frontend_span_count: {normalized.frontend_span_count}")
        print(f"  backend_span_count:  {normalized.backend_span_count}")

        # ── Quick acceptance checks ─────────────────────────────
        print_section("Acceptance checks (FE-020 spec)")
        fe_errors = [
            s
            for s in normalized.golden_signals
            if s.service_tier == "frontend" and s.severity == "error"
        ]
        print(f"  ✅ 前端client_error信号: {len(fe_errors)} 条")
        for s in fe_errors:
            # Check both stack metadata and summary text for TaskBoard
            stack = s.metadata.get("stack", "")
            msg = s.summary + stack
            if "TaskBoard" in msg:
                idx = msg.find("TaskBoard")
                print(
                    f"     → found 'TaskBoardPage.tsx' in signal ✅ ({msg[max(0, idx) : idx + 60]})"
                )
            elif "SortableTaskCard" in msg:
                idx = msg.find("SortableTaskCard")
                print(f"     → found 'SortableTaskCard' ✅ ({msg[max(0, idx) : idx + 60]})")
            else:
                print("     → no TaskBoardPage/SortableTaskCard in signal ⚠️")

        be_signals = [s for s in normalized.golden_signals if s.service_tier == "backend"]
        print(f"  ✅ 后端信号: {len(be_signals)} 条")
        for s in be_signals:
            print(f"     [{s.signal_type}] {s.summary[:120]}")

        cross_layer = [
            c for c in normalized.correlations if c.frontend_signals and c.backend_signals
        ]
        print(f"  ✅ 跨层correlation(fe+be): {len(cross_layer)} 条")
        for c in cross_layer:
            fe_sig_ids = {s.signal_id for s in fe_errors}
            if set(c.frontend_signals) & fe_sig_ids:
                print("     → 前端error信号在correlation中 ✅")
            else:
                print("     → 前端error信号未入correlation ⚠️")

        print(
            f"  ✅ 噪声比例: {normalized.noise_ratio:.2%} "
            + f"{'(noise stripped)' if normalized.noise_ratio > 0 else '(info: only 28 logs, likely few /health calls)'}"
        )

        fe_timeline = [t for t in normalized.timeline if t.service_tier == "frontend"]
        print(f"  ✅ 前端面包屑保留: {len(fe_timeline)} timeline events (frontend)")
    else:
        # Full graph run (ingest → triage → reporter)
        print_section("Step 3: Running full graph (ingest → triage → reporter)")
        print(f"  LLM model: {settings.llm_model} @ {settings.llm_base_url}")
        print()

        result = await graph.ainvoke(state_dict, config)

        print_section("RESULTS")

        # Ingest output
        normalized = result.get("evidence")
        if normalized:
            print(f"\n[Ingest] golden_signals: {len(normalized.golden_signals)}")
            print(f"[Ingest] correlations: {len(normalized.correlations)}")
            print(f"[Ingest] noise_ratio: {normalized.noise_ratio:.2%}")

        # Triage output
        triage = result.get("triage")
        if triage:
            print(f"\n[Triage] primary: {triage.primary}")
            print(f"[Triage] cross_layer_suspected: {triage.cross_layer_suspected}")
            print("[Triage] scores:")
            for s in triage.scores:
                bar = "█" * int(s.confidence * 20)
                print(f"    {s.category:20s} {s.confidence:.2f} {bar}")
            print(f"[Triage] reasoning: {triage.reasoning[:300]}")

        # Reporter output
        report = result.get("report")
        if report:
            print(f"\n[Report] primary_category: {report.primary_category}")
            print(f"[Report] categories: {report.categories}")
            print(f"[Report] root_cause: {report.root_cause[:300]}")
            print(f"[Report] fix_suggestion: {report.fix_suggestion[:300]}")
            print(f"[Report] affected_file: {report.affected_file}")
            print(f"[Report] confidence: {report.confidence}")
            print(f"[Report] symptom_tier: {report.symptom_tier}")
            print(f"[Report] root_cause_tier: {report.root_cause_tier}")
            print(f"[Report] early_stopped: {report.early_stopped}")

        # Findings
        findings = result.get("findings", [])
        print(f"\n[Findings] count: {len(findings)}")
        for i, f in enumerate(findings):
            print(f"  [{i}] agent={f.agent}, confidence={f.confidence:.2f}")
            print(f"      summary: {f.summary[:200]}")
            print(f"      affected_files: {f.affected_files}")
            print(f"      evidence_refs: {f.evidence_refs}")


# ── CLI entry ────────────────────────────────────────────────────────


if __name__ == "__main__":
    import asyncio

    case_id = sys.argv[1] if len(sys.argv) > 1 else "FE-020"
    skip_llm = "--no-llm" in sys.argv or "--ingest-only" in sys.argv

    print(f"╔{'═' * 58}╗")
    print(f"║  DiagDoctor Debug Runner — case: {case_id:<30s} ║")
    print(f"╚{'═' * 58}╝")
    if skip_llm:
        print("[MODE] Ingest-only (no LLM required)")

    asyncio.run(run_case(case_id, skip_llm=skip_llm))
