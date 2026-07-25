"""Generate synthetic (symptom + root_cause) cases for retrieval-side testing.

Why synthetic (not the 15 bug-factory gold cases):
- Retrieval recall is a *statistical* quantity -- it needs enough same-root /
  same-symptom / cross-tier pairs to draw a meaningful cosine distribution and
  calibrate thresholds. The 15 gold cases are too few (cross-tier same-root is
  a single pair, BE-022/FE-021).
- Retrieval testing only needs symptom text + root_cause text + labels. It does
  NOT need code injection / evidence chains / agent-diagnosability (that's what
  the bug-factory gold cases are for). So these can be mass-generated.
- Lives in a SEPARATE test collection (``historical_cases_test``), never touches
  the dev ``historical_cases`` library. Rebuild-and-reseed for reproducibility.

Anti-overfit measure (the key design point): each case's root_cause_summary AND
user_report text is UNIQUE across the whole 96-case set. Same-root_cause_type
cases share a root *type* but vary by entity (tasks/comments vs orders/items)
and phrasing angle, so cosine reflects real semantic similarity, not verbatim
template match. Earlier versions reused 3-4 phrasings across the 12 cases of a
type (pigeonhole -> duplicates at cosine≈1.0) which let exact-duplicate cases
crowd the twin (P1) out of top-3 and biased threshold calibration. Fixed by
building a per-type pool of templates × entities (≥ cases-per-type) and
assigning WITHOUT REPLACEMENT across all cases of that type.

Grid: root_cause_type (~8) x symptom_type (~6), 2 phrasings per cell ~ 96 cases.
Automatically covers all four propositions (P1 same-root-same-symptom,
P2 same-root-diff-symptom incl. cross-tier, P3 diff-root-same-symptom,
P4 diff-root-diff-symptom), each with tens of pairs -- P2 cross-tier ~15+ pairs,
solving the n=1 problem.

Output: ``tests/fixtures/retrieval_cases/cases.yaml`` -- a slim schema (no
bug-factory baggage): case_id / user_report / root_cause_summary /
root_cause_type / symptom_type / tier / cross_tier. Deterministic (fixed seed).

Usage (from doctor/backend)::

    uv run python scripts/gen_retrieval_test_cases.py            # write fixture
    uv run python scripts/gen_retrieval_test_cases.py --seed 42  # alt seed
    uv run python scripts/gen_retrieval_test_cases.py --stdout   # print, no write
"""

from __future__ import annotations

import argparse
import contextlib
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# ── Root-cause types ────────────────────────────────────────────────
# Each type carries ``templates`` (different phrasing ANGLES) × ``entities``
# (concrete feature/field pairs). The cross-product gives a pool of unique
# root_cause texts >= cases-per-type (12). Generation assigns WITHOUT REPLACEMENT
# across all 12 cases of the type, so no two cases share a root_cause_summary.
#
# ``root_tier`` is the tier the ROOT CAUSE lives in (frontend/backend); a case is
# cross_tier when root_tier != symptom_tier. Used to populate P2 cross-tier pairs.
ROOT_CAUSE_TYPES: dict[str, dict[str, Any]] = {
    "n-plus-1": {
        "root_tier": "backend",
        "templates": [
            "{op} 删了 selectinload 预加载，循环里逐条查 {related}，N 条记录 N 次 DB 往返",
            "返回 {parent} 列表前对每个 {parent} 单独发子查询取 {related}，查询数随数据量线性增长",
            "ORM 懒加载在序列化 {parent} 时被触发，N 条 {parent} 触发 N 次查 {related} 的额外查询",
        ],
        "entities": [
            {"op": "list_tasks", "parent": "tasks", "related": "comments"},
            {"op": "list_orders", "parent": "orders", "related": "items"},
            {"op": "list_users", "parent": "users", "related": "posts"},
            {"op": "list_projects", "parent": "projects", "related": "tags"},
        ],
    },
    "null-check": {
        "root_tier": "backend",
        "templates": [
            "接口访问 {obj}.{field} 前未判空，{obj} 为 None 时 .{access} 抛 AttributeError",
            "序列化时直接读取 {obj} 的 {field}，关系为空时解引用报错",
            "返回前读取可能为 null 的 {field}，没有 None 检查导致崩溃",
        ],
        "entities": [
            {"obj": "user", "field": "assignee_id", "access": "hex"},
            {"obj": "order", "field": "shipping_address", "access": "city"},
            {"obj": "comment", "field": "parent_id", "access": "id"},
            {"obj": "task", "field": "owner", "access": "name"},
        ],
    },
    "idor": {
        "root_tier": "backend",
        "templates": [
            "{op} 删了 owner_id == current_user 的过滤，凭 id 即可操作他人 {resource}，越权",
            "接口缺少 {resource} 归属校验，未鉴权所有者就按请求 id 返回/更新，IDOR 越权",
            "{op} 没限制查询范围到当前用户，导致跨用户 {resource} 数据泄露",
        ],
        "entities": [
            {"op": "get_order", "resource": "订单"},
            {"op": "update_comment", "resource": "评论"},
            {"op": "delete_task", "resource": "任务"},
            {"op": "list_invoices", "resource": "发票"},
        ],
    },
    "missing-field": {
        "root_tier": "backend",
        "templates": [
            "响应 schema 漏返了前端依赖的 {field}，前端读到 undefined 后解构报错",
            "后端返回体缺少约定的 {field}，前端按非空假设访问触发 TypeError",
            "序列化模型未包含 {field}，前端拿到空值崩在渲染逻辑",
        ],
        "entities": [
            {"field": "tags"},
            {"field": "status"},
            {"field": "due_date"},
            {"field": "assignee_name"},
        ],
    },
    "fk-violation": {
        "root_tier": "backend",
        "templates": [
            "写入删除了 {parent} 存在性校验，对不存在的 {parent}_id "
            "直接插 {child} 触发 IntegrityError 返回 500",
            "创建 {child} 未校验关联 {parent} 是否存在，违反外键约束导致数据库报错",
            "接口未验证 {parent}_id 有效性就落库 {child}，外键约束违反返回服务端错误",
        ],
        "entities": [
            {"parent": "project", "child": "task"},
            {"parent": "order", "child": "item"},
            {"parent": "user", "child": "post"},
            {"parent": "category", "child": "article"},
        ],
    },
    "race-condition": {
        "root_tier": "backend",
        "templates": [
            "更新 {resource} 用了先读后写模式，并发下读到旧值再覆盖，状态错乱",
            "{resource} 更新缺乏行锁/版本控制，并发更新同一资源互相覆盖，偶尔丢更新",
            "check-then-act 没有事务保护，并发请求对 {resource} 竞态导致状态不一致",
        ],
        "entities": [
            {"resource": "库存"},
            {"resource": "余额"},
            {"resource": "计数器"},
            {"resource": "状态机"},
        ],
    },
    "config-error": {
        "root_tier": "backend",
        "templates": [
            "{config} 被设成 0（误以为 0 表永久），导致令牌/缓存立即失效",
            "关键 {config} 值错误，触发非预期行为",
            "{config} 单位/默认值设错，运行时行为偏离预期",
        ],
        "entities": [
            {"config": "过期时间"},
            {"config": "超时阈值"},
            {"config": "连接池大小"},
            {"config": "重试次数"},
        ],
    },
    "silent-data-loss": {
        "root_tier": "backend",
        "templates": [
            "更新前把 {field} 从 payload 里 pop 掉，{field} 被静默置空/丢失",
            "创建时 {field} 未传就直接落 None，业务上该字段应保留旧值或默认值",
            "patch 用全量覆盖而非部分更新，未传的 {field} 被清空，数据悄悄丢失",
        ],
        "entities": [
            {"field": "tags"},
            {"field": "description"},
            {"field": "metadata"},
            {"field": "attachments"},
        ],
    },
}

# ── Symptom types ───────────────────────────────────────────────────
# Same structure: templates × entities -> pool of 16 unique symptom texts per
# type (>= 16 cases per symptom_type). Assigned without replacement.
# ``symptom_tier`` is the tier the SYMPTOM manifests in. cross_tier when
# symptom_tier != root_tier of the case's root_cause_type.
SYMPTOM_TYPES: dict[str, dict[str, Any]] = {
    "http-500": {
        "symptom_tier": "backend",
        "templates": [
            "{op} 后页面直接报错，接口返回 500 内部服务器错误",
            "提交{op}请求后服务器报内部错误，什么都没成功也没提示",
            "{op} 时后端抛错，页面卡住，网络面板看到 500 响应",
            "功能用着用着就报服务端异常，{op} 请求失败状态码 500",
        ],
        "entities": [
            {"op": "提交表单"},
            {"op": "导出报表"},
            {"op": "创建订单"},
            {"op": "上传文件"},
        ],
    },
    "frontend-crash": {
        "symptom_tier": "frontend",
        "templates": [
            "页面打开就白屏，控制台报 TypeError 读取 {comp} 的 undefined 属性",
            "渲染{comp}时崩溃，前端报 Cannot read properties of undefined，整页空白",
            "{comp}组件加载到一半报错，界面卡死，控制台一堆红色异常",
            "操作后界面直接挂掉，{comp} 抛未捕获异常白屏",
        ],
        "entities": [
            {"comp": "列表"},
            {"comp": "详情面板"},
            {"comp": "表单"},
            {"comp": "图表"},
        ],
    },
    "slow-query": {
        "symptom_tier": "backend",
        "templates": [
            "{feature}加载越来越慢，数据多了之后要转圈等一两分钟才出来",
            "打开{feature}响应明显变慢，后端查询耗时随数据量飙升",
            "{feature}接口偶发超时，数据量大时几乎卡死，明显是查询性能问题",
            "{feature}变卡，刷新要等很久，监控里看到大量慢 SQL",
        ],
        "entities": [
            {"feature": "列表页"},
            {"feature": "报表页"},
            {"feature": "搜索"},
            {"feature": "仪表盘"},
        ],
    },
    "access-anomaly": {
        "symptom_tier": "backend",
        "templates": [
            "发现能看到别人的{resource}，权限似乎没生效，越权访问",
            "用普通账号点开不该看的{resource}居然能打开，访问控制异常",
            "列表里混入了其他用户的{resource}，隔离没做好",
            "凭 id 改了不属于我的{resource}居然成功了，疑似越权",
        ],
        "entities": [
            {"resource": "订单"},
            {"resource": "评论"},
            {"resource": "任务"},
            {"resource": "发票"},
        ],
    },
    "silent-loss": {
        "symptom_tier": "backend",
        "templates": [
            "编辑保存后{field}莫名变空了，也没报错，数据悄悄丢了",
            "更新后{field}消失，没有任何错误提示，业务对不上",
            "提交后{field}没了，数据库里是空值，但操作显示成功",
            "改完发现{field}数据没了，静默丢失，很难察觉",
        ],
        "entities": [
            {"field": "标签"},
            {"field": "描述"},
            {"field": "元数据"},
            {"field": "附件"},
        ],
    },
    "intermittent": {
        "symptom_tier": "frontend",
        "templates": [
            "偶发性{feature}卡顿/闪退，复现不稳定，时好时坏",
            "{feature}偶尔失效，多试几次又好了，规律难抓",
            "间歇性{feature}报错，大多数时候正常，少数情况下崩",
            "偶发{feature}卡死，刷新后恢复，问题不稳定难定位",
        ],
        "entities": [
            {"feature": "列表"},
            {"feature": "表单提交"},
            {"feature": "导航"},
            {"feature": "搜索"},
        ],
    },
}

# 2 phrasings per (root, symptom) cell -> same-type-same-symptom pairs (P1)
# within a cell, cross-cell pairs give P2/P3/P4. ~8x6x2 = 96 cases.
PHRASINGS_PER_CELL = 2
DEFAULT_SEED = 20260723


@dataclass
class RetrievalCase:
    """One synthetic case for retrieval testing (slim schema)."""

    case_id: str
    user_report: str  # symptom text (what symptom vector embeds, index=query)
    root_cause_summary: str  # root-cause text (what root_cause vector embeds)
    root_cause_type: str  # test label: ground-truth root-cause cluster
    symptom_type: str  # test label: ground-truth symptom cluster
    tier: str  # symptom_tier (index-side payload label, NOT a query filter)
    cross_tier: bool  # root_tier != symptom_tier (for P2 cross-tier tagging)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "user_report": self.user_report,
            "root_cause_summary": self.root_cause_summary,
            "root_cause_type": self.root_cause_type,
            "symptom_type": self.symptom_type,
            "tier": self.tier,
            "cross_tier": self.cross_tier,
        }


def _slug(s: str) -> str:
    return s.replace("_", "-")


def _build_pool(type_def: dict[str, Any]) -> list[str]:
    """Cross-product templates × entities -> unique phrasings for one type."""
    templates: list[str] = type_def["templates"]
    entities: list[dict[str, str]] = type_def["entities"]
    return [t.format_map(e) for t in templates for e in entities]


def generate_cases(seed: int = DEFAULT_SEED) -> list[RetrievalCase]:
    """Generate the grid of synthetic cases deterministically (all-unique texts).

    For each (root_cause_type, symptom_type) cell, emit ``PHRASINGS_PER_CELL``
    cases. The root_cause_summary and user_report are drawn from per-type pools
    (templates × entities) WITHOUT REPLACEMENT across ALL cases of that type, so
    every case's root_cause_summary is unique and every user_report is unique ->
    no cosine≈1.0 verbatim clusters; cosine reflects real semantic similarity.
    """
    rng = random.Random(seed)
    cases: list[RetrievalCase] = []
    counter = 0

    # Pre-shuffle each type's phrasing pool once; assign sequentially without
    # replacement as cases of that type are emitted.
    rc_pools: dict[str, list[str]] = {}
    for t, d in ROOT_CAUSE_TYPES.items():
        pool = _build_pool(d)
        rng.shuffle(pool)
        rc_pools[t] = pool
    sym_pools: dict[str, list[str]] = {}
    for t, d in SYMPTOM_TYPES.items():
        pool = _build_pool(d)
        rng.shuffle(pool)
        sym_pools[t] = pool

    for rc_type, rc_def in ROOT_CAUSE_TYPES.items():
        root_tier = rc_def["root_tier"]
        for sym_type, sym_def in SYMPTOM_TYPES.items():
            sym_tier = sym_def["symptom_tier"]
            n = min(PHRASINGS_PER_CELL, len(rc_pools[rc_type]), len(sym_pools[sym_type]))
            for _ in range(n):
                counter += 1
                cases.append(
                    RetrievalCase(
                        case_id=f"RTC-{counter:03d}-{_slug(rc_type)[:8]}-{_slug(sym_type)[:8]}",
                        user_report=sym_pools[sym_type].pop(),
                        root_cause_summary=rc_pools[rc_type].pop(),
                        root_cause_type=rc_type,
                        symptom_type=sym_type,
                        tier=sym_tier,
                        cross_tier=(root_tier != sym_tier),
                    )
                )
    return cases


def _distribution(cases: list[RetrievalCase]) -> dict[str, Any]:
    """Summary counts for the generated set (sanity check on coverage)."""
    rc_counts: dict[str, int] = {}
    sym_counts: dict[str, int] = {}
    cross = 0
    for c in cases:
        rc_counts[c.root_cause_type] = rc_counts.get(c.root_cause_type, 0) + 1
        sym_counts[c.symptom_type] = sym_counts.get(c.symptom_type, 0) + 1
        if c.cross_tier:
            cross += 1
    # uniqueness check (anti-overfit): no two cases share root_cause / user_report
    rc_texts = [c.root_cause_summary for c in cases]
    sym_texts = [c.user_report for c in cases]
    unique_rc = len(set(rc_texts))
    unique_sym = len(set(sym_texts))
    # pair counts per proposition (over unordered case pairs)
    n = len(cases)
    p1 = p2 = p3 = p4 = 0
    for i in range(n):
        for j in range(i + 1, n):
            a, b = cases[i], cases[j]
            same_root = a.root_cause_type == b.root_cause_type
            same_sym = a.symptom_type == b.symptom_type
            if same_root and same_sym:
                p1 += 1
            elif same_root:
                p2 += 1
            elif same_sym:
                p3 += 1
            else:
                p4 += 1
    return {
        "total_cases": n,
        "unique_root_cause_texts": unique_rc,
        "unique_user_report_texts": unique_sym,
        "root_cause_type_counts": rc_counts,
        "symptom_type_counts": sym_counts,
        "cross_tier_cases": cross,
        "pairs": {
            "P1_same_root_same_sym": p1,
            "P2_same_root_diff_sym": p2,
            "P3_diff_root_same_sym": p3,
            "P4_diff_root_diff_sym": p4,
        },
    }


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "retrieval_cases"
FIXTURE_PATH = FIXTURE_DIR / "cases.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic retrieval test cases.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--stdout", action="store_true", help="print YAML, don't write file")
    args = parser.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    cases = generate_cases(seed=args.seed)
    dist = _distribution(cases)

    payload = {
        "meta": {
            "description": "Synthetic (symptom+root_cause) cases for retrieval-side testing. "
            "Slim schema: no bug-factory baggage. Separate test collection only. "
            "All root_cause_summary/user_report texts UNIQUE (templates×entities, "
            "assigned without replacement) -> no cosine≈1.0 verbatim clusters.",
            "seed": args.seed,
            "schema": [
                "case_id",
                "user_report",
                "root_cause_summary",
                "root_cause_type",
                "symptom_type",
                "tier",
                "cross_tier",
            ],
            "distribution": dist,
        },
        "cases": [c.to_dict() for c in cases],
    }

    yaml_text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100)

    if args.stdout:
        print(yaml_text)
    else:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(yaml_text, encoding="utf-8")
        print(f"wrote {len(cases)} cases -> {FIXTURE_PATH}")
    # always print the distribution for a quick coverage check
    print("\n=== coverage ===")
    print(f"  total cases:        {dist['total_cases']}")
    print(f"  unique root_cause:  {dist['unique_root_cause_texts']} (== total? all-unique)")
    print(f"  unique user_report: {dist['unique_user_report_texts']} (== total? all-unique)")
    print(f"  root_cause types:   {len(dist['root_cause_type_counts'])}")
    print(f"  symptom types:      {len(dist['symptom_type_counts'])}")
    print(f"  cross_tier cases:   {dist['cross_tier_cases']}")
    print(f"  P1 same-root/sym:   {dist['pairs']['P1_same_root_same_sym']} pairs")
    print(f"  P2 same-root/diff:  {dist['pairs']['P2_same_root_diff_sym']} pairs")
    print(f"  P3 diff-root/same:  {dist['pairs']['P3_diff_root_same_sym']} pairs")
    print(f"  P4 diff-root/diff:  {dist['pairs']['P4_diff_root_diff_sym']} pairs")


if __name__ == "__main__":
    main()
