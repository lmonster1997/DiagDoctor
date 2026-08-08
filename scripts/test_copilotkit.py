"""CopilotKit path test: generate chat message + optional auto-score.

Usage:
    # Generate message only (copy to frontend)
    & ".venv\\Scripts\\python.exe" scripts/test_copilotkit.py BE-020

    # Generate + auto-diagnose + Langfuse score
    & ".venv\\Scripts\\python.exe" scripts/test_copilotkit.py BE-020 --score

    # P1 two-round (condition-dependent bugs): also print the second-round
    # clarify answer to paste when the agent asks
    & ".venv\\Scripts\\python.exe" scripts/test_copilotkit.py BE-022 --clarify

    # P1 two-round, fully auto-driven via REST: inject -> round1 -> detect
    # clarify_input -> resume with clarify_answer -> verify must_mention
    & ".venv\\Scripts\\python.exe" scripts/test_copilotkit.py BE-022 --two-round

Two-round records (user_report + clarify_answer + expected) live in
scripts/two_round_cases.yaml. Add entries there for new condition-dependent bugs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DOCTOR_BACKEND = PROJECT_ROOT / "doctor" / "backend"
BUG_FACTORY_DIR = PROJECT_ROOT / "bug-factory"
sys.path.insert(0, str(DOCTOR_BACKEND))

PYTHON = sys.executable
DEMO_URL = "http://localhost:8000"
RELOAD_WAIT = 5


def inject(rid: str) -> None:
    subprocess.run([PYTHON, "-m", "bug_factory.cli", "inject", rid, "--in-place"], check=True)


def trigger(rid: str) -> tuple[str, list[str]]:
    r = subprocess.run(
        [PYTHON, "-m", "bug_factory.cli", "trigger", rid, "--base-url", DEMO_URL],
        cwd=str(BUG_FACTORY_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tids = []
    for line in r.stdout.splitlines():
        if line.strip().startswith("TRACE_IDS_JSON="):
            tids = list(json.loads(line.strip()[len("TRACE_IDS_JSON=") :]).get("trace_ids", []))
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(), tids


def load_report(rid: str, fb: str = "") -> str:
    import yaml

    p = BUG_FACTORY_DIR / "output" / rid / "case.yaml"
    return (
        yaml.safe_load(p.read_text(encoding="utf-8")).get("input", {}).get("user_report", "") or fb
        if p.exists()
        else fb
    )


TWO_ROUND_FILE = SCRIPT_DIR / "two_round_cases.yaml"


def load_two_round(rid: str) -> dict:
    """Load a P1 two-round case record by recipe id (see two_round_cases.yaml).

    Returns the record dict (user_report / clarify_answer / expected_* /
    must_mention) or {} if the registry or the rid is absent -- caller falls
    back to single-round behavior.
    """
    import yaml

    if not TWO_ROUND_FILE.exists():
        return {}
    data = yaml.safe_load(TWO_ROUND_FILE.read_text(encoding="utf-8")) or {}
    return data.get(rid) or {}


def make_msg(ur: str, tt: str = "") -> str:
    return f"{ur}\n\n(trigger_time: {tt})" if tt else ur


async def do_score(rid: str, ur: str, tt: str) -> None:
    from langfuse import Langfuse
    from src.config import settings
    from src.graph.copilotkit_graph import get_copilotkit_graph
    from langfuse_scorers import score_all_dimensions, score_process_quality

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )
    msg = make_msg(ur, tt)
    tid = f"cktest-{__import__('uuid').uuid4().hex[:8]}"
    trace = lf.trace(name=f"cktest_{rid}", tags=["copilotkit"], metadata={"recipe_id": rid})

    print("\n  Diagnosing via CopilotKit (free-text -> bug_info -> diag_agent) ...")
    graph = get_copilotkit_graph()
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": msg}]}, {"configurable": {"thread_id": tid}}
    )
    lf.flush()

    report = result.get("report")
    r = report.model_dump() if report and hasattr(report, "model_dump") else {}
    print(
        f"  categories={r.get('categories')}, file={r.get('affected_file')}, conf={r.get('confidence', 0):.0%}"
    )

    import yaml

    exp = {}
    cp = BUG_FACTORY_DIR / "output" / rid / "case.yaml"
    if cp.exists():
        raw = yaml.safe_load(cp.read_text(encoding="utf-8")).get("expected", {})
        # 字段映射：case.yaml 用 affected_files (list)，scorer 用 affected_file (str)
        exp = dict(raw)
        if "affected_files" in exp and "affected_file" not in exp:
            files = exp["affected_files"]
            exp["affected_file"] = files[0] if isinstance(files, list) and files else str(files)
        if "category" in exp and "categories" not in exp:
            exp["categories"] = (
                [exp["category"]] if isinstance(exp["category"], str) else exp["category"]
            )
        if "primary_category" not in exp and "category" in exp:
            exp["primary_category"] = (
                exp["category"]
                if isinstance(exp["category"], str)
                else (exp["category"][0] if exp["category"] else "")
            )

    # 对齐 scorer 期望：顶层字段 + report 嵌套
    diag = {
        **r,
        "report": r,
        "categories": r.get("categories", []),
        "confidence": r.get("confidence", 0),
    }
    scores = await score_all_dimensions(lf, trace.id, exp, diag, skip_llm_judge=False)
    await asyncio.sleep(1)
    pq = score_process_quality(lf, trace.id)

    print(
        f"  overall={scores.get('overall', 0):.2f} (rc={scores.get('root_cause_accuracy', 0):.2f} cat={scores.get('category_accuracy', 0):.2f} file={scores.get('affected_file_accuracy', 0):.2f} fix={scores.get('fix_suggestion_quality', 0):.2f}) pq={pq:.2f}"
    )


async def do_two_round(rid: str, ur: str, tt: str, case: dict) -> None:
    """Auto-drive the P1 active-clarification two-round flow via the REST API.

    inject+trigger already ran. Round 1: POST /api/diagnose with the vague
    user_report and NO trace_id -- the agent should proactively call
    ``request_user_clarification`` and pause at ``clarify_input``. If it does,
    resume with the recorded ``clarify_answer`` (the reproduction condition
    tools can't fetch), then verify the final report's root_cause against
    ``must_mention``. If the agent did NOT ask (converged or hit budget-HITL),
    report that and show the first-round result.
    """
    import httpx

    DOCTOR = "http://127.0.0.1:8001"
    async with httpx.AsyncClient(timeout=300.0) as c:
        evidence = {"user_report": ur}
        if tt:
            evidence["trigger_time"] = tt
        print("\n  [round 1] POST /api/diagnose (vague user_report, no trace_id) ...")
        r = await c.post(f"{DOCTOR}/api/diagnose", json={"evidence": evidence})
        r.raise_for_status()
        body = r.json()
        tid = body["thread_id"]

        # Did the agent pause at clarify_input (proactive ask) or reach END?
        s = await c.get(f"{DOCTOR}/api/diagnose/threads?limit=5")
        threads = s.json().get("threads", [])
        th = next((t for t in threads if t.get("thread_id") == tid), {})
        nxt = th.get("next", [])
        print(f"  thread={tid} status={th.get('status')} next={nxt}")

        if "clarify_input" in nxt:
            print("  [P1] agent proactively asked -> resume with clarify_answer ...")
            r2 = await c.post(
                f"{DOCTOR}/api/diagnose/resume",
                json={"thread_id": tid, "guidance": case["clarify_answer"]},
            )
            r2.raise_for_status()
            report = r2.json().get("report") or {}
        else:
            print("  [!] agent did NOT ask (converged, or budget-HITL @ human_input).")
            report = body.get("report") or {}

        rc = report.get("root_cause", "") or ""
        print(f"\n  root_cause : {rc}")
        print(
            f"  affected   : {report.get('affected_file')} / {report.get('affected_function')}"
            f"  conf={report.get('confidence')}"
        )
        must = case.get("must_mention", []) or []
        hit = [k for k in must if k.lower() in rc.lower()]
        ok = bool(must) and len(hit) == len(must)
        print(f"  must_mention: {hit}/{must}  {'PASS' if ok else 'FAIL'}")
        print(f"  expected   : {case.get('expected_root_cause', '')}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="CopilotKit path test: inject bug + generate chat message (+ P1 two-round)."
    )
    p.add_argument("rid", nargs="?")
    p.add_argument("--skip-inject", action="store_true")
    p.add_argument("--skip-trigger", action="store_true")
    p.add_argument("--msg", type=str, default="")
    p.add_argument("--score", action="store_true")
    p.add_argument(
        "--clarify",
        action="store_true",
        help="also print the second-round clarify answer (P1 two-round cases; needs an entry in two_round_cases.yaml)",
    )
    p.add_argument(
        "--two-round",
        action="store_true",
        help="auto-drive P1 two-round: inject->diagnose(round1)->detect clarify_input->resume(round2)->verify must_mention",
    )
    args = p.parse_args()

    two_round = load_two_round(args.rid) if args.rid else {}

    # First-round user_report precedence: --msg > registry user_report > case.yaml > prompt
    ur = (
        args.msg
        or (two_round.get("user_report") if two_round else "")
        or (load_report(args.rid) if args.rid else "")
    )
    ur = ur or input("Bug description: ").strip()
    if not ur:
        print("No input.")
        return

    tt = ""
    if args.rid and not args.skip_inject:
        print(f"[inject] {args.rid}")
        inject(args.rid)
        time.sleep(RELOAD_WAIT)
    if args.rid and not args.skip_trigger:
        print(f"[trigger] {args.rid}")
        tt, _ = trigger(args.rid)
        time.sleep(3)

    if args.two_round:
        if not two_round:
            print(
                f"[!] no two_round_cases.yaml record for {args.rid!r}; --two-round needs a "
                "clarify_answer. Falling back to single-round print."
            )
        else:
            asyncio.run(do_two_round(args.rid or "manual", ur, tt, two_round))
            return

    if args.score:
        asyncio.run(do_score(args.rid or "manual", ur, tt))
    else:
        msg = make_msg(ur, tt)
        print(f"\n{'=' * 60}\n  Copy to CopilotKit frontend:\n{'=' * 60}\n{msg}\n{'=' * 60}")
        if args.clarify:
            if two_round:
                print(
                    f"\n  [round 2] when the agent asks, reply:\n{'-' * 60}\n"
                    f"  {two_round['clarify_answer']}\n{'-' * 60}"
                )
                print(f"  expected root_cause: {two_round.get('expected_root_cause', '')}")
                print(f"  must_mention: {two_round.get('must_mention', [])}")
            else:
                print(
                    f"\n  (no two-round record for {args.rid!r}; add one to "
                    "scripts/two_round_cases.yaml to use --clarify)"
                )


if __name__ == "__main__":
    main()
