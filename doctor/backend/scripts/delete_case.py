"""删除 historical_cases 里的某条历史记忆(按 case_id / point id 或 payload 关键词匹配)。

为什么需要它:
  历史记忆靠 👍 触发入库(case_store.maybe_index_diagnosis,source=user_upvote),
  point id = case_id = thread_id。一旦某次诊断结果有问题又被 👍 入库,它会作为
  历史 case 被未来检索召回,污染后续诊断 -> 需要把它从 Qdrant 删掉。

  但 point id 必须是 uint 或 UUID(qdrant_client 实证过"point-id 雷":非 UUID id
  会被 Qdrant 拒绝)。所以用户记的 case 标识(如 "BE020")不一定是真正的 point id
  -- 可能是 thread_id 别名,也可能当时压根没入库。本脚本先只读侦察,确认到底匹配
  到哪些点,再决定删不删。

  纯 Qdrant 操作,不需要 embedding / LLM 环境。

匹配策略(三者取并集):
  1. 精确按 point id retrieve(target 当 point id 直接查;非 UUID 可能报错,try/except)
  2. payload 字段精确匹配:case_id / trace_id == target
  3. payload 任意字符串字段子串匹配 target(兜底:root_cause / user_report_snippet 等)

用法(在 doctor/backend 下):
  uv run python scripts/delete_case.py BE020            # dry-run:只查不删,展示匹配点
  uv run python scripts/delete_case.py BE020 --yes      # 真删
  uv run python scripts/delete_case.py BE020 --limit 5  # 没匹配时,展示最近 N 个点辅助定位
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from qdrant_client import models

from src.memory.long_term.qdrant_client import COLLECTION_NAME, get_qdrant_client
from src.observability.logger import get_logger

logger = get_logger(__name__)

SEP = "─" * 60


async def _collection_count(client) -> int:
    try:
        info = await client.count(COLLECTION_NAME, exact=True)
        return info.count
    except Exception:
        return -1


async def _scroll_all(client) -> list[models.Record]:
    """Scroll every point in the collection (with payload, no vectors)."""
    pts: list[models.Record] = []
    offset = None
    while True:
        batch, offset = await client.scroll(
            collection_name=COLLECTION_NAME,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        pts.extend(batch)
        if offset is None:
            break
    return pts


def _matches(point: models.Record, target: str) -> tuple[bool, list[str]]:
    """Return (hit, reasons). Reasons describe which field triggered the match."""
    reasons: list[str] = []
    pl = dict(point.payload or {})

    # 1. point id 精确
    if str(point.id) == target:
        reasons.append(f"point_id={point.id}")

    # 2. payload 关键字段精确
    for key in ("case_id", "trace_id"):
        if pl.get(key) == target:
            reasons.append(f"{key}={pl.get(key)}")

    # 3. payload 任意字符串字段子串(兜底)
    for key, val in pl.items():
        if isinstance(val, str) and target in val and key not in ("case_id", "trace_id"):
            preview = val[:80].replace("\n", " ")
            reasons.append(f"{key}~={preview!r}")
            break  # 每个字段只记一条

    return (len(reasons) > 0, reasons)


def _fmt(point: models.Record, reasons: list[str] | None = None) -> str:
    pl = dict(point.payload or {})
    rc = str(pl.get("root_cause", ""))[:60].replace("\n", " ")
    line = (
        f"  id={point.id}  case_id={pl.get('case_id')}  "
        f"trace_id={pl.get('trace_id')}  conf={pl.get('confidence')}  "
        f"created={pl.get('created_at')}\n"
        f"    root_cause={rc!r}"
    )
    if reasons:
        line += f"\n    match: {' | '.join(reasons)}"
    return line


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", help="要删除的 case 标识(point id / case_id / 关键词)")
    parser.add_argument("--yes", action="store_true", help="真删(默认 dry-run 只查不删)")
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="未匹配时,展示最近 N 个点辅助定位(按 created_at 倒序,默认 5)",
    )
    args = parser.parse_args()

    client = await get_qdrant_client()
    total = await _collection_count(client)
    print(
        f"{SEP}\n collection={COLLECTION_NAME}  total_points={total}  target={args.target!r}  "
        f"mode={'DELETE' if args.yes else 'dry-run'}\n{SEP}"
    )

    # ── 1. 精确按 point id retrieve(非 UUID 可能报错)──
    by_id: list[models.Record] = []
    try:
        by_id = await client.retrieve(
            collection_name=COLLECTION_NAME,
            ids=[args.target],
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        print(
            f"\n[retrieve by id={args.target!r} 失败(非 UUID 正常)] "
            f"{type(e).__name__}: {str(e)[:120]}"
        )
    if by_id:
        print(f"\n[point-id 精确命中 {len(by_id)} 个]")

    # ── 2. scroll 全量,payload 匹配 ──
    print("\n[scroll 全量 + payload 匹配中...]")
    all_pts = await _scroll_all(client)
    matched: list[tuple[models.Record, list[str]]] = []
    for p in all_pts:
        # point-id 命中的也算一次 match(统一走 _matches,reasons 里会有 point_id=...)
        hit, reasons = _matches(p, args.target)
        if hit:
            matched.append((p, reasons))

    # 去重:by_id 命中的点可能也在 matched 里(若 point_id==target)
    seen_ids: set[str] = set()
    uniq: list[tuple[models.Record, list[str]]] = []
    for p, r in [(p, [f"point_id={p.id}"]) for p in by_id] + matched:
        k = str(p.id)
        if k in seen_ids:
            continue
        seen_ids.add(k)
        uniq.append((p, r))

    if not uniq:
        print(f"\n[!] 没有匹配 {args.target!r} 的点。库里共 {len(all_pts)} 个点。")
        # 展示最近 N 个辅助定位
        recent = sorted(
            all_pts,
            key=lambda p: str(dict(p.payload or {}).get("created_at", "")),
            reverse=True,
        )[: args.limit]
        if recent:
            print(f"\n最近 {len(recent)} 个点(看有没有你印象中的那条):")
            for p in recent:
                print(_fmt(p))
        print("\n提示:target 可以是 point id / case_id / trace_id / root_cause 里的关键词。")
        return 0

    print(f"\n[匹配到 {len(uniq)} 个点]")
    for p, r in uniq:
        print(_fmt(p, r))

    if not args.yes:
        print("\n[dry-run] 未删除。确认无误后加 --yes 真删:")
        print(f"  uv run python scripts/delete_case.py {args.target} --yes")
        return 0

    # ── 3. 真删 ──
    ids = [p.id for p, _ in uniq]
    print(f"\n[DELETE] 即将删除 {len(ids)} 个 point: {ids}")
    await client.delete(collection_name=COLLECTION_NAME, points_selector=ids)
    print("[DELETE] 完成。删除后剩余(精确计数):")
    after = await _collection_count(client)
    print(f"  total_points={after}  (删除前={total})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
