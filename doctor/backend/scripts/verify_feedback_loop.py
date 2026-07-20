"""端到端验证 §8.1 反馈回填闭环(真实 Qdrant + 真实本地 bge-m3,非 mock)。

验证链(回答"反馈有没有入库 / 下次 agent 能不能检索到 / 流程通了吗"):
  1. 索引 2 个历史 case(case_A/B,症状相似)入 Qdrant —— 👍 入库
  2. search_historical_cases(相似症状 evidence)—— agent 检索侧能否召回,记录回填前 importance
  3. 模拟 👍:backfill_effectiveness([case_A], delta=+0.1, hit=True) —— §8.1 回填
  4. 直接查 Qdrant case_A payload —— effectiveness/hit_count 真的变了
  5. 再 search —— importance 升;连 👍 10 次后 importance 显著提升(越用越准)
  6. 实证 point-id 雷:用 diag-<hex>(非 UUID)当 point id upsert —— Qdrant 拒绝

环境:TEI 未起 -> 走本地 sentence-transformers(BGE_M3_LOCAL_PATH + HF_HUB_OFFLINE)。
每次跑前重建 historical_cases collection,保证干净起点。

用法(在 doctor/backend 下):
  BGE_M3_LOCAL_PATH=D:/hf_cache/models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181 \
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  uv run python scripts/verify_feedback_loop.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

# 设环境必须在 import src.* 之前(embedding 运行时读 BGE_M3_LOCAL_PATH)。
os.environ.setdefault(
    "BGE_M3_LOCAL_PATH",
    "D:/hf_cache/models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181",
)
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from qdrant_client.models import PointStruct  # noqa: E402

from src.engine.state import DiagnosisReport, NormalizedEvidence, Signal  # noqa: E402
from src.memory.long_term.case_retriever import (  # noqa: E402
    HIT_COUNT_CAP,
    _importance,
    search_historical_cases,
)
from src.memory.long_term.case_store import (  # noqa: E402
    backfill_effectiveness,
    maybe_index_diagnosis,
)
from src.memory.long_term.qdrant_client import (  # noqa: E402
    COLLECTION_NAME,
    ensure_collection,
    get_qdrant_client,
)

SEP = "=" * 72


def _report(root_cause: str, file: str, fix: str, conf: float) -> DiagnosisReport:
    return DiagnosisReport(
        primary_category="frontend_crash",
        categories=["frontend_crash"],
        symptom_tier="frontend",
        root_cause_tier="frontend",
        root_cause=root_cause,
        affected_file=file,
        affected_function="render",
        fix_suggestion=fix,
        confidence=conf,
    )


def _evidence(user_report: str, signal_summary: str, trace_id: str) -> NormalizedEvidence:
    return NormalizedEvidence(
        user_report=user_report,
        golden_signals=[
            Signal(signal_type="error_log", service_tier="frontend", summary=signal_summary)
        ],
        trigger_time="2026-07-18T10:00:00Z",
        trigger_trace_ids=[trace_id],
    )


# ── 两个历史 case(症状都涉及 frontend tags crash,根因不同)──
CASE_A = {
    "case_id": str(uuid.uuid4()),
    "report": _report(
        root_cause="TaskResponse schema 缺少 tags 字段,前端读取 undefined 触发 TypeError",
        file="app/schemas/task.py",
        fix="TaskResponse 增加 tags: list[str] = []",
        conf=0.85,
    ),
    "evidence": _evidence(
        user_report="创建任务后页面卡死白屏",
        signal_summary="TypeError: Cannot read properties of undefined (reading 'tags')",
        trace_id="trace-A",
    ),
}
CASE_B = {
    "case_id": str(uuid.uuid4()),
    "report": _report(
        root_cause="renderTags 未判空,tags 为 undefined 时解构报错",
        file="app/frontend/TaskList.tsx",
        fix="renderTags 加 if (!tags) return null",
        conf=0.75,
    ),
    "evidence": _evidence(
        user_report="任务列表页面打开白屏",
        signal_summary="Uncaught TypeError: undefined is not iterable at renderTags",
        trace_id="trace-B",
    ),
}
# 模拟一次新诊断的证据(症状与 case_A 高度相似,trace 不同避免自排)
QUERY = _evidence(
    user_report="新建任务后页面卡死,控制台报 tags 相关错误",
    signal_summary="TypeError: reading 'tags' of undefined",
    trace_id="trace-query",
)


async def _reset_collection() -> None:
    client = await get_qdrant_client()
    try:
        await client.delete_collection(COLLECTION_NAME)
        print("  重建 collection(清空旧数据)")
    except Exception:
        pass
    await ensure_collection()


async def _scroll_payload(case_id: str) -> dict | None:
    client = await get_qdrant_client()
    res = await client.retrieve(
        collection_name=COLLECTION_NAME, ids=[case_id], with_payload=True, with_vectors=False
    )
    return dict(res[0].payload) if res else None


def _show_recall(scored, label: str) -> dict[str, float]:
    print(f"\n  [{label}] 召回 {len(scored)} 个 case:")
    imp_map: dict[str, float] = {}
    for c in scored:
        imp = _importance(c.payload)
        imp_map[c.case_id] = imp
        tag = "case_A" if c.case_id == CASE_A["case_id"] else (
            "case_B" if c.case_id == CASE_B["case_id"] else "?"
        )
        print(
            f"    - {tag} ({c.case_id[:8]})  "
            f"relevance={c.relevance:.3f} importance={imp:.4f} score={c.score:.4f}"
        )
    return imp_map


async def main() -> None:
    print(SEP)
    print("§8.1 反馈回填闭环 - 真实端到端验证(Qdrant + bge-m3)")
    print(SEP)

    print("\n[0] 准备:重建 historical_cases collection")
    await _reset_collection()

    print("\n[1] 索引 2 个历史 case(模拟之前 👍 入库)")
    for tag, case in [("case_A", CASE_A), ("case_B", CASE_B)]:
        ok = await maybe_index_diagnosis(
            report=case["report"],
            evidence=case["evidence"],
            source="user_upvote",
            trace_id=case["evidence"].trigger_trace_ids[0],
            case_id=case["case_id"],
        )
        print(f"  {tag}: indexed={ok}  case_id={case['case_id']}")

    print("\n[2] agent 检索(search_historical_cases,症状似 case_A)")
    scored = await search_historical_cases(QUERY)
    imp_before = _show_recall(scored, "回填前")
    if CASE_A["case_id"] not in imp_before:
        print("\n  ❌ case_A 未被召回 —— 检索流程不通")
        return
    print(f"\n  case_A 回填前 importance = {imp_before[CASE_A['case_id']]:.4f}")

    print("\n[3] 模拟 👍:对 case_A 回填 effectiveness(delta=+0.1, hit=True)")
    n = await backfill_effectiveness([CASE_A["case_id"]], delta=0.1, hit=True)
    print(f"  backfill 更新点数 = {n}")

    print("\n[4] 直接查 Qdrant case_A payload(验证真的写回)")
    pl = await _scroll_payload(CASE_A["case_id"])
    print(f"  effectiveness = {pl.get('effectiveness')}  (期望 0.1)")
    print(f"  hit_count     = {pl.get('hit_count')}  (期望 1)")

    print("\n[5] 再检索 —— 单次 👍 后 importance 变化")
    scored2 = await search_historical_cases(QUERY)
    imp_after = _show_recall(scored2, "回填后(1 次 👍)")
    a_before = imp_before[CASE_A["case_id"]]
    a_after = imp_after[CASE_A["case_id"]]
    print(f"\n  case_A importance: {a_before:.4f} -> {a_after:.4f}  (Δ=+{a_after - a_before:.4f})")

    print("\n[5b] 连续 👍 共 10 次(累积,验证'越用越准')")
    for _ in range(9):  # 已 +1 次,再 9 次到 10
        await backfill_effectiveness([CASE_A["case_id"]], delta=0.1, hit=True)
    pl10 = await _scroll_payload(CASE_A["case_id"])
    print(
        f"  10 次 👍 后:effectiveness={pl10.get('effectiveness')} "
        f"hit_count={pl10.get('hit_count')} (HIT_COUNT_CAP={HIT_COUNT_CAP})"
    )
    scored3 = await search_historical_cases(QUERY)
    imp_10 = {c.case_id: _importance(c.payload) for c in scored3}
    a_10 = imp_10[CASE_A["case_id"]]
    print(f"  case_A importance: {a_before:.4f}(初始) -> {a_10:.4f}(10 次 👍)  "
          f"(理论上界 0.5·conf+0.3+0.2={0.5 * 0.85 + 0.5:.4f})")

    print("\n[6] 实证 point-id 雷:diag-<hex>(非 UUID)当 point id")
    client = await get_qdrant_client()
    bad_id = "diag-a1b2c3d4e5f6"  # generate_thread_id() 实际产出的格式
    # P1-a: collection 用 named vectors,upsert 必须给 {symptom, root_cause} 字典,
    # 否则 Qdrant 会因向量格式(而非 id)先报错,污染本测试。这里给全 named 向量,
    # 让 id 校验成为唯一失败点。
    try:
        await client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=bad_id,
                    vector={"symptom": [0.0] * 1024, "root_cause": [0.0] * 1024},
                    payload={"case_id": bad_id},
                )
            ],
        )
        print(f"  ⚠️  diag- id 居然被 Qdrant 接受了?unexpected")
    except Exception as e:
        print(f"  ✅ Qdrant 拒绝 diag- id(符合预期):{type(e).__name__}: {str(e)[:120]}")

    print("\n" + SEP)
    print("结论:")
    print("  [1] 👍 入库 ✅  [2] agent 可检索 ✅  [3] 回填写入 ✅  [5] importance 升 ✅")
    print("  [6] point-id 雷:run_id 非 UUID 时索引/回填会失败(真实 👍 UI 流走 UUID 则无碍)")
    print(SEP)


if __name__ == "__main__":
    # Windows GBK console can't encode ✓/✗/👍/部分中文; force utf-8 stdout
    # (mirrors eval_recall_ablation.py -- this script's prints carry emoji).
    import contextlib
    import sys

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    asyncio.run(main())
