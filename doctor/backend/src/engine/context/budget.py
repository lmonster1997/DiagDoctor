"""
ContextBudget — token 预算追踪与阶段判定。

提供：
- ``estimate_tokens()`` — cl100k_base 精确 token 估算
- ``ContextPhase`` — 诊断上下文消耗阶段枚举
- ``ContextBudget`` — 四维度（token / iteration / tool_calls / time）预算追踪
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import tiktoken

from src.config import settings
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

    S1.5：phase 现在同时考虑 token / iteration / tool_calls / time 四个维度，
    取最严级别。对 15-case 规模，token 几乎到不了 80%，但 iteration 10-12
    会触发 FINALIZING——让 phase 策略真正在 agent flail 之前 fire。
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

    # ── S1.5：iteration / tool_calls / time 维度 ──
    iteration: int = 0
    max_iterations: int = 12
    tool_calls: int = 0
    max_tool_calls: int = 18
    started_at_monotonic: float = 0.0
    max_time_seconds: float = 180.0

    # iteration-based phase 阈值（按 max_iterations 比例）
    iter_investigating_ratio: float = 0.25
    iter_converging_ratio: float = 0.58
    iter_finalizing_ratio: float = 0.83

    @property
    def effective_window(self) -> int:
        return self.model_context_window - self.reserved_for_output

    @property
    def total_used(self) -> int:
        return (
            self.system_prompt_tokens
            + self.evidence_tokens
            + self.tool_result_tokens
            + self.agent_reasoning_tokens
        )

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
        if self.max_iterations <= 0:
            return ContextPhase.INITIAL
        ratio = self.iteration / self.max_iterations
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
        if self.tool_calls >= self.max_tool_calls:
            candidates.append(ContextPhase.FINALIZING)
        if self.max_time_seconds > 0 and self.elapsed_seconds >= self.max_time_seconds:
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
            "total_used": self.total_used,
            "effective_window": self.effective_window,
            "usage_ratio": round(self.usage_ratio, 4),
            "phase": self.phase.value,
            "remaining_tokens": self.remaining_tokens,
            "is_warning": self.is_warning(),
            "is_critical": self.is_critical(),
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }
