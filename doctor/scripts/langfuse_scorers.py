"""Langfuse 多维度 Scorer（D13 任务 2.1 + D14 任务 2.2）。

提供两个入口（被 ``scripts/run_baseline_experiment.py`` 调用）：

- ``score_all_dimensions(langfuse, trace_id, expected_output, diagnosis, skip_llm_judge=False)``
  计算 7 个维度分数并写入 Langfuse，返回 ``{dim: score, ..., "overall": weighted}``。
- ``score_process_quality(langfuse, trace_id)``
  读取 trace 的 observation，评估 agent 调用过程质量，返回 0-1 分数。

维度与权重（来自 docs/diagdoctor-depth-handbook-v2.md D13）：

| 维度 | 评分方式 | 权重 |
|------|---------|------|
| root_cause_accuracy       | LLM-as-Judge | 0.30 |
| fix_suggestion_quality    | LLM-as-Judge | 0.20 |
| affected_file_accuracy    | Python 精确匹配 | 0.15 |
| affected_line_accuracy    | Python 范围匹配 | 0.10 |
| category_accuracy         | Python 多标签 F1 | 0.10 |
| evidence_chain_completeness | LLM-as-Judge | 0.10 |
| confidence_calibration    | Python | 0.05 |

注：``expected_output`` schema 由 ``scripts/import_cases_to_langfuse.py`` 决定：
``{primary_category, category: list, root_cause, affected_file, fix_suggestion, fix_keywords}``。
``diagnosis`` 是 ``DiagnoseResponse`` 与其内嵌 ``report`` 字段合并后的 dict
（见 ``run_baseline_experiment.py`` 中的 ``diagnosis_for_scorer`` 构造）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langfuse import Langfuse

from src.llm_factory import get_llm_for_role
from src.prompts.registry import render_prompt

# ── 权重 ─────────────────────────────────────────────────────────────
WEIGHTS: dict[str, float] = {
    "root_cause_accuracy": 0.30,
    "fix_suggestion_quality": 0.20,
    "affected_file_accuracy": 0.15,
    "affected_line_accuracy": 0.10,
    "category_accuracy": 0.10,
    "evidence_chain_completeness": 0.10,
    "confidence_calibration": 0.05,
}

# ── 过程质量 ─────────────────────────────────────────────────────────
_PROCESS_MAX_CALLS = 12
_LINE_TOLERANCE_TIGHT = 5  # ±5 行 → 满分
_LINE_TOLERANCE_LOOSE = 20  # ±20 行 → 半分

# 匹配 LLM judge 输出中的 "Score: 0.XX"
_SCORE_RE = re.compile(r"score\s*[:：]\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)


# ═════════════════════════════════════════════════════════════════════
# Python 自定义 Scorer
# ═════════════════════════════════════════════════════════════════════


def _to_str_list(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def score_category_accuracy(expected: dict, diagnosis: dict) -> float:
    """类别命中召回率（recall-only，不惩罚多给类别，不泄露 gold 答案）。

    两个旧问题：
    1. 旧版用多标签 F1，precision 项把「agent 给出多个合理类别」误判为
       扣分，导致成功 case（合理多类别）反而比灾难 case 分数低。
    2. 旧版兜底 ``primary_category or expected.get("primary_category")``
       会把 **gold 的 primary_category 泄露进预测**——当 agent 输出空
       categories 时，scorer 用 gold 的 primary 当 pred，于是空输出的
       灾难 case（RACE/LOGIC-022/CONFIG）反而得 1.00。这正是「灾难 case
       category 普遍 1.00、成功 case 反而 0.5-0.67」反向相关的真因。

    修正：只用 recall（命中 gold 即计分，多给的正确类别不受罚），且
    primary_category 兜底**只取 agent 自己的**，不读 gold。agent 输出
    空 → category 得 0。

    gold = expected["category"]（列表，import 时写入）；
    pred = diagnosis["categories"]（DiagnosisReport.categories），
    若为空则退化为 agent 自己的 [primary_category]（不读 expected）。
    """
    gold = set(_to_str_list(expected.get("category") or expected.get("categories")))
    pred_set = set(_to_str_list(diagnosis.get("categories")))
    if not pred_set:
        # 仅用 agent 自己的 primary_category 兜底；绝不读 expected（避免泄露 gold）
        pc = diagnosis.get("primary_category")
        if pc:
            pred_set = {str(pc).strip()}
    if not gold:
        # gold 缺失时无法判定，给中性分避免污染 overall
        return 1.0 if not pred_set else 0.5
    tp = len(pred_set & gold)
    return float(tp / len(gold))


def score_affected_file_accuracy(expected: dict, diagnosis: dict) -> float:
    """文件定位精确匹配（按 basename 容错，避免绝对路径差异）。"""
    expected_file = (expected.get("affected_file") or "").strip()
    actual_file = (diagnosis.get("affected_file") or "").strip()
    if not expected_file or not actual_file:
        return 0.0
    # 优先 basename 比较；其次 endswith（兼容只给相对路径的情况）
    if Path(expected_file).name == Path(actual_file).name:
        return 1.0
    if actual_file.endswith(expected_file) or expected_file.endswith(actual_file):
        return 1.0
    return 0.0


def score_affected_line_accuracy(expected: dict, diagnosis: dict) -> float:
    """行号范围匹配。

    expected_output 通常不含 affected_line（import 脚本未写入），
    此时返回 0.0（权重 0.10，对 overall 影响有限）。
    """
    expected_line = expected.get("affected_line")
    actual_line = diagnosis.get("affected_line")
    if expected_line is None or actual_line is None:
        return 0.0
    try:
        diff = abs(int(actual_line) - int(expected_line))
    except (TypeError, ValueError):
        return 0.0
    if diff <= _LINE_TOLERANCE_TIGHT:
        return 1.0
    if diff <= _LINE_TOLERANCE_LOOSE:
        return 0.5
    return 0.0


def _root_cause_keyword_hit(expected: dict, diagnosis: dict) -> float:
    """根因关键词命中率（root_cause 正确性的廉价兜底代理，免 judge）。

    用 expected.fix_keywords 检查 diagnosis.root_cause 文本是否触及核心
    概念。仅在无 judge root_cause_accuracy 可用时作 calibration 的兜底，
    不作主路径——关键词子串匹配对同义措辞太脆（LOGIC-020 root_cause
    judge=0.95 但关键词命中仅 0.20，会误判正确 case 为过度自信）。
    """
    rc_text = str(diagnosis.get("root_cause") or "").lower()
    keywords = _to_str_list(expected.get("fix_keywords"))
    if not keywords or not rc_text:
        return 0.0
    hits = sum(1 for k in keywords if k.lower() in rc_text)
    return float(hits / len(keywords))


def score_confidence_calibration(
    expected: dict,
    diagnosis: dict,
    root_cause_accuracy: float | None = None,
) -> float:
    """置信度校准：confidence 越接近「真实正确度」越高。

    旧版只用 category_hit 当正确性代理，导致「类别命中但根因机制错」的
    case（CASCADE-020：命中 performance、root_cause=0.40、confidence=1.0）
    拿满校准分 1.0——没抓到过度自信。

    正确性代理优先级：
    1. **root_cause_accuracy（judge，主路径）**：当传入 judge 算出的
       root_cause_accuracy 时直接用它——这是最可靠的正确性信号，能识别
       「类别/文件对但根因机制错/漏」的过度自信（CASCADE）与「根因对但
       低自信」的欠自信（DATA-021/FE-020）。
    2. **结构 + 关键词兜底**：无 judge 分时退化为
       ``0.5 * structural_hit + 0.5 * root_keyword_hit``（structural_hit
       = 类别命中 OR 文件命中）。仅用于 skip_llm_judge 的快速冒烟。

    ``calibration = 1 - |confidence - correctness|``。
    """
    try:
        confidence = float(diagnosis.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    if root_cause_accuracy is not None:
        correctness = float(max(0.0, min(1.0, root_cause_accuracy)))
        return float(max(0.0, 1.0 - abs(confidence - correctness)))

    # 兜底：无 judge 分时的廉价代理（快速冒烟用）
    gold_categories = set(_to_str_list(expected.get("category") or expected.get("categories")))
    pred_primary = (diagnosis.get("primary_category") or "").strip()
    pred_categories = set(_to_str_list(diagnosis.get("categories")))
    category_hit = bool(pred_primary in gold_categories or (pred_categories & gold_categories))
    expected_file = (expected.get("affected_file") or "").strip()
    actual_file = (diagnosis.get("affected_file") or "").strip()
    file_hit = bool(
        expected_file
        and actual_file
        and (
            Path(expected_file).name == Path(actual_file).name
            or actual_file.endswith(expected_file)
            or expected_file.endswith(actual_file)
        )
    )
    structural_hit = 1.0 if (category_hit or file_hit) else 0.0
    root_hit = _root_cause_keyword_hit(expected, diagnosis)
    correctness = 0.5 * structural_hit + 0.5 * root_hit
    return float(max(0.0, 1.0 - abs(confidence - correctness)))


# ═════════════════════════════════════════════════════════════════════
# LLM-as-Judge Scorer
# ═════════════════════════════════════════════════════════════════════


def _parse_judge_score(text: str) -> float:
    """从 judge 输出中解析 'Score: 0.XX'，失败返回 0.0。"""
    if not text:
        return 0.0
    m = _SCORE_RE.search(text)
    if not m:
        return 0.0
    try:
        val = float(m.group(1))
    except ValueError:
        return 0.0
    return max(0.0, min(1.0, val))


def _run_judge(template: str, **render_vars: Any) -> float:
    """渲染 prompt 模板并调用 judge LLM，返回解析后的 0-1 分数。

    模板位于 ``src/prompts/templates/scorers/*.txt``。
    """
    try:
        prompt = render_prompt(template, **render_vars)
    except Exception:
        return 0.0
    try:
        llm = get_llm_for_role("judge")
        resp = llm.invoke(prompt)
        content = resp.content if hasattr(resp, "content") else str(resp)
        if isinstance(content, list):
            content = "".join(
                blk.get("text", "") if isinstance(blk, dict) else str(blk)
                for blk in content
            )
        return _parse_judge_score(str(content))
    except Exception:
        return 0.0


def score_root_cause_accuracy(expected: dict, diagnosis: dict) -> float:
    return _run_judge(
        "scorers/root_cause_accuracy.txt",
        expected_root_cause=expected.get("root_cause") or "",
        diagnosis_root_cause=diagnosis.get("root_cause") or "",
        diagnosis=_stringify_diagnosis(diagnosis),
    )


def score_fix_suggestion_quality(expected: dict, diagnosis: dict) -> float:
    # 期望侧：优先 fix_suggestion，回退 fix_keywords 拼接
    expected_fix = expected.get("fix_suggestion") or ""
    if not expected_fix and expected.get("fix_keywords"):
        expected_fix = "关键词: " + ", ".join(_to_str_list(expected.get("fix_keywords")))
    return _run_judge(
        "scorers/fix_suggestion_quality.txt",
        expected_fix=expected_fix,
        diagnosis_fix=diagnosis.get("fix_suggestion") or "",
        diagnosis=_stringify_diagnosis(diagnosis),
    )


def score_evidence_chain_completeness(expected: dict, diagnosis: dict) -> float:
    chain = diagnosis.get("evidence_chain") or []
    if isinstance(chain, list):
        chain_text = "\n".join(str(x) for x in chain)
    else:
        chain_text = str(chain)
    return _run_judge(
        "scorers/evidence_chain_completeness.txt",
        diagnosis_evidence_chain=chain_text,
        diagnosis=_stringify_diagnosis(diagnosis),
    )


def _stringify_diagnosis(diagnosis: dict) -> str:
    """把诊断 dict 压成可读文本供 judge 参考。"""
    keys = (
        "primary_category",
        "categories",
        "root_cause_tier",
        "root_cause",
        "affected_file",
        "affected_line",
        "fix_suggestion",
        "evidence_chain",
        "confidence",
        "notes",
    )
    parts: list[str] = []
    for k in keys:
        if k in diagnosis and diagnosis[k] not in (None, "", [], ()):
            parts.append(f"- {k}: {diagnosis[k]}")
    return "\n".join(parts) if parts else "(空)"


# ═════════════════════════════════════════════════════════════════════
# 汇总入口
# ═════════════════════════════════════════════════════════════════════


async def score_all_dimensions(
    langfuse: Langfuse,
    trace_id: str,
    expected_output: dict,
    diagnosis: dict,
    skip_llm_judge: bool = False,
) -> dict[str, float]:
    """计算并写入 7 个维度分数，返回含 overall 的 dict。

    Args:
        langfuse: Langfuse 客户端。
        trace_id: 关联的 trace id。
        expected_output: Dataset item 的 expected_output（gold）。
        diagnosis: Doctor 诊断结果（top-level + report 字段已合并）。
        skip_llm_judge: True 时跳过 LLM-as-Judge 维度（置 0.0），
            用于无 judge 模型或快速冒烟。
    """
    expected = expected_output or {}

    # ── Python 维度（不依赖 judge）──
    py_scores: dict[str, float] = {
        "category_accuracy": score_category_accuracy(expected, diagnosis),
        "affected_file_accuracy": score_affected_file_accuracy(expected, diagnosis),
        "affected_line_accuracy": score_affected_line_accuracy(expected, diagnosis),
    }

    # ── LLM-as-Judge 维度 ──
    if skip_llm_judge:
        judge_scores: dict[str, float] = {
            "root_cause_accuracy": 0.0,
            "fix_suggestion_quality": 0.0,
            "evidence_chain_completeness": 0.0,
        }
    else:
        judge_scores = {
            "root_cause_accuracy": score_root_cause_accuracy(expected, diagnosis),
            "fix_suggestion_quality": score_fix_suggestion_quality(expected, diagnosis),
            "evidence_chain_completeness": score_evidence_chain_completeness(
                expected, diagnosis
            ),
        }

    # ── confidence_calibration：依赖 judge 的 root_cause_accuracy 作正确性代理 ──
    # 必须在 judge_scores 算完后计算；skip_llm_judge 时走兜底代理
    rc_acc = judge_scores["root_cause_accuracy"] if not skip_llm_judge else None
    py_scores["confidence_calibration"] = score_confidence_calibration(
        expected, diagnosis, root_cause_accuracy=rc_acc
    )

    all_scores = {**py_scores, **judge_scores}

    # ── 加权 overall ──
    overall = 0.0
    for dim, weight in WEIGHTS.items():
        overall += weight * all_scores.get(dim, 0.0)
    all_scores["overall"] = round(overall, 4)

    # ── 写入 Langfuse ──
    for dim, value in all_scores.items():
        try:
            langfuse.score(trace_id=trace_id, name=dim, value=float(value))
        except Exception:
            # 单维度写入失败不应中断其余打分
            pass

    return all_scores


# ═════════════════════════════════════════════════════════════════════
# 过程质量 Scorer（D14 任务 2.2）
# ═════════════════════════════════════════════════════════════════════


def _extract_tool_name(obs_name: str) -> str | None:
    """从 observation name 还原工具名。

    Doctor 的工具 span 命名为 ``tool_{tool_name}_{idx}``
    （见 ``langfuse_tracing.py`` ``record_tool_span``）。
    """
    if not obs_name or not obs_name.startswith("tool_"):
        return None
    # tool_search_observability_1 → search_observability
    body = obs_name[len("tool_") :]
    # 去掉末尾的 _<数字>（或 _skipped_<数字>）
    m = re.match(r"(.+?)_(?:skipped_)?\d+$", body)
    if m:
        return m.group(1)
    return body


# 工具按「证据类别」归组：一个扎实诊断过程应覆盖三类。
# 不论 case 难度，覆盖信号+代码+验证都是好方法的标志（与旧 budget/dedup
# 不同，evidence_coverage 不会因 case 难、调用多而降分）。
_TOOL_CATEGORY: dict[str, str] = {
    "search_observability": "signal",  # 查日志/Trace 取信号
    "inspect_frontend_error": "signal",  # 前端错误信号
    "code_search": "code",  # 代码定位
    "get_file_content": "code",  # 读代码
    "db_query": "verify",  # 数据状态验证
}
_EVIDENCE_CATEGORIES = ("signal", "code", "verify")


def _extract_args_key(obs_input: Any) -> str:
    """从工具 observation 的 input 提取 args 的稳定字符串键，用于判真重复。

    observation.input 形如 ``{'args': "{'query': '...', 'source': 'tempo'}"}``，
    args 值是参数 dict 的 repr 字符串。直接用该字符串作为去重键——
    同工具名 + 同 args 字符串 = 真重复调用（无意义的重发）；
    同工具名 + 不同 args = 合理的多目标查证（不同 trace_id / 文件 / SQL），
    不应被惩罚。
    """
    if not isinstance(obs_input, dict):
        return ""
    args = obs_input.get("args")
    if args is None:
        # 兜底：用整个 input 的 repr
        return str(obs_input)
    return str(args)


def score_process_quality(langfuse: Langfuse, trace_id: str) -> float:
    """基于 trace observation 评估 agent 调用过程质量。

    旧版用 ``dedup_ratio(按工具名去重) + budget_ratio(用满预算惩罚)``，
    两个分项都错：

    - ``dedup_ratio`` 把「同工具名」当重复，但 N+1 诊断里调 5 次 db_query
      跑 5 条不同 SQL、3 次 search_observability 查 3 个不同 trace_id 都是
      合理的多目标查证，不是重复。结果 PERF-020（三重印证、诊断全对）
      被打 0.14——全场最低——恰恰因为它的彻底查证用了多个同名调用。
    - ``budget_ratio`` 把「用满预算」一律当坏事，但难 case 用满预算得到
      正确答案不是坏过程。分数与 case 难度负相关，与过程质量无关。

    新版量两个真正反映「方法是否扎实」的信号：

    1. **evidence_coverage**（方法覆盖度，权重 0.5）：
       agent 是否用了信号类工具（search_observability / inspect_frontend_error）
       + 代码类工具（code_search / get_file_content）+ 验证类工具（db_query）
       三类。覆盖越全 = 方法越扎实，与 case 难度无关。
    2. **efficiency**（无真重复，权重 0.5）：
       ``1 - 真重复调用数 / 总调用数``。真重复 = 同工具名 + 同参数字符串
       （见 :func:`_extract_args_key`）。只惩罚无意义的重发，不惩罚合理的
       多目标查证。

    ``score = 0.5 * evidence_coverage + 0.5 * efficiency``

    Returns:
        0-1 的过程质量分数。
    """
    try:
        trace = langfuse.get_trace(trace_id)
    except Exception:
        return 0.0
    observations = getattr(trace, "observations", None) or []

    # 收集 (tool_name, args_key) 对，跳过 skipped（未真正执行的工具）
    calls: list[tuple[str, str]] = []
    for obs in observations:
        name = getattr(obs, "name", "") or ""
        tn = _extract_tool_name(name)
        if not tn:
            continue
        if "_skipped_" in name:
            # 被安全层/去重跳过的工具调用不计入过程（它没真正执行）
            continue
        args_key = _extract_args_key(getattr(obs, "input", None))
        calls.append((tn, args_key))

    total = len(calls)
    if total == 0:
        return 0.0

    # ── evidence_coverage：覆盖了几类证据工具 ──
    used_categories: set[str] = set()
    for tn, _ in calls:
        cat = _TOOL_CATEGORY.get(tn)
        if cat:
            used_categories.add(cat)
    evidence_coverage = len(used_categories & set(_EVIDENCE_CATEGORIES)) / len(
        _EVIDENCE_CATEGORIES
    )

    # ── efficiency：真重复调用占比（同工具名 + 同参数）──
    seen: set[tuple[str, str]] = set()
    true_dupes = 0
    for tn, args_key in calls:
        key = (tn, args_key)
        if key in seen:
            true_dupes += 1
        else:
            seen.add(key)
    efficiency = 1.0 - (true_dupes / total)

    score = 0.5 * evidence_coverage + 0.5 * efficiency
    score = float(max(0.0, min(1.0, score)))

    try:
        langfuse.score(trace_id=trace_id, name="process_quality", value=score)
    except Exception:
        pass
    return score
