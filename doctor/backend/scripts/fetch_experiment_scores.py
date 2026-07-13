"""Fetch Langfuse experiment scores for a run (by session_id = run_name).

Usage:
    cd doctor && uv run python scripts/fetch_experiment_scores.py <run_name>
    cd doctor && uv run python scripts/fetch_experiment_scores.py framework-smoke-20260708-141807
"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlencode

import requests

from src.config import settings

HOST = settings.langfuse_host.rstrip("/")
AUTH = (settings.langfuse_public_key, settings.langfuse_secret_key)


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{HOST}/api/public{path}"
    if params:
        url += "?" + urlencode(params)
    r = requests.get(url, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_traces_for_session(session_id: str) -> list[dict]:
    """Page through /traces filtered by sessionId."""
    traces: list[dict] = []
    page = 1
    while True:
        data = _get("/traces", {"sessionId": session_id, "page": page, "limit": 100})
        batch = data.get("data", [])
        traces.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if page > 20:
            break
    return traces


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: fetch_experiment_scores.py <run_name>")
        sys.exit(1)
    run_name = sys.argv[1]
    print(f"Fetching traces for session_id={run_name} from {HOST} ...")
    traces = fetch_traces_for_session(run_name)
    print(f"  found {len(traces)} traces\n")

    # Score dimensions of interest
    dims = [
        "overall",
        "root_cause_accuracy",
        "fix_suggestion_quality",
        "affected_file_accuracy",
        "affected_line_accuracy",
        "category_accuracy",
        "evidence_chain_completeness",
        "confidence_calibration",
        "process_quality",
    ]

    rows: list[dict] = []
    for t in traces:
        name = t.get("name", "")
        recipe_id = (t.get("metadata") or {}).get("recipe_id", name)
        scores_list = t.get("scores") or []
        # scores may be a list of score IDs (strings) or dicts — handle both,
        # and fetch full score objects via /api/public/scores/{id} if needed.
        score_map: dict = {}
        for s in scores_list:
            if isinstance(s, dict):
                score_map[s.get("name")] = s.get("value")
            elif isinstance(s, str):
                # score ID — fetch full object
                try:
                    sobj = _get(f"/scores/{s}")
                    score_map[sobj.get("name")] = sobj.get("value")
                except Exception as exc:
                    print(f"  [warn] failed to fetch score {s}: {exc}", file=sys.stderr)
        row = {"recipe_id": recipe_id, "trace_id": t.get("id")}
        for d in dims:
            row[d] = score_map.get(d)
        rows.append(row)

    # Sort by recipe_id
    rows.sort(key=lambda r: r["recipe_id"])

    # Print table
    header = f"{'recipe_id':<12} " + " ".join(f"{d[:14]:>14}" for d in dims)
    print(header)
    print("-" * len(header))
    for r in rows:
        line = f"{r['recipe_id']:<12} "
        for d in dims:
            v = r[d]
            line += f"{(f'{v:.2f}' if isinstance(v, (int, float)) else '-'):>14} "
        print(line)
    print()

    # Averages
    print("AVERAGES:")
    for d in dims:
        vals = [r[d] for r in rows if isinstance(r[d], (int, float))]
        if vals:
            print(f"  {d:<32} {sum(vals)/len(vals):.3f}  (n={len(vals)})")
        else:
            print(f"  {d:<32} (no scores)")

    # Dump full JSON for downstream doc writing
    out_path = os.path.join(os.path.dirname(__file__), f"_scores_{run_name}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\nFull JSON written to {out_path}")


if __name__ == "__main__":
    main()
