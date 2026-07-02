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
    """多标签分类 F1。

    gold = expected["category"]（列表，import 时写入）；
    pred = diagnosis["categories"]（DiagnosisReport.categories），
    若为空则退化为 [diagnosis["primary_category"]]。
    """
    gold = set(_to_str_list(expected.get("category") or expected.get("categories")))
    pred_set = set(_to_str_list(diagnosis.get("categories")))
    if not pred_set:
        pc = diagnosis.get("primary_category") or expected.get("primary_category")
        if pc:
            pred_set = {str(pc).strip()}
    if not gold and not pred_set:
        return 1.0
    tp = len(pred_set & gold)
    if tp == 0:
        return 0.0
    precision = tp / len(pred_set) if pred_set else 0.0
    recall = tp / len(gold) if gold else 0.0
    if precision + recall == 0:
        return 0.0
    return float(2 * precision * recall / (precision + recall))


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


def score_confidence_calibration(expected: dict, diagnosis: dict) -> float:
    """置信度校准：以类别是否命中作为"是否正确"的代理。

    confidence 越接近 category_hit，分数越高：``1 - |confidence - hit|``。
    """
    try:
        confidence = float(diagnosis.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    gold_categories = set(_to_str_list(expected.get("category") or expected.get("categories")))
    pred_primary = (diagnosis.get("primary_category") or "").strip()
    pred_categories = set(_to_str_list(diagnosis.get("categories")))
    hit = 1.0 if (pred_primary in gold_categories or (pred_categories & gold_categories)) else 0.0
    return float(max(0.0, 1.0 - abs(confidence - hit)))


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

    # ── Python 维度（始终计算）──
    py_scores: dict[str, float] = {
        "category_accuracy": score_category_accuracy(expected, diagnosis),
        "affected_file_accuracy": score_affected_file_accuracy(expected, diagnosis),
        "affected_line_accuracy": score_affected_line_accuracy(expected, diagnosis),
        "confidence_calibration": score_confidence_calibration(expected, diagnosis),
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


def score_process_quality(langfuse: Langfuse, trace_id: str) -> float:
    """基于 trace observation 评估 agent 调用过程质量。

    指标：
    1. dedup_ratio = unique_tool_names / total_tool_calls（重复调用惩罚）
    2. budget_ratio = min(total_calls / max_calls, 1.0)（用满预算惩罚）
    score = dedup_ratio * 0.5 + (1 - budget_ratio) * 0.5

    Returns:
        0-1 的过程质量分数。
    """
    try:
        trace = langfuse.get_trace(trace_id)
    except Exception:
        return 0.0
    observations = getattr(trace, "observations", None) or []

    tool_names: list[str] = []
    for obs in observations:
        name = getattr(obs, "name", "") or ""
        # 工具 span 名以 tool_ 开头；跳过 llm_call_* / 顶层 trace span
        tn = _extract_tool_name(name)
        if tn:
            tool_names.append(tn)

    total_calls = len(tool_names)
    unique_tools = len(set(tool_names))
    dedup_ratio = unique_tools / total_calls if total_calls else 1.0
    budget_ratio = min(total_calls / _PROCESS_MAX_CALLS, 1.0)
    score = dedup_ratio * 0.5 + (1 - budget_ratio) * 0.5
    score = float(max(0.0, min(1.0, score)))

    try:
        langfuse.score(trace_id=trace_id, name="process_quality", value=score)
    except Exception:
        pass
    return score
