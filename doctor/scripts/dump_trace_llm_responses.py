"""拉一个 trace 的所有 LLM GENERATION observation 内容（截断展示）。

用于诊断"agent 是否产出有效 JSON"——看 S1 forced call 的 LLM 响应
到底是 JSON 还是 narrative 文本。

Usage:
    uv run python scripts/dump_trace_llm_responses.py --session <session_id> --bug <bug_id>
    uv run python scripts/dump_trace_llm_responses.py --session smoke-after-SimplifiedConvergenceStrategy --bug FE-020
"""
from __future__ import annotations

import argparse
import os
import sys

from langfuse import Langfuse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import settings  # noqa: E402


def _fetch_session_traces(lf: Langfuse, session_id: str) -> list:
    traces: list = []
    page = 1
    while True:
        resp = lf.fetch_traces(session_id=session_id, page=page, limit=100)
        batch = getattr(resp, "data", []) or []
        traces.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return traces


def _bug_id(trace) -> str:
    md = trace.metadata or {}
    return md.get("bug_id") or md.get("recipe_id") or trace.name.split("_")[-1]


def _walk_observations(obs_list, depth=0, out=None):
    if out is None:
        out = []
    for obs in obs_list:
        out.append((depth, obs))
        children = getattr(obs, "children", None) or []
        if children:
            _walk_observations(children, depth + 1, out)
    return out


def _extract_llm_content(obs) -> tuple[str, str]:
    """从 GENERATION observation 提取 (input_messages_summary, output_content)。"""
    inp = getattr(obs, "input", None) or {}
    outp = getattr(obs, "output", None) or {}

    # output 通常是 {"content": "..."} 或 AIMessage 的 dict
    out_content = ""
    if isinstance(outp, dict):
        out_content = str(outp.get("content", "") or "")
        if not out_content:
            # 有些版本是 {"generations": [[{"text": "..."}]]}
            gens = outp.get("generations") or []
            if gens and isinstance(gens, list) and isinstance(gens[0], list):
                out_content = str(gens[0][0].get("text", "") or "")

    # input summary：抓最后一条消息的 content 摘要
    in_summary = ""
    if isinstance(inp, dict):
        msgs = inp.get("messages") or []
        if msgs and isinstance(msgs, list):
            last = msgs[-1]
            if isinstance(last, dict):
                role = last.get("role", "?")
                content = str(last.get("content", ""))
                in_summary = f"[{role}] {content[:300]}"
            else:
                in_summary = str(last)[:300]

    return in_summary, out_content


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--bug", required=True)
    ap.add_argument(
        "--show-input",
        action="store_true",
        help="也显示每次 LLM 调用的最后一条 input message（看 nudge 是否被注入）",
    )
    ap.add_argument(
        "--last-content-len",
        type=int,
        default=2000,
        help="最后一次 LLM 调用的输出展示字符数（默认 2000）",
    )
    args = ap.parse_args()

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    traces = _fetch_session_traces(lf, args.session)
    trace = None
    for t in traces:
        if _bug_id(t) == args.bug:
            trace = lf.fetch_trace(t.id).data
            break
    if trace is None:
        print(f"no trace for bug={args.bug} in session={args.session}")
        return 1

    obs = getattr(trace, "observations", None) or []
    flat = _walk_observations(obs)

    # 过滤出 GENERATION 类型（LLM 调用）
    llm_calls = []
    for depth, o in flat:
        t = getattr(o, "type", "?") or "?"
        if t == "GENERATION":
            llm_calls.append((depth, o))

    print(f"# Trace: {trace.name}\n# id: {trace.id}")
    print(f"# total observations: {len(flat)}")
    print(f"# GENERATION (LLM call) count: {len(llm_calls)}\n")

    for i, (depth, o) in enumerate(llm_calls, 1):
        nm = getattr(o, "name", "") or ""
        in_sum, out_content = _extract_llm_content(o)
        is_last = i == len(llm_calls)
        print(f"--- LLM call #{i}  (name={nm})  {'[LAST]' if is_last else ''} ---")
        if args.show_input and in_sum:
            print(f"  INPUT (last msg): {in_sum}{'...' if len(in_sum) >= 300 else ''}")
        # 最后一次完整展示，其他截断 300 字
        show_len = args.last_content_len if is_last else 300
        preview = out_content[:show_len]
        if len(out_content) > show_len:
            preview = preview + f"\n... [truncated, total {len(out_content)} chars]"
        print(f"  OUTPUT ({len(out_content)} chars):")
        print(preview)
        print()

    # ── 额外：检查最后一次 LLM 输出能否解析为 JSON ──
    if llm_calls:
        last_out = _extract_llm_content(llm_calls[-1][1])[1]
        print("=" * 60)
        print("# JSON parsability check on LAST LLM output:")
        # 复用 unified_agent 的 JSON 提取器
        try:
            from src.graph.nodes.unified_agent import _extract_json_from_text
            data = _extract_json_from_text(last_out)
            if data is None:
                print("  ❌ _extract_json_from_text returned None")
            else:
                print(f"  ✅ parsed JSON, keys: {list(data.keys())}")
                if "primary_category" in data:
                    print(f"     primary_category = {data.get('primary_category')!r}")
                if "explained_signals" in data:
                    print(f"     explained_signals = {data.get('explained_signals')!r}")
                if "confidence" in data:
                    print(f"     confidence = {data.get('confidence')!r}")
        except Exception as e:
            print(f"  ⚠️  extractor error: {e}")

        # 检测 tool_calls 字段
        if '"tool_calls"' in last_out or "'tool_calls'" in last_out:
            print("  ⚠️  输出含 tool_calls 字段（LLM 仍想调工具，可能没交付 JSON）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
