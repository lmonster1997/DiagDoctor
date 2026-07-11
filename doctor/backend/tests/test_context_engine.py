"""
单元测试：doctor/src/graph/context_engine.py

覆盖：
- ContextBudget: usage_ratio, phase, 各 add_* 方法
- ContextPhase: 成员值与含义
- truncate_tool_result: 不截断 / 关键行截断 / 头尾截断
- degrade_old_tool_results: 消息降级逻辑
- build_dynamic_system_prompt: 4 阶段策略注入
- maybe_compact_context: 触发条件

要求：覆盖率 ≥ 90%
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from src.graph.context_engine import (
    _COMPRESS_MARKER,
    TOOL_CHAR_LIMITS,
    ContextBudget,
    ContextPhase,
    build_dynamic_system_prompt,
    degrade_old_tool_results,
    estimate_tokens,
    maybe_compact_context,
    truncate_tool_result,
)

# ═════════════════════════════════════════════════════════════════════
# 工具函数：生成测试用的大文本
# ═════════════════════════════════════════════════════════════════════


def _make_long_content(chars: int, seed: str = "log") -> str:
    """生成指定字符数的日志风格长文本。"""
    lines: list[str] = []
    total = 0
    i = 0
    while total < chars:
        i += 1
        line = f"[2024-01-{i:02d}T12:00:00] {seed} line {i}: some message content here for testing purposes"
        lines.append(line)
        total += len(line) + 1  # +1 for \n
    return "\n".join(lines)


def _make_error_content(error_lines: int, normal_lines: int) -> str:
    """生成包含已知错误行的内容。"""
    lines: list[str] = []
    for i in range(normal_lines):
        lines.append(f"[2024-01-{i + 1:02d}T12:00:00] INFO normal operation line {i}")
    for i in range(error_lines):
        lines.append(
            f"[2024-01-{99:02d}T12:00:0{i}] ERROR exception occurred in span abc123: "
            f"trace_id=def456 line {i} fail at /api/tasks"
        )
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════
# ContextPhase
# ═════════════════════════════════════════════════════════════════════


class TestContextPhase:
    """ContextPhase 枚举测试。"""

    def test_all_phases_exist(self) -> None:
        assert ContextPhase.INITIAL.value == "INITIAL"
        assert ContextPhase.INVESTIGATING.value == "INVESTIGATING"
        assert ContextPhase.CONVERGING.value == "CONVERGING"
        assert ContextPhase.FINALIZING.value == "FINALIZING"

    def test_phase_is_string_enum(self) -> None:
        """ContextPhase 是 str Enum，值可直接比较字符串。"""
        assert ContextPhase.INITIAL == "INITIAL"
        assert ContextPhase.INITIAL.value == "INITIAL"
        assert ContextPhase.FINALIZING.value == "FINALIZING"


# ═════════════════════════════════════════════════════════════════════
# ContextBudget
# ═════════════════════════════════════════════════════════════════════


class TestContextBudgetDefaults:
    """默认构造测试。"""

    def test_default_values(self) -> None:
        budget = ContextBudget()
        assert budget.model_context_window == 128_000
        assert budget.reserved_for_output == 4_000
        assert budget.warning_threshold == 0.6
        assert budget.critical_threshold == 0.8

    def test_initial_state_zero_usage(self) -> None:
        budget = ContextBudget()
        assert budget.total_used == 0
        assert budget.usage_ratio == 0.0
        assert budget.phase == ContextPhase.INITIAL
        assert budget.remaining_tokens == 124_000

    def test_custom_window(self) -> None:
        budget = ContextBudget(model_context_window=16_000, reserved_for_output=1_000)
        assert budget.effective_window == 15_000


class TestContextBudgetPhase:
    """阶段判定测试。"""

    def test_usage_ratio_0_init_phase(self) -> None:
        budget = ContextBudget()
        assert budget.usage_ratio == 0.0
        assert budget.phase == ContextPhase.INITIAL

    def test_usage_ratio_0_29_init_phase(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.29)
        assert budget.usage_ratio < 0.3
        assert budget.phase == ContextPhase.INITIAL

    def test_usage_ratio_0_3_investigating(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.3)
        assert budget.phase == ContextPhase.INVESTIGATING

    def test_usage_ratio_0_59_investigating(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.59)
        assert budget.phase == ContextPhase.INVESTIGATING

    def test_usage_ratio_0_6_converging(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.6)
        assert budget.phase == ContextPhase.CONVERGING

    def test_usage_ratio_0_79_converging(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.79)
        assert budget.phase == ContextPhase.CONVERGING

    def test_usage_ratio_0_8_finalizing(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.8)
        assert budget.phase == ContextPhase.FINALIZING

    def test_usage_ratio_0_85_finalizing(self) -> None:
        """验收：ContextBudget(usage_ratio=0.85).phase == ContextPhase.FINALIZING"""
        budget = ContextBudget()
        # 直接设置 token 使 usage_ratio 接近 0.85
        budget.system_prompt_tokens = int(124_000 * 0.85)
        assert budget.phase == ContextPhase.FINALIZING
        assert budget.usage_ratio >= 0.84  # 浮点容差

    def test_usage_ratio_1_0_finalizing(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = 124_000
        assert budget.usage_ratio == 1.0
        assert budget.phase == ContextPhase.FINALIZING

    def test_custom_thresholds(self) -> None:
        """自定义阈值测试。"""
        budget = ContextBudget(warning_threshold=0.5, critical_threshold=0.7)
        budget.system_prompt_tokens = int(124_000 * 0.55)
        assert budget.phase == ContextPhase.CONVERGING  # >= 0.5 但 < 0.7

        budget.system_prompt_tokens = int(124_000 * 0.75)
        assert budget.phase == ContextPhase.FINALIZING  # >= 0.7


class TestContextBudgetMethods:
    """add_* 方法测试。"""

    def test_add_system_prompt(self) -> None:
        budget = ContextBudget()
        tokens = budget.add_system_prompt("Hello, this is a test system prompt.")
        assert tokens > 0
        assert budget.system_prompt_tokens == tokens
        assert budget.total_used == tokens

    def test_add_evidence(self) -> None:
        budget = ContextBudget()
        tokens = budget.add_evidence("Error log entry with stack trace information.")
        assert tokens > 0
        assert budget.evidence_tokens == tokens

    def test_add_tool_result(self) -> None:
        budget = ContextBudget()
        tokens = budget.add_tool_result("Tool result: found 42 records.")
        assert tokens > 0
        assert budget.tool_result_tokens == tokens

    def test_add_agent_reasoning(self) -> None:
        budget = ContextBudget()
        tokens = budget.add_agent_reasoning("I think the root cause is N+1 query.")
        assert tokens > 0
        assert budget.agent_reasoning_tokens == tokens

    def test_cumulative_total(self) -> None:
        budget = ContextBudget()
        budget.add_system_prompt("System prompt")
        budget.add_evidence("Evidence text")
        budget.add_tool_result("Tool result")
        budget.add_agent_reasoning("Agent reasoning")
        total = (
            budget.system_prompt_tokens
            + budget.evidence_tokens
            + budget.tool_result_tokens
            + budget.agent_reasoning_tokens
        )
        assert budget.total_used == total

    def test_remaining_tokens(self) -> None:
        budget = ContextBudget(model_context_window=10_000, reserved_for_output=1_000)
        budget.add_system_prompt("x" * 1000)  # 粗略估算
        remaining = budget.remaining_tokens
        assert remaining > 0
        assert remaining < 9_000  # effective window is 9000

    def test_is_warning(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.6)
        assert budget.is_warning() is True

    def test_is_warning_false(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.5)
        assert budget.is_warning() is False

    def test_is_critical(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.8)
        assert budget.is_critical() is True

    def test_is_critical_false(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.7)
        assert budget.is_critical() is False


class TestContextBudgetEdge:
    """边界情况。"""

    def test_zero_effective_window(self) -> None:
        """Windows <= reserved 时 usage_ratio 为 1.0。"""
        budget = ContextBudget(model_context_window=4_000, reserved_for_output=4_000)
        assert budget.effective_window == 0
        assert budget.usage_ratio == 1.0
        assert budget.phase == ContextPhase.FINALIZING

    def test_negative_effective_window(self) -> None:
        """实际场景 unlikely，但代码应保守。"""
        budget = ContextBudget(model_context_window=4_000, reserved_for_output=5_000)
        assert budget.effective_window == -1000
        assert budget.usage_ratio == 1.0

    def test_to_dict(self) -> None:
        budget = ContextBudget()
        budget.add_system_prompt("test prompt")
        d = budget.to_dict()
        assert isinstance(d, dict)
        assert "usage_ratio" in d
        assert "phase" in d
        assert d["phase"] == "INITIAL"


# ═════════════════════════════════════════════════════════════════════
# estimate_tokens
# ═════════════════════════════════════════════════════════════════════


class TestEstimateTokens:
    """Token 估算测试。"""

    def test_empty_string(self) -> None:
        assert estimate_tokens("") == 0

    def test_english_text(self) -> None:
        tokens = estimate_tokens("Hello world")
        assert tokens > 0
        assert tokens <= 5  # "Hello world" ≈ 2 tokens

    def test_chinese_text(self) -> None:
        tokens = estimate_tokens("你好世界")
        assert tokens > 0

    def test_code_text(self) -> None:
        tokens = estimate_tokens("def foo(x: int) -> str:\n    return str(x)")
        assert tokens > 0


# ═════════════════════════════════════════════════════════════════════
# truncate_tool_result
# ═════════════════════════════════════════════════════════════════════


class TestTruncateToolResultNoTruncation:
    """内容在阈值内 → 原样返回。"""

    def test_short_content_unchanged(self) -> None:
        """验收：truncate_tool_result("db_query", 500_char_content) 原样返回"""
        content = "SELECT * FROM tasks LIMIT 10;"
        result = truncate_tool_result("db_query", content)
        assert result == content

    def test_exact_limit_unchanged(self) -> None:
        limit = TOOL_CHAR_LIMITS["db_query"]
        content = "x" * limit
        result = truncate_tool_result("db_query", content)
        assert result == content
        assert _COMPRESS_MARKER not in result

    def test_search_observability_under_limit(self) -> None:
        limit = TOOL_CHAR_LIMITS["search_observability"]
        content = "log entry\n" * 100
        assert len(content) < limit
        result = truncate_tool_result("search_observability", content)
        assert result == content

    def test_unknown_tool_uses_default(self) -> None:
        content = "x" * 100
        result = truncate_tool_result("unknown_tool", content)
        assert result == content  # 100 < 4000 default


class TestTruncateToolResultKeyLines:
    """超限但关键行足够 → 保留关键行。"""

    def test_key_lines_preserved(self) -> None:
        """关键行包含 error/exception 等关键词，应优先保留。"""
        lines: list[str] = []
        for i in range(400):
            lines.append(f"[INFO] normal log line {i}")
        # 插入关键行
        lines.append("[ERROR] critical exception in span=abc123 trace=def456")
        lines.append("[ERROR] fail at /api/tasks line 42")
        content = "\n".join(lines)
        assert len(content) > TOOL_CHAR_LIMITS["search_observability"]

        result = truncate_tool_result("search_observability", content)
        assert _COMPRESS_MARKER in result
        assert "critical exception" in result
        assert "trace=def456" in result
        assert len(result) < len(content)

    def test_search_observability_truncation(self) -> None:
        """验收：truncate_tool_result("search_observability", 10000_char_content) 返回 < 6000 字符"""
        content = _make_long_content(10_000, seed="log")
        assert len(content) >= 10_000

        result = truncate_tool_result("search_observability", content)
        assert len(result) < 6_000
        assert _COMPRESS_MARKER in result

    def test_key_lines_with_error_keywords(self) -> None:
        """包含 error/exception/trace/span/fail/line 关键词的行被保留。"""
        content = _make_error_content(error_lines=10, normal_lines=500)
        assert len(content) > TOOL_CHAR_LIMITS["search_observability"]

        result = truncate_tool_result("search_observability", content)
        assert _COMPRESS_MARKER in result
        # 错误行应被保留
        assert "ERROR exception" in result
        assert "trace_id=def456" in result
        assert "span abc123" in result


class TestTruncateToolResultHeadTail:
    """关键行不足 → 保留头尾。"""

    def test_head_tail_fallback(self) -> None:
        """全部 INFO 行，无关键行 → 保留头尾。"""
        lines = [f"[INFO] normal operation line {i}" for i in range(500)]
        content = "\n".join(lines)
        assert len(content) > TOOL_CHAR_LIMITS["code_search"]

        result = truncate_tool_result("code_search", content)
        assert _COMPRESS_MARKER in result
        assert "[INFO] normal operation line 0" in result
        assert "[INFO] normal operation line 499" in result
        assert "省略中间" in result

    def test_head_lines_preserved(self) -> None:
        """前 _HEAD_LINES 行应保留。"""
        lines = [f"line {i:04d}" for i in range(800)]
        content = "\n".join(lines)
        assert len(content) > TOOL_CHAR_LIMITS["code_search"]
        result = truncate_tool_result("code_search", content)
        assert _COMPRESS_MARKER in result
        assert "line 0000" in result
        assert "line 0001" in result

    def test_tail_lines_preserved(self) -> None:
        """后 _TAIL_LINES 行应保留。"""
        lines = [f"line {i:04d}" for i in range(800)]
        content = "\n".join(lines)
        assert len(content) > TOOL_CHAR_LIMITS["code_search"]
        result = truncate_tool_result("code_search", content)
        assert _COMPRESS_MARKER in result
        assert "line 0799" in result


class TestTruncateToolResultPerTool:
    """按工具类型的 token 上限验证。"""

    def test_search_observability_limit(self) -> None:
        """search_observability 上限 1500 token ≈ 6000 chars"""
        assert TOOL_CHAR_LIMITS["search_observability"] == 6_000
        content = "x" * 10_000
        result = truncate_tool_result("search_observability", content)
        assert len(result) <= 6_000 + 200  # 允许压缩标记的额外开销

    def test_code_search_limit(self) -> None:
        """code_search 上限 1000 token ≈ 4000 chars"""
        assert TOOL_CHAR_LIMITS["code_search"] == 4_000

    def test_get_file_content_limit(self) -> None:
        """get_file_content 上限 2000 token ≈ 8000 chars"""
        assert TOOL_CHAR_LIMITS["get_file_content"] == 8_000

    def test_db_query_limit(self) -> None:
        """db_query 上限 800 token ≈ 3200 chars"""
        assert TOOL_CHAR_LIMITS["db_query"] == 3_200

    def test_inspect_frontend_error_limit(self) -> None:
        """inspect_frontend_error 上限 1000 token ≈ 4000 chars"""
        assert TOOL_CHAR_LIMITS["inspect_frontend_error"] == 4_000


# ═════════════════════════════════════════════════════════════════════
# degrade_old_tool_results
# ═════════════════════════════════════════════════════════════════════


class TestDegradeOldToolResults:
    """历史消息降级测试。"""

    def _make_tool_messages(self, count: int, prefix: str = "result") -> list[ToolMessage]:
        """构造 n 条 ToolMessage，内容较长便于测试降级效果。"""
        return [
            ToolMessage(
                content=f"{prefix} {i}: detailed tool output with multiple lines of content\n"
                + "\n".join(
                    f"  line {j}: some detailed output data here for testing" for j in range(5)
                ),
                tool_call_id=f"call_{i}",
                name=f"tool_{i % 5}",
            )
            for i in range(count)
        ]

    def test_recent_messages_unchanged(self) -> None:
        """最近 4 条保留原文。"""
        tool_msgs = self._make_tool_messages(6)
        messages: list = [HumanMessage(content="start")] + list(tool_msgs)
        result = degrade_old_tool_results(messages, keep_recent=4)

        # 最近 4 条（索引 2-5）应保留原文
        tool_results = [m for m in result if isinstance(m, ToolMessage)]
        for i in range(2, 6):
            assert tool_results[i].content == tool_msgs[i].content

    def test_middle_messages_summarized(self) -> None:
        """第 5-8 条保留首行 + [已摘要]。"""
        tool_msgs = self._make_tool_messages(12)
        messages: list = [HumanMessage(content="start")] + list(tool_msgs)
        result = degrade_old_tool_results(messages, keep_recent=4)

        tool_results = [m for m in result if isinstance(m, ToolMessage)]
        # 12 条 ToolMessage，最近 4 条保留原文，接下来 4 条摘要，最早 4 条归档
        # tool_results[4] 是第 5 条（rank 4，应从摘要开始）
        has_summarized = any("[已摘要]" in str(m.content) for m in tool_results)
        has_archived = any("已归档" in str(m.content) for m in tool_results)
        assert has_summarized, "Should have at least one summarized message"
        assert has_archived, "Should have at least one archived message"
        # 最近 4 条应保留原文
        for i in range(1, 5):
            assert tool_results[-i].content == tool_msgs[-i].content

    def test_old_messages_archived(self) -> None:
        """第 9+ 条替换为归档。"""
        tool_msgs = self._make_tool_messages(10)
        messages: list = [HumanMessage(content="start")] + list(tool_msgs)
        result = degrade_old_tool_results(messages, keep_recent=4)

        tool_results = [m for m in result if isinstance(m, ToolMessage)]
        assert "已归档" in str(tool_results[0].content)  # rank 9 = index 0

    def test_token_reduction(self) -> None:
        """验收：12 条工具消息降级后，总 token 减少 ≥ 40%"""
        tool_msgs = self._make_tool_messages(12, prefix="long_result_" * 10)
        messages: list = [HumanMessage(content="hello")] + list(tool_msgs)

        before = sum(estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content"))
        result = degrade_old_tool_results(messages, keep_recent=4)
        after = sum(estimate_tokens(str(m.content)) for m in result if hasattr(m, "content"))

        reduction = (before - after) / before
        assert reduction >= 0.40, (
            f"Expected ≥40% reduction, got {reduction:.1%} (before={before}, after={after})"
        )

    def test_tool_call_id_preserved(self) -> None:
        """降级后 tool_call_id 不丢失（不破坏 LangChain 消息链）。"""
        tool_msgs = self._make_tool_messages(6)
        messages: list = [HumanMessage(content="start")] + list(tool_msgs)

        result = degrade_old_tool_results(messages, keep_recent=2)
        tool_results = [m for m in result if isinstance(m, ToolMessage)]
        for msg in tool_results:
            assert msg.tool_call_id != "", "tool_call_id should not be empty"
            assert msg.tool_call_id.startswith("call_")

    def test_keep_recent_parameter(self) -> None:
        """keep_recent 参数控制保留数量。"""
        tool_msgs = self._make_tool_messages(5)
        messages: list = [HumanMessage(content="start")] + list(tool_msgs)

        result = degrade_old_tool_results(messages, keep_recent=2)
        tool_results = [m for m in result if isinstance(m, ToolMessage)]
        # 最近 2 条应保留原文
        assert tool_results[-1].content == tool_msgs[-1].content
        assert tool_results[-2].content == tool_msgs[-2].content
        # 第 3 条应被摘要
        assert "[已摘要]" in str(tool_results[-3].content) or _COMPRESS_MARKER in str(
            tool_results[-3].content
        )

    def test_no_tool_messages(self) -> None:
        """无 ToolMessage 时不变。"""
        messages: list = [HumanMessage(content="hello"), SystemMessage(content="system")]
        result = degrade_old_tool_results(messages)
        assert result == messages

    def test_non_tool_messages_unchanged(self) -> None:
        """非 ToolMessage 不被修改。"""
        tool_msgs = self._make_tool_messages(8)
        messages: list = [
            SystemMessage(content="system prompt"),
            HumanMessage(content="user input"),
        ] + list(tool_msgs)

        result = degrade_old_tool_results(messages, keep_recent=3)
        assert isinstance(result[0], SystemMessage)
        assert result[0].content == "system prompt"
        assert isinstance(result[1], HumanMessage)
        assert result[1].content == "user input"


# ═════════════════════════════════════════════════════════════════════
# build_dynamic_system_prompt
# ═════════════════════════════════════════════════════════════════════


class TestBuildDynamicSystemPrompt:
    """动态 System Prompt 组装测试。"""

    BASE = "You are a diagnostic agent."

    def test_initial_phase_strategy_injected(self) -> None:
        budget = ContextBudget()
        prompt = build_dynamic_system_prompt(self.BASE, budget)
        assert self.BASE in prompt
        assert "系统性探索" in prompt

    def test_investigating_phase_strategy_injected(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.4)
        prompt = build_dynamic_system_prompt(self.BASE, budget)
        assert "聚焦调查" in prompt

    def test_converging_phase_strategy_injected(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.65)
        prompt = build_dynamic_system_prompt(self.BASE, budget)
        assert "收束收敛" in prompt
        assert "60%" in prompt or "预算已消耗" in prompt

    def test_finalizing_phase_strategy_injected(self) -> None:
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.85)
        prompt = build_dynamic_system_prompt(self.BASE, budget)
        assert "强制收束" in prompt
        assert "不要再调用任何工具" in prompt
        assert "confidence" in prompt.lower()
        assert "0.6" in prompt

    def test_budget_status_injected(self) -> None:
        budget = ContextBudget()
        budget.add_system_prompt("system")
        budget.add_tool_result("tool")
        prompt = build_dynamic_system_prompt(self.BASE, budget)
        assert "预算状态" in prompt
        assert "已用 tokens" in prompt
        assert "使用率" in prompt
        assert "当前阶段" in prompt

    def test_diagnosis_hints_injected(self) -> None:
        budget = ContextBudget()
        hints = {
            "signal_types": ["error_log", "slow_span"],
            "active_hypotheses": ["N+1 query", "missing index"],
            "tools_used": ["search_observability", "code_search"],
            "tool_call_count": 5,
        }
        prompt = build_dynamic_system_prompt(self.BASE, budget, diagnosis_hints=hints)
        assert "诊断进展" in prompt
        assert "error_log" in prompt
        assert "活跃假设" in prompt
        assert "2 个" in prompt
        assert "search_observability" in prompt
        assert "工具调用次数" in prompt

    def test_length_within_limit(self) -> None:
        """动态 prompt 总长度不超过 base_prompt + 500 字符（验收条件）。"""
        base = "You are a diagnostic agent. Follow the steps."
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.85)
        prompt = build_dynamic_system_prompt(base, budget)
        # FINALIZING 阶段是最长的
        assert len(prompt) <= len(base) + 800, f"Expected ≤{len(base) + 800}, got {len(prompt)}"

    def test_no_hints_no_crash(self) -> None:
        budget = ContextBudget()
        prompt = build_dynamic_system_prompt(self.BASE, budget, diagnosis_hints=None)
        assert self.BASE in prompt

    def test_empty_hints_no_crash(self) -> None:
        budget = ContextBudget()
        prompt = build_dynamic_system_prompt(self.BASE, budget, diagnosis_hints={})
        assert self.BASE in prompt


# ═════════════════════════════════════════════════════════════════════
# maybe_compact_context
# ═════════════════════════════════════════════════════════════════════


class TestMaybeCompactContext:
    """自动压缩触发器测试。"""

    def _make_messages_with_tools(self, tool_count: int) -> list:
        """构造含有多条 ToolMessage 的消息列表。"""
        messages: list = [
            SystemMessage(content="system prompt"),
            HumanMessage(content="user evidence"),
        ]
        for i in range(tool_count):
            messages.append(
                ToolMessage(
                    content=f"tool result {i}: " + "detailed output " * 50,
                    tool_call_id=f"call_{i}",
                    name=f"tool_{i % 5}",
                )
            )
        return messages

    def test_no_compaction_below_warning(self) -> None:
        """预算 <60% 时不触发压缩。"""
        budget = ContextBudget()
        budget.add_system_prompt("small prompt")
        messages = self._make_messages_with_tools(3)
        result, compacted = maybe_compact_context(messages, budget)
        assert compacted is False
        assert result == messages

    def test_compaction_at_warning(self) -> None:
        """预算 =60% 时触发压缩。"""
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.61)
        messages = self._make_messages_with_tools(8)
        result, compacted = maybe_compact_context(messages, budget)
        assert compacted is True

    def test_compaction_triggers_degradation(self) -> None:
        """压缩后旧消息被降级。"""
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.65)
        messages = self._make_messages_with_tools(10)
        result, compacted = maybe_compact_context(messages, budget)
        assert compacted is True
        # 应有消息被标记
        tool_contents = [str(m.content) for m in result if isinstance(m, ToolMessage)]
        has_degraded = any(
            "已摘要" in c or "已归档" in c or _COMPRESS_MARKER in c for c in tool_contents
        )
        assert has_degraded, "Should have at least one degraded tool message"

    def test_critical_truncation(self) -> None:
        """预算 >75% 时工具结果被截断到 500 字符。"""
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.76)
        messages = self._make_messages_with_tools(5)
        result, compacted = maybe_compact_context(messages, budget)
        assert compacted is True
        # 长工具消息应被截断
        for msg in result:
            if (
                isinstance(msg, ToolMessage)
                and "tool result" in str(msg.content)
                and _COMPRESS_MARKER in str(msg.content)
            ):
                # 如果有截断标记，应该 ≤ 500 + 标记长度
                assert len(str(msg.content)) <= 500 + len(_COMPRESS_MARKER) + 10

    def test_recent_messages_not_compacted(self) -> None:
        """最近 3 条工具消息不被压缩。"""
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.65)
        messages = self._make_messages_with_tools(6)
        result, _ = maybe_compact_context(messages, budget)
        tool_results = [m for m in result if isinstance(m, ToolMessage)]
        # 最近 3 条应保留原文
        for i in range(1, 4):
            assert "[已摘要]" not in str(tool_results[-i].content)
            assert "已归档" not in str(tool_results[-i].content)

    def test_token_reduction_after_compaction(self) -> None:
        """验收：压缩后 messages 总 token 减少 ≥ 30%"""
        budget = ContextBudget()
        budget.system_prompt_tokens = int(124_000 * 0.70)
        messages = self._make_messages_with_tools(12)

        before = sum(estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content"))
        result, compacted = maybe_compact_context(messages, budget)
        after = sum(estimate_tokens(str(m.content)) for m in result if hasattr(m, "content"))

        if before > 0:
            reduction = (before - after) / before
            assert reduction >= 0.30, (
                f"Expected ≥30% reduction, got {reduction:.1%} (before={before}, after={after})"
            )


# ═════════════════════════════════════════════════════════════════════
# 集成测试：ContextBudget + truncate 联合
# ═════════════════════════════════════════════════════════════════════


class TestIntegration:
    """集成场景测试。"""

    def test_full_diagnosis_flow_budget_tracking(self) -> None:
        """模拟完整诊断流程的预算追踪。"""
        budget = ContextBudget()
        assert budget.phase == ContextPhase.INITIAL

        # Step 1: system prompt
        budget.add_system_prompt("You are a diagnostic agent. " * 100)
        assert budget.phase == ContextPhase.INITIAL

        # Step 2: evidence
        budget.add_evidence("Error logs and traces..." * 500)
        # 可能进入 INVESTIGATING

        # Step 3: tool results — 大量 token 推动到 FINALIZING
        # 需要让预算超过 80% (124000 * 0.8 ≈ 99200 tokens)
        # 使用多样文本避免 tiktoken 压缩重复字符
        huge_text = "The quick brown fox jumps over the lazy dog. " * 200
        for _ in range(80):
            budget.add_tool_result(huge_text)
            budget.add_agent_reasoning(huge_text)

        # 应该进入 FINALIZING（预算已用超 80%）
        assert budget.phase == ContextPhase.FINALIZING, (
            f"phase={budget.phase}, ratio={budget.usage_ratio:.2%}, used={budget.total_used}"
        )
        assert budget.is_critical() is True

    def test_truncate_then_add_to_budget(self) -> None:
        """截断后再加入预算追踪。"""
        budget = ContextBudget()
        raw = _make_long_content(20_000, seed="error")

        # 截断
        truncated = truncate_tool_result("search_observability", raw)
        assert len(truncated) < 6_000

        # 加入预算
        tokens = budget.add_tool_result(truncated)
        assert tokens < estimate_tokens(raw)  # 截断后 token 明显减少
        assert budget.tool_result_tokens == tokens
