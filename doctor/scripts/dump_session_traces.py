"""排查一个 session 里到底有几个 trace、每个 trace 有几个 observation。

直接用 Langfuse REST API（避开 Python SDK 的 get_traces 分页问题）。

用法：
    cd doctor && uv run python scripts/dump_session_traces.py baseline-15case
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.config import settings  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/dump_session_traces.py <session_id>")
        sys.exit(1)
    session_id = sys.argv[1]

    base = settings.langfuse_host.rstrip("/")
    auth = (settings.langfuse_public_key, settings.langfuse_secret_key)

    # Langfuse v3 API: GET /api/public/traces，支持 session_id 过滤
    all_traces = []
    page = 1
    while True:
        resp = requests.get(
            f"{base}/api/public/traces",
            auth=auth,
            params={"session_id": session_id, "page": page, "limit": 100},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("data", [])
        if not batch:
            break
        all_traces.extend(batch)
        # 检查是否还有下一页
        meta = data.get("meta", {})
        total_pages = meta.get("totalPages", 1)
        if page >= total_pages:
            break
        page += 1

    print(f"\nSession: {session_id}")
    print(f"Total traces: {len(all_traces)}\n")
    print(
        f"{'name':<40} {'obs':<5} {'input':<6} {'output':<7} {'scores':<25}"
    )
    print("-" * 90)

    pairs_by_recipe: dict[str, list] = {}
    for t in sorted(all_traces, key=lambda x: x.get("name", "") or ""):
        name = t.get("name") or "<no name>"
        obs = t.get("observations", []) or []
        scores = t.get("scores", []) or []
        # scores 可能是 list[str]（ID 列表）或 list[dict]
        score_names = []
        for s in scores:
            if isinstance(s, dict):
                score_names.append(s.get("name", ""))
            elif isinstance(s, str):
                score_names.append(s)  # 就是个 ID
        has_input = "y" if t.get("input") else "-"
        has_output = "y" if t.get("output") else "-"
        score_names_str = ",".join(score_names)
        print(
            f"{name[:40]:<40} {len(obs):<5} {has_input:<6} {has_output:<7} {score_names_str[:25]:<25}"
        )

        # 按 recipe_id 归组（name 形如 baseline-15case_BE-020）
        for part in name.split("_"):
            if "-" in part and part[0:2] in {"BE", "FE", "PE", "LO", "DA", "CO", "CA", "RA"}:
                pairs_by_recipe.setdefault(part, []).append(
                    {
                        "name": name,
                        "obs_count": len(obs),
                        "has_output": bool(t.get("output")),
                        "score_names": score_names_str,
                    }
                )
                break

    print(f"\n=== 按 recipe 归组（看是否每个 case 出现 2 个 trace）===")
    for recipe, lst in sorted(pairs_by_recipe.items()):
        marker = "  ← 双 trace!" if len(lst) > 1 else ""
        print(f"  {recipe}: {len(lst)} trace(s){marker}")
        for item in lst:
            print(
                f"    - name={item['name']}, obs={item['obs_count']}, "
                f"output={'y' if item['has_output'] else '-'}, scores={item['score_names']}"
            )


if __name__ == "__main__":
    main()
