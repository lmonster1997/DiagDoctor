"""
ContextBudget — token 预算追踪与阶段判定。

提供：
- ``estimate_tokens()`` — cl100k_base 精确 token 估算
- ``ContextPhase`` — 诊断上下文消耗阶段枚举
- ``ContextBudget`` — 四维度（token / iteration / tool_calls / time）预算追踪
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import tiktoken

from src.engine.budget.constants import MAX_MODEL_CALLS, MAX_TIME_SECONDS
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

    四维度取最严级别。单次诊断中 iteration/tool_calls 维度先于 token fire
    （入口截断压小单次工具结果，累积 token 峰值 ~50k 远低于 100k 阈值），
    token 阈值实际很少触及 80%，是兜底。详见 docs/context_engineering_design.md §5.2。

    token 口径（§7.3 接真实 usage 后）：
    - ``real_input_tokens``：从每轮 AIMessage 的 ``usage_metadata.input_tokens``
      取 peak，反映模型实际收到的 context 大小（截断后口径，根治 §6.1 split-brain）。
    - ``total_used``（gate 用）= ``max(system+evidence 静态估算, real_input_tokens)``：
      首次调用前用 tiktoken 静态估算作 floor，有调用后用真实 peak。
    - ``tool_result_tokens`` / ``agent_reasoning_tokens``：tiktoken 估算，仅作
      to_dict telemetry breakdown，**不进 gate**（曾因 BudgetGuard 拿截断前
      result 虚高触发 §6.1 split-brain）。

    预算硬上限（MAX_MODEL_CALLS / MAX_TOKENS_BUDGET / MAX_TIME_SECONDS）的单一来源
    是 engine/budget/constants.py，本类不另存副本。phase 字段当前仅导出（to_dict），
    未接回 prompt 调整策略（§5.1/§7.5）。
    """

    model_context_window: int = 128_000
    reserved_for_output: int = 4_000
    warning_threshold: float = 0.6
    critical_threshold: float = 0.8

    # ── 各来源 token 计数（system/evidence 进 gate；tool_result/reasoning 仅 telemetry）──
    system_prompt_tokens: int = 0
    evidence_tokens: int = 0
    tool_result_tokens: int = 0
    agent_reasoning_tokens: int = 0

    # ── 真实 usage（§7.3）：peak input_tokens，gate 主口径 ──
    real_input_tokens: int = 0

    # ── S1.5：iteration / tool_calls / time 维度 ──
    iteration: int = 0
    tool_calls: int = 0
    started_at_monotonic: float = 0.0

    # iteration-based phase 阈值（按 MAX_MODEL_CALLS 比例）
    iter_investigating_ratio: float = 0.25
    iter_converging_ratio: float = 0.58
    iter_finalizing_ratio: float = 0.83

    @property
    def effective_window(self) -> int:
        return self.model_context_window - self.reserved_for_output

    @property
    def total_used(self) -> int:
        # gate 用真实 context fill（peak input_tokens），静态 system+evidence 估算
        # 作为首次调用前的 floor。tool_result/agent_reasoning 的 tiktoken 估算不进
        # gate（曾导致 §6.1 split-brain：BudgetGuard 拿截断前 result 虚高），
        # 仅作 to_dict telemetry breakdown。
        static_estimate = self.system_prompt_tokens + self.evidence_tokens
        return max(static_estimate, self.real_input_tokens)

    @property
    def usage_ratio(self) -> float:
        if self.effective_window <= 0:
            return 1.0
        return min(self.total_used / self.effective_window, 1.0)

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at_monotonic:
            return 0.0
        import time as _time

        return _time.monotonic() - self.started_at_monotonic

    @property
    def _token_phase(self) -> ContextPhase:
        if self.usage_ratio >= self.critical_threshold:
            return ContextPhase.FINALIZING
        if self.usage_ratio >= self.warning_threshold:
            return ContextPhase.CONVERGING
        if self.usage_ratio >= 0.3:
            return ContextPhase.INVESTIGATING
        return ContextPhase.INITIAL

    @property
    def _iteration_phase(self) -> ContextPhase:
        if MAX_MODEL_CALLS <= 0:
            return ContextPhase.INITIAL
        ratio = self.iteration / MAX_MODEL_CALLS
        if ratio >= self.iter_finalizing_ratio:
            return ContextPhase.FINALIZING
        if ratio >= self.iter_converging_ratio:
            return ContextPhase.CONVERGING
        if ratio >= self.iter_investigating_ratio:
            return ContextPhase.INVESTIGATING
        return ContextPhase.INITIAL

    @property
    def phase(self) -> ContextPhase:
        candidates = [self._token_phase, self._iteration_phase]
        if self.tool_calls >= MAX_MODEL_CALLS:
            candidates.append(ContextPhase.FINALIZING)
        if MAX_TIME_SECONDS > 0 and self.elapsed_seconds >= MAX_TIME_SECONDS:
            candidates.append(ContextPhase.FINALIZING)
        severity = {
            ContextPhase.INITIAL: 0,
            ContextPhase.INVESTIGATING: 1,
            ContextPhase.CONVERGING: 2,
            ContextPhase.FINALIZING: 3,
        }
        return max(candidates, key=lambda p: severity[p])

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.effective_window - self.total_used)

    def start_timer(self) -> None:
        import time as _time

        self.started_at_monotonic = _time.monotonic()

    def tick_iteration(self) -> int:
        self.iteration += 1
        return self.iteration

    def add_tool_call(self, count: int = 1) -> int:
        self.tool_calls += count
        return self.tool_calls

    def add_system_prompt(self, text: str) -> int:
        tokens = estimate_tokens(text)
        self.system_prompt_tokens += tokens
        return tokens

    def add_evidence(self, text: str) -> int:
        tokens = estimate_tokens(text)
        self.evidence_tokens += tokens
        return tokens

    def add_tool_result(self, text: str) -> int:
        tokens = estimate_tokens(text)
        self.tool_result_tokens += tokens
        return tokens

    def add_agent_reasoning(self, text: str) -> int:
        tokens = estimate_tokens(text)
        self.agent_reasoning_tokens += tokens
        return tokens

    def record_real_usage(self, input_tokens: int) -> int:
        """Record real input_tokens from the latest AIMessage's usage_metadata.

        Tracks the peak context size the model actually received (post-truncation
       口径). This is the gate's primary token metric (see ``total_used``), replacing
        the tiktoken tool-result estimate that caused §6.1 split-brain.
        """
        if input_tokens > self.real_input_tokens:
            self.real_input_tokens = input_tokens
        return self.real_input_tokens

    def is_warning(self) -> bool:
        return self.usage_ratio >= self.warning_threshold

    def is_critical(self) -> bool:
        return self.usage_ratio >= self.critical_threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_prompt_tokens": self.system_prompt_tokens,
            "evidence_tokens": self.evidence_tokens,
            "tool_result_tokens": self.tool_result_tokens,
            "agent_reasoning_tokens": self.agent_reasoning_tokens,
            "real_input_tokens": self.real_input_tokens,
            "total_used": self.total_used,
            "effective_window": self.effective_window,
            "usage_ratio": round(self.usage_ratio, 4),
            "phase": self.phase.value,
            "remaining_tokens": self.remaining_tokens,
            "is_warning": self.is_warning(),
            "is_critical": self.is_critical(),
            "iteration": self.iteration,
            "max_model_calls": MAX_MODEL_CALLS,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }
