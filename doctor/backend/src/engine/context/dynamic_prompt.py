"""
动态 System Prompt 组装 — 根据预算阶段注入策略指令。

提供 ``build_dynamic_system_prompt()`` 按 ContextPhase 向 base_prompt 追加：
- 阶段策略文本（INITIAL / INVESTIGATING / CONVERGING / FINALIZING）
- 预算状态（已用/剩余 tokens）
- 诊断进展提示
"""

from __future__ import annotations

from typing import Any

from src.engine.context.budget import ContextBudget, ContextPhase

# 各阶段的策略文本
_PHASE_STRATEGY: dict[ContextPhase, str] = {
    ContextPhase.INITIAL: (
        "## 当前策略：系统性探索\n"
        "- 证据刚刚入，尚未建立假设\n"
        "- 从置信度最高的信号开始调查\n"
        "- 可使用所有工具进行探索\n"
        "- 建立 2-3 个初步假设后再深入"
    ),
    ContextPhase.INVESTIGATING: (
        "## 当前策略：聚焦调查\n"
        "- 聚焦最可疑的信号和假设\n"
        "- 对每个假设进行验证（搜索代码 + 查看文件内容）\n"
        "- 优先使用 code_search 定位相关代码\n"
        "- 如果发现矛盾证据，淘汰错误假设"
    ),
    ContextPhase.CONVERGING: (
        "## 当前策略：收束收敛\n"
        "- ⚠️ 预算已消耗 60%+，减少新探索\n"
        "- 从存活假设中选择置信度最高的\n"
        "- 最多再调 2-3 次工具进行最终验证\n"
        "- 优先验证代码和数据，确认根因"
    ),
    ContextPhase.FINALIZING: (
        "## 当前策略：给出最终结论\n"
        "- ⏳ 预算已消耗 80%+，不应再发起新工具调用\n"
        "- 综合已有证据，以 JSON 格式输出诊断报告\n"
        "- 证据不充分时 confidence ≤ 0.6，并在 notes 中说明缺口\n"
        '- 明确区分"已确认"与"推测"的结论\n'
    ),
}


def build_dynamic_system_prompt(
    base_prompt: str,
    budget: ContextBudget,
    diagnosis_hints: dict[str, Any] | None = None,
) -> str:
    """根据预算阶段和诊断进展组装动态 System Prompt。

    注入内容:
    1. 阶段策略文本（根据 budget.phase）
    2. 预算状态（已用 / 剩余 tokens）
    3. 诊断进展提示（如有）
    """
    parts: list[str] = [base_prompt]

    strategy = _PHASE_STRATEGY.get(budget.phase, "")
    if strategy:
        parts.append(f"\n---\n{strategy}")

    parts.append(
        "\n---\n"
        "## 预算状态\n"
        f"- 已用 tokens: {budget.total_used:,} / {budget.effective_window:,}\n"
        f"- 使用率: {budget.usage_ratio:.1%}\n"
        f"- 当前阶段: {budget.phase.value}\n"
        f"- 工具结果占用: {budget.tool_result_tokens:,}\n"
        f"- Agent 推理占用: {budget.agent_reasoning_tokens:,}"
    )

    if diagnosis_hints:
        hints_text = _format_diagnosis_hints(diagnosis_hints)
        if hints_text:
            parts.append(f"\n---\n## 诊断进展\n{hints_text}")

    return "\n".join(parts)


def _format_diagnosis_hints(hints: dict[str, Any]) -> str:
    """格式化诊断进展提示文本。"""
    lines: list[str] = []

    signal_types = hints.get("signal_types", [])
    if signal_types:
        lines.append(f"- 信号类型: {', '.join(signal_types)}")

    active_hypotheses = hints.get("active_hypotheses", [])
    if active_hypotheses:
        lines.append(f"- 活跃假设: {len(active_hypotheses)} 个")

    tools_used = hints.get("tools_used", [])
    if tools_used:
        lines.append(f"- 已用工具: {', '.join(tools_used)}")

    tool_call_count = hints.get("tool_call_count", 0)
    if tool_call_count:
        lines.append(f"- 工具调用次数: {tool_call_count}")

    return "\n".join(lines)
