"""CopilotKit path test: generate chat message + optional auto-score.

Usage:
    # Generate message only (copy to frontend)
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py BE-020

    # Generate + auto-diagnose + Langfuse score
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py BE-020 --score
"""

from __future__ import annotations

import argparse, asyncio, json, subprocess, sys, time
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
    r = subprocess.run([PYTHON, "-m", "bug_factory.cli", "trigger", rid, "--base-url", DEMO_URL],
        cwd=str(BUG_FACTORY_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace")
    tids = []
    for line in r.stdout.splitlines():
        if line.strip().startswith("TRACE_IDS_JSON="):
            tids = list(json.loads(line.strip()[len("TRACE_IDS_JSON="):]).get("trace_ids", []))
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(), tids

def load_report(rid: str, fb: str = "") -> str:
    import yaml
    p = BUG_FACTORY_DIR / "output" / rid / "case.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")).get("input", {}).get("user_report", "") or fb if p.exists() else fb

def make_msg(ur: str, tt: str = "") -> str:
    return f"{ur}\n\n(trigger_time: {tt})" if tt else ur


async def do_score(rid: str, ur: str, tt: str) -> None:
    from langfuse import Langfuse
    from src.config import settings
    from src.graph.copilotkit_graph import get_copilotkit_graph
    from langfuse_scorers import score_all_dimensions, score_process_quality

    lf = Langfuse(secret_key=settings.langfuse_secret_key, public_key=settings.langfuse_public_key, host=settings.langfuse_host)
    msg = make_msg(ur, tt)
    tid = f"cktest-{__import__('uuid').uuid4().hex[:8]}"
    trace = lf.trace(name=f"cktest_{rid}", tags=["copilotkit"], metadata={"recipe_id": rid})

    print("\n  Diagnosing via CopilotKit (free-text -> bug_info -> diag_agent) ...")
    graph = get_copilotkit_graph()
    result = await graph.ainvoke({"messages": [{"role": "user", "content": msg}]}, {"configurable": {"thread_id": tid}})
    lf.flush()

    report = result.get("report")
    r = report.model_dump() if report and hasattr(report, "model_dump") else {}
    print(f"  categories={r.get('categories')}, file={r.get('affected_file')}, conf={r.get('confidence',0):.0%}")

    import yaml
    exp = {}
    cp = BUG_FACTORY_DIR / "output" / rid / "case.yaml"
    if cp.exists():
        exp = yaml.safe_load(cp.read_text(encoding="utf-8")).get("expected", {})

    # 对齐 scorer 期望：顶层字段 + report 嵌套
    diag = {**r, "report": r, "categories": r.get("categories", []), "confidence": r.get("confidence", 0)}
    scores = await score_all_dimensions(lf, trace.id, exp, diag, skip_llm_judge=False)
    await asyncio.sleep(1)
    pq = score_process_quality(lf, trace.id)

    print(f"  overall={scores.get('overall',0):.2f} (rc={scores.get('root_cause_accuracy',0):.2f} cat={scores.get('category_accuracy',0):.2f} file={scores.get('affected_file_accuracy',0):.2f} fix={scores.get('fix_suggestion_quality',0):.2f}) pq={pq:.2f}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("rid", nargs="?")
    p.add_argument("--skip-inject", action="store_true")
    p.add_argument("--skip-trigger", action="store_true")
    p.add_argument("--msg", type=str, default="")
    p.add_argument("--score", action="store_true")
    args = p.parse_args()

    ur = args.msg or (load_report(args.rid) if args.rid else "")
    ur = ur or input("Bug description: ").strip()
    if not ur: print("No input."); return

    tt = ""
    if args.rid and not args.skip_inject:
        print(f"[inject] {args.rid}"); inject(args.rid); time.sleep(RELOAD_WAIT)
    if args.rid and not args.skip_trigger:
        print(f"[trigger] {args.rid}"); tt, _ = trigger(args.rid); time.sleep(3)

    if args.score:
        asyncio.run(do_score(args.rid or "manual", ur, tt))
    else:
        msg = make_msg(ur, tt)
        print(f"\n{'='*60}\n  Copy to CopilotKit frontend:\n{'='*60}\n{msg}\n{'='*60}")

if __name__ == "__main__":
    main()
