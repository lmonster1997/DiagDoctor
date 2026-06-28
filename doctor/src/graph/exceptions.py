"""
DiagDoctor V3 异常体系 — 异常驱动的控制流。

借鉴 mini-swe-agent 的设计：用异常代替复杂的条件路由来实现
"预算超限 → 提前终止"、"诊断完成 → 直接跳到 Reporter"等控制流。

Usage:
    from src.graph.exceptions import (
        DiagnosisInterrupt,
        BudgetExceeded,
        DiagnosisComplete,
        FatalToolError,
    )

    raise DiagnosisComplete(report=my_report)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


class DiagnosisInterrupt(Exception):  # noqa: N818
    """所有可控诊断中断的基类。

    任何继承此类的异常都不会被视为"未处理崩溃"，而是诊断流程中的
    正常控制流信号。图编排层应捕获这些异常并优雅地转换到正确的状态。
    """

    def __init__(self, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message: str = message
        self.extra: dict[str, Any] = extra or {}


class BudgetExceeded(DiagnosisInterrupt):
    """预算超限。

    当 Agent 的工具调用次数或 token 消耗超出预设上限时抛出。
    携带当前最佳假设（best-effort findings/hypotheses），
    Reporter 节点用这些信息生成兜底报告。

    Attributes:
        message: 超限原因描述。
        extra: 当前最佳假设数据，包含：
            - ``"findings"``: list[Finding]
            - ``"hypotheses"``: list[DiagnosisHypothesis]
            - ``"tool_calls"``: int（实际调用次数）
            - ``"total_tokens"``: int
    """


class DiagnosisComplete(DiagnosisInterrupt):
    """Agent 正常完成诊断。

    当 UnifiedAgent 成功定位根因并生成结构化报告后抛出。
    携带完整的 DiagnosisReport，Reporter 节点直接使用。

    Attributes:
        message: 完成描述。
        extra: 包含 ``"report"`` 键，值为 DiagnosisReport 对象。
    """

    @property
    def report(self) -> Any:
        """便捷访问：从 extra 中提取 DiagnosisReport。"""
        return self.extra.get("report")


class FatalToolError(DiagnosisInterrupt):
    """工具不可恢复错误。

    当关键工具（如 search_observability 或 db_query）返回不可恢复的
    错误时抛出。图编排层应优雅降级，用已有证据生成 best-effort 报告。

    不应在普通工具调用失败时抛出（ReAct 循环自己会重试）。
    只在以下情况抛出：
    - 关键基础设施不可用（Tempo/Loki 全部宕机）
    - 数据库连接完全断开
    - 所有工具调用均失败

    Attributes:
        message: 错误描述。
        extra: 包含：
            - ``"tool_name"``: str（出错的工具名）
            - ``"error_detail"``: str（原始错误信息）
    """

    @property
    def tool_name(self) -> str:
        """返回出错的工具名。"""
        return str(self.extra.get("tool_name", "unknown"))
