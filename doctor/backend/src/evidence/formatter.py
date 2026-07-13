"""Format NormalizedEvidence into the agent's HumanMessage text.

Ingest 节点已完成 Loki/Tempo 实时查询 + 标准化管线处理，
此处仅格式化 golden_signals、correlations、frontend_error_spans 供 LLM 消费。
"""

from __future__ import annotations

from src.engine.state import Correlation, NormalizedEvidence, Signal


def format_evidence_for_agent(evidence: NormalizedEvidence) -> str:
    """
    将 NormalizedEvidence 格式化为 Agent 的 HumanMessage。

    Ingest 节点已完成 Loki/Tempo 实时查询 + 标准化管线处理，
    此处仅格式化 golden_signals、correlations、frontend_error_spans 供 LLM 消费。
    """
    parts: list[str] = []

    # ── User report ──
    if evidence.user_report:
        parts.append(f"【用户报告】\n{evidence.user_report}\n")

    # ── Trigger trace_ids (precise query handles) ──
    # These W3C trace_ids belong to THIS bug trigger only. Querying Tempo/Loki
    # by them yields exactly this trigger's spans/logs — no cross-case noise.
    if evidence.trigger_trace_ids:
        tids = ", ".join(evidence.trigger_trace_ids)
        parts.append(
            "【本次触发的 trace_id】\n"
            f"  {tids}\n"
            '  💡 用 search_observability(source="tempo", query="<某个 trace_id>") '
            "可精准拿到本次请求的完整 Trace；查日志可用 "
            'search_observability(source="loki", query=\'{trace_id="<某个 trace_id>"}\')。'
        )

    # ── Golden signals ──
    has_signals = bool(evidence.golden_signals)
    has_trace_spans = any(s.source == "trace" for s in evidence.golden_signals)

    if has_signals:
        parts.append("【实时查询信号】")
        parts.append(_format_signals(evidence.golden_signals))
        parts.append(f"（共 {len(evidence.golden_signals)} 个信号）")
    else:
        # 无错误信号场景——引导 LLM 主动调查
        parts.append("【实时查询信号】")
        parts.append("  ℹ️ （无错误/异常信号——日志和 Trace 均正常）")
        parts.append(
            "  💡 建议：根据用户报告推断 Bug 类型，主动调 search_observability "
            "查看 API 响应内容，或调 code_search / get_file_content 检查代码逻辑。"
        )

    # ── Trace availability hint ──
    if not has_trace_spans:
        parts.append(
            "\n⚠️ **缺失数据提示**：当前时间窗口内无 Trace 数据。"
            "建议先调 search_observability 获取完整 Trace。"
        )

    # ── Frontend error spans (from ingest metadata) ──
    frontend_errors = evidence.metadata.get("frontend_error_spans", [])
    if frontend_errors:
        parts.append("\n### 🔴 前端崩溃 Span (client_error)")
        for span in frontend_errors[:5]:
            name = span.get("operation_name", span.get("name", "?"))
            attrs = span.get("attributes", {})
            err_msg = attrs.get("error.message", "") or attrs.get("error", "")
            dur = span.get("duration_ms", 0)
            parts.append(f"- {name} (duration={dur}ms): {err_msg[:150]}")
            if err_msg:
                parts.append("  ⚠️ 建议调 inspect_frontend_error 分析此错误")

    # ── Correlations ──
    if evidence.correlations:
        parts.append("\n【跨层关联】")
        parts.append(_format_correlations(evidence.correlations))

    # ── Instruction ──
    parts.append(
        "\n---\n"
        "⚡ 请基于以上实时查询结果进行诊断：\n"
        "1. 分析日志和 Trace 中的错误模式\n"
        "2. 调 code_search 定位相关代码\n"
        "3. 调 get_file_content 确认代码细节\n"
        "4. 必要时调 db_query 验证数据状态（如检查字段值是否为 NULL）\n"
        "5. 输出 JSON 诊断报告（confidence 必须基于工具返回的实际数据）"
    )

    return "\n".join(parts)


def _format_signals(signals: list[Signal]) -> str:
    """Format golden signals compactly (max 30).

    Signals are classified by type (error_log / error_span / slow_span /
    repeated_query) but NOT scored — the LLM agent decides which signals
    are most relevant based on the diagnostic context.
    """
    lines: list[str] = []
    for sig in signals[:30]:
        tier_label = "前端" if sig.service_tier == "frontend" else "后端"
        sev_label = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(sig.severity, "•")
        ref = f" [{sig.signal_id}]" if sig.signal_id else ""
        lines.append(
            f"  {sev_label} [{tier_label}] [{sig.source}/{sig.signal_type}] {sig.summary}{ref}"
        )
    if not lines:
        return "  （无信号）"
    return "\n".join(lines)


def _format_correlations(correlations: list[Correlation]) -> str:
    """Format cross-layer correlations compactly (max 10)."""
    lines: list[str] = []
    for corr in correlations[:10]:
        trace_str = f" trace={corr.trace_id}" if corr.trace_id else ""
        lines.append(
            f"  - [{corr.correlation_id}]{trace_str} confidence={corr.confidence:.1f}: "
            f"{corr.description}"
        )
    if not lines:
        return "  （无关联）"
    return "\n".join(lines)
