"""事后打分：给已有的 Langfuse trace 加上评分。

用法:
    & ".venv\Scripts\python.exe" scripts/score_trace.py <trace_id> <recipe_id>

示例:
    & ".venv\Scripts\python.exe" scripts/score_trace.py 0e73e982-287f-4b6b-bce1-0d8d67fa8e5b BE-020
"""

from __future__ import annotations

import asyncio, sys, yaml
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DOCTOR_BACKEND = PROJECT_ROOT / "doctor" / "backend"
BUG_FACTORY_DIR = PROJECT_ROOT / "bug-factory"
sys.path.insert(0, str(DOCTOR_BACKEND))


def load_expected(recipe_id: str) -> dict:
    """从 case.yaml 加载 expected_output，映射字段名以匹配 scorer。"""
    cp = BUG_FACTORY_DIR / "output" / recipe_id / "case.yaml"
    if not cp.exists():
        print(f"[WARN] case.yaml not found: {cp}")
        return {}
    raw = yaml.safe_load(cp.read_text(encoding="utf-8")).get("expected", {})
    exp = dict(raw)
    # case.yaml 用 affected_files (list)，scorer 用 affected_file (str)
    if "affected_files" in exp and "affected_file" not in exp:
        files = exp["affected_files"]
        exp["affected_file"] = files[0] if isinstance(files, list) and files else str(files)
    if "category" in exp and "categories" not in exp:
        exp["categories"] = [exp["category"]] if isinstance(exp["category"], str) else exp["category"]
    if "primary_category" not in exp and "category" in exp:
        exp["primary_category"] = exp["category"] if isinstance(exp["category"], str) else (exp["category"][0] if exp["category"] else "")
    return exp


async def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python scripts/score_trace.py <trace_id> <recipe_id>")
        sys.exit(1)

    trace_id = sys.argv[1]
    recipe_id = sys.argv[2]

    from langfuse import Langfuse
    from src.config import settings
    from langfuse_scorers import score_all_dimensions, score_process_quality

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    trace = lf.get_trace(trace_id)
    if not trace:
        print(f"[FAIL] Trace not found: {trace_id}")
        return

    # 从 trace output 提取诊断
    output = trace.output or {}
    r = output.get("diagnosis_report", output.get("report", {}))
    if isinstance(r, dict):
        pass  # already a dict
    elif hasattr(r, "model_dump"):
        r = r.model_dump()
    else:
        r = {"categories": [], "affected_file": None, "root_cause": str(r)}

    diag = {**r, "report": r, "categories": r.get("categories", []), "confidence": r.get("confidence", 0)}

    print(f"Trace: {trace_id}")
    print(f"Recipe: {recipe_id}")
    print(f"categories={diag.get('categories')}, file={diag.get('affected_file')}, conf={diag.get('confidence', 0):.0%}")

    exp = load_expected(recipe_id)

    scores = await score_all_dimensions(lf, trace_id, exp, diag, skip_llm_judge=False)
    await asyncio.sleep(1)
    pq = score_process_quality(lf, trace_id)

    print(f"\noverall={scores.get('overall', 0):.2f}  "
          f"(rc={scores.get('root_cause_accuracy', 0):.2f} "
          f"cat={scores.get('category_accuracy', 0):.2f} "
          f"file={scores.get('affected_file_accuracy', 0):.2f} "
          f"fix={scores.get('fix_suggestion_quality', 0):.2f})  "
          f"pq={pq:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
