"""
上下文引擎核心模块（方向 4，P0）—— Agent 推理质量的地基。

提供：
1. ``ContextBudget`` — token 预算追踪与阶段判定
2. ``truncate_tool_result`` — 工具结果入 context 前的预算控制
3. ``degrade_old_tool_results`` — 历史工具消息降级（任务 1.5）
4. ``build_dynamic_system_prompt`` — 动态 System Prompt 组装（任务 1.6）
5. ``maybe_compact_context`` — 自动压缩触发器（任务 1.7）

依赖方向 0（手动循环）提供注入点。

Usage (in unified_agent_node)::

    from src.graph.context_engine import (
        ContextBudget,
        ContextPhase,
        truncate_tool_result,
        maybe_compact_context,
        build_dynamic_system_prompt,
    )

    budget = ContextBudget(model_context_window=128_000)
    # ... in agent loop:
    messages, compacted = maybe_compact_context(messages, budget)
    result = truncate_tool_result(tool_name, str(raw_result))
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import tiktoken
from langchain_core.messages import BaseMessage, ToolMessage

from src.observability.logger import get_logger

logger = get_logger(__name__)

# ── Token 编码器（cl100k_base，模块级缓存）──────────
_encoder = tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str) -> int:
    """精确估算 token 数（cl100k_base 编码，适用于 OpenAI 兼容模型）。"""
    return len(_encoder.encode(text))


# ═════════════════════════════════════════════════════════════════════
# ContextPhase — 诊断阶段枚举
# ═════════════════════════════════════════════════════════════════════


class ContextPhase(StrEnum):
    """诊断上下文消耗阶段。

    - INITIAL: 刚开始，预算充裕，鼓励系统性探索
    - INVESTIGATING: 主体调查阶段，聚焦最可疑信号
    - CONVERGING: 预算 >60%，减少探索，收紧策略
    - FINALIZING: 预算 >80%，强制收束，禁止工具调用
    """

    INITIAL = "INITIAL"
    INVESTIGATING = "INVESTIGATING"
    CONVERGING = "CONVERGING"
    FINALIZING = "FINALIZING"


# ═════════════════════════════════════════════════════════════════════
# ContextBudget — token 预算数据类
# ═════════════════════════════════════════════════════════════════════


@dataclass
class ContextBudget:
    """追踪 system_prompt / evidence / tool_result / agent_reasoning 的 token 使用。

    属性:
        model_context_window: 模型上下文窗口大小（token 数）
        reserved_for_output: 保留给输出的 token 数
        warning_threshold: 警告阈值（开始降级）
        critical_threshold: 临界阈值（强制终结）

    计算属性:
        usage_ratio: 已用 token / 可用 token
        phase: 当前上下文消耗阶段
    """

    model_context_window: int = 128_000
    reserved_for_output: int = 4_000
    warning_threshold: float = 0.6
    critical_threshold: float = 0.8

    # ── 各来源 token 计数 ──
    system_prompt_tokens: int = 0
    evidence_tokens: int = 0
    tool_result_tokens: int = 0
    agent_reasoning_tokens: int = 0

    @property
    def effective_window(self) -> int:
        """有效上下文窗口 = 模型窗口 - 输出保留。"""
        return self.model_context_window - self.reserved_for_output

    @property
    def total_used(self) -> int:
        """已使用的总 token 数。"""
        return (
            self.system_prompt_tokens
            + self.evidence_tokens
            + self.tool_result_tokens
            + self.agent_reasoning_tokens
        )

    @property
    def usage_ratio(self) -> float:
        """已用 token 占有效窗口的比例 [0, 1]。"""
        if self.effective_window <= 0:
            return 1.0
        return min(self.total_used / self.effective_window, 1.0)

    @property
    def phase(self) -> ContextPhase:
        """根据 usage_ratio 自动判定当前阶段。"""
        if self.usage_ratio >= self.critical_threshold:
            return ContextPhase.FINALIZING
        if self.usage_ratio >= self.warning_threshold:
            return ContextPhase.CONVERGING
        if self.usage_ratio >= 0.3:
            return ContextPhase.INVESTIGATING
        return ContextPhase.INITIAL

    @property
    def remaining_tokens(self) -> int:
        """剩余可用 token 数。"""
        return max(0, self.effective_window - self.total_used)

    def add_system_prompt(self, text: str) -> int:
        """记录 system_prompt token 使用。返回新增 token 数。"""
        tokens = estimate_tokens(text)
        self.system_prompt_tokens += tokens
        return tokens

    def add_evidence(self, text: str) -> int:
        """记录 evidence token 使用。返回新增 token 数。"""
        tokens = estimate_tokens(text)
        self.evidence_tokens += tokens
        return tokens

    def add_tool_result(self, text: str) -> int:
        """记录工具结果 token 使用。返回新增 token 数。"""
        tokens = estimate_tokens(text)
        self.tool_result_tokens += tokens
        return tokens

    def add_agent_reasoning(self, text: str) -> int:
        """记录 Agent 推理 token 使用。返回新增 token 数。"""
        tokens = estimate_tokens(text)
        self.agent_reasoning_tokens += tokens
        return tokens

    def is_warning(self) -> bool:
        """是否达到警告阈值。"""
        return self.usage_ratio >= self.warning_threshold

    def is_critical(self) -> bool:
        """是否达到临界阈值。"""
        return self.usage_ratio >= self.critical_threshold

    def to_dict(self) -> dict[str, Any]:
        """导出为字典（用于日志/metrics）。"""
        return {
            "system_prompt_tokens": self.system_prompt_tokens,
            "evidence_tokens": self.evidence_tokens,
            "tool_result_tokens": self.tool_result_tokens,
            "agent_reasoning_tokens": self.agent_reasoning_tokens,
            "total_used": self.total_used,
            "effective_window": self.effective_window,
            "usage_ratio": round(self.usage_ratio, 4),
            "phase": self.phase.value,
            "remaining_tokens": self.remaining_tokens,
            "is_warning": self.is_warning(),
            "is_critical": self.is_critical(),
        }


# ═════════════════════════════════════════════════════════════════════
# 工具结果截断
# ═════════════════════════════════════════════════════════════════════

# 各工具类型的字符上限（按 ~4 chars/token 估算）
TOOL_CHAR_LIMITS: dict[str, int] = {
    "search_observability": 6_000,  # 1500 token
    "code_search": 4_000,  # 1000 token
    "get_file_content": 8_000,  # 2000 token
    "db_query": 3_200,  # 800 token
    "inspect_frontend_error": 4_000,  # 1000 token
}

# 从 TOOL_CHAR_LIMITS 反推，用于 default fallback
_DEFAULT_CHAR_LIMIT = 4_000

# 关键行关键词（匹配这些词的行优先保留）
_KEY_LINE_PATTERNS: list[str] = [
    "error",
    "exception",
    "trace",
    "span",
    "fail",
    "line",
    "warning",
    "critical",
    "fatal",
    "crash",
    "panic",
    "timeout",
    "refused",
    "denied",
    "forbidden",
    "stack",
    "at ",
    " caused by",
    "root cause",
    "500",
    "502",
    "503",
    "504",
    "4xx",
    "5xx",
]

# 编译正则（大小写不敏感）
_KEY_LINE_RE = re.compile(
    "|".join(_KEY_LINE_PATTERNS),
    re.IGNORECASE,
)

# 头/尾保留行数
_HEAD_LINES = 15
_TAIL_LINES = 10

# 压缩标记
_COMPRESS_MARKER = "[已压缩]"


def truncate_tool_result(tool_name: str, content: str) -> str:
    """工具结果入 context 前的预算控制。

    策略:
    1. 按工具类型设字符上限
    2. 超上限时优先保留关键行（含 error/exception/trace/span 等关键词）
    3. 关键行不足时保留头尾（前 15 + 后 10 行），中间省略
    4. 追加 ``[已压缩]`` 标记

    Args:
        tool_name: 工具名称（用于查找字符上限）
        content: 工具返回的原始内容

    Returns:
        截断后的内容（可能在阈值内保持原样）
    """
    char_limit = TOOL_CHAR_LIMITS.get(tool_name, _DEFAULT_CHAR_LIMIT)

    # 未超限 → 原样返回
    if len(content) <= char_limit:
        return content

    lines = content.split("\n")
    total_lines = len(lines)

    # ── 策略 1：仅保留关键行 ──────────────────────────────────
    key_lines: list[str] = []
    key_line_indices: set[int] = set()
    for i, line in enumerate(lines):
        if _KEY_LINE_RE.search(line):
            key_lines.append(line)
            key_line_indices.add(i)

    key_content = "\n".join(key_lines)
    if len(key_content) <= char_limit:
        # 包含上下文：关键行前后各 1 行
        enriched: list[str] = []
        for i in range(total_lines):
            # 关键行 ±1 范围内保留
            if any(abs(i - ki) <= 1 for ki in key_line_indices):
                enriched.append(lines[i])
        enriched_content = "\n".join(enriched)
        if len(enriched_content) <= char_limit:
            return enriched_content + f"\n{_COMPRESS_MARKER}（保留 {len(key_lines)} 个关键事件）"

        return key_content + f"\n{_COMPRESS_MARKER}"

    # ── 策略 2：关键行不足 → 保留头尾 ──────────────────────────
    # 先确定头尾各保留多少行（动态调整使总字符不超过限制）
    compress_marker_full = (
        f"\n... [省略中间 {total_lines - _HEAD_LINES - _TAIL_LINES} 行] ...\n{_COMPRESS_MARKER}"
    )
    marker_len = len(compress_marker_full)
    available = char_limit - marker_len

    head_lines_count = min(_HEAD_LINES, total_lines)
    tail_lines_count = min(_TAIL_LINES, total_lines - head_lines_count)

    # 动态调整：确保头+尾不超过 available
    head_text = "\n".join(lines[:head_lines_count])
    tail_text = "\n".join(lines[-tail_lines_count:]) if tail_lines_count > 0 else ""

    while len(head_text) + len(tail_text) > available and (
        head_lines_count > 3 or tail_lines_count > 2
    ):
        if head_lines_count > tail_lines_count and head_lines_count > 3:
            head_lines_count -= 1
            head_text = "\n".join(lines[:head_lines_count])
        elif tail_lines_count > 2:
            tail_lines_count -= 1
            tail_text = "\n".join(lines[-tail_lines_count:])
        else:
            break

    omitted = total_lines - head_lines_count - tail_lines_count
    result = head_text + f"\n... [省略中间 {omitted} 行] ...\n" + _COMPRESS_MARKER
    if tail_lines_count > 0:
        result += "\n" + tail_text

    return result


# ═════════════════════════════════════════════════════════════════════
# 历史消息降级（任务 1.5）
# ═════════════════════════════════════════════════════════════════════


def degrade_old_tool_results(
    messages: list[BaseMessage],
    keep_recent: int = 4,
) -> list[BaseMessage]:
    """降级旧工具消息以减少 context 占用。

    策略:
    - 最近 ``keep_recent`` 条 ToolMessage：保留原文
    - 第 keep_recent+1 至 keep_recent*2 条：保留首行 + ``[已摘要]``
    - 第 keep_recent*2+1 条及更早：替换为 ``[已归档：工具 {name} 的结果已省略]``

    Args:
        messages: 当前消息列表
        keep_recent: 保留原文的最近 ToolMessage 数量

    Returns:
        降级后的消息列表（不破坏 LangChain 消息链的 tool_call_id）
    """
    # 从后往前找到所有 ToolMessage 的索引
    tool_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], ToolMessage):
            tool_indices.append(i)

    # tool_indices 是从后往前的，即最近的在前
    for rank, idx in enumerate(tool_indices):
        msg = messages[idx]
        if not isinstance(msg, ToolMessage):
            continue

        original_content = str(msg.content)

        if rank < keep_recent:
            # 最近 keep_recent 条：保留原文
            continue
        elif rank < keep_recent * 2:
            # 次近 keep_recent 条：保留首行 + [已摘要]
            first_line = original_content.split("\n")[0]
            messages[idx] = ToolMessage(
                content=f"{first_line}\n[已摘要]",
                tool_call_id=msg.tool_call_id,
                name=msg.name,
            )
        else:
            # 更早的：归档
            tool_label = msg.name or "unknown"
            messages[idx] = ToolMessage(
                content=f"[已归档：工具 {tool_label} 的结果已省略]",
                tool_call_id=msg.tool_call_id,
                name=msg.name,
            )

    return messages


# ═════════════════════════════════════════════════════════════════════
# 动态 System Prompt 组装（任务 1.6）
# ═════════════════════════════════════════════════════════════════════

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
        "## 当前策略：强制收束\n"
        "- 🛑 预算即将耗尽（80%+），**不要再调用任何工具**\n"
        "- 基于当前已有证据，立即输出最终诊断 JSON\n"
        "- confidence 必须 ≤ 0.6（因为无法进一步验证）\n"
        "- 如果证据不足，在 notes 中说明需要哪些额外调查"
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

    Args:
        base_prompt: 基础 System Prompt
        budget: 当前预算状态
        diagnosis_hints: 可选的诊断进展提示（信号类型、活跃假设、已用工具）

    Returns:
        组装后的 System Prompt
    """
    parts: list[str] = [base_prompt]

    # ── 1. 阶段策略 ───────────────────────────────────────────
    strategy = _PHASE_STRATEGY.get(budget.phase, "")
    if strategy:
        parts.append(f"\n---\n{strategy}")

    # ── 2. 预算状态 ───────────────────────────────────────────
    parts.append(
        "\n---\n"
        "## 预算状态\n"
        f"- 已用 tokens: {budget.total_used:,} / {budget.effective_window:,}\n"
        f"- 使用率: {budget.usage_ratio:.1%}\n"
        f"- 当前阶段: {budget.phase.value}\n"
        f"- 工具结果占用: {budget.tool_result_tokens:,}\n"
        f"- Agent 推理占用: {budget.agent_reasoning_tokens:,}"
    )

    # ── 3. 诊断进展提示 ───────────────────────────────────────
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


# ═════════════════════════════════════════════════════════════════════
# 自动压缩触发器（任务 1.7）
# ═════════════════════════════════════════════════════════════════════


def maybe_compact_context(
    messages: list[BaseMessage],
    budget: ContextBudget,
) -> tuple[list[BaseMessage], bool]:
    """根据预算自动压缩 context。

    触发条件:
    - usage_ratio > 60%: 调用 ``degrade_old_tool_results(keep_recent=3)``
    - usage_ratio > 75%: 额外将所有工具结果截断到 500 字符以内

    Args:
        messages: 当前消息列表
        budget: 当前预算状态

    Returns:
        (压缩后的消息列表, 是否执行了压缩)
    """
    compacted = False

    if budget.is_warning():
        # >60%: 降级旧工具消息
        original_len = sum(
            estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content")
        )
        messages = degrade_old_tool_results(messages, keep_recent=3)
        new_len = sum(estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content"))
        compacted = True
        logger.info(
            "context_compacted_warning",
            phase=budget.phase.value,
            before_tokens=original_len,
            after_tokens=new_len,
            reduction_pct=round((1 - new_len / original_len) * 100, 1) if original_len else 0,
        )

    if budget.usage_ratio > 0.75:
        # >75%: 所有工具结果截断到 500 字符
        for i, msg in enumerate(messages):
            if isinstance(msg, ToolMessage):
                content = str(msg.content)
                if len(content) > 500:
                    messages[i] = ToolMessage(
                        content=content[:500] + f"\n{_COMPRESS_MARKER}",
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
        compacted = True
        logger.info(
            "context_compacted_critical",
            phase=budget.phase.value,
            usage_ratio=budget.usage_ratio,
        )

    return messages, compacted
