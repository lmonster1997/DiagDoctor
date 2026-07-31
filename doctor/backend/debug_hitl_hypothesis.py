"""手工验证 §7.2 HITL 假设树 scratchpad(commit 6c4d008)。

驱动【真】CopilotKit 诊断 graph(真节点 / 路由 / checkpointer / extract_findings /
_format_scratchpad / resume),只把内层 LLM 换成 FakeAgent,且 FakeAgent 会在 pass1
emit §7.2 假设块 -> 假设树端到端亮起来。只有 LLM 是假的,§7.2 接线全是真的。

为什么用假 agent:cap=16,真 LLM 在普通 case 上很少耗尽预算,HITL 很难自然触发;
且 LLM 是否肯 emit 假设块不保证。本脚本把 commit 的接线确定性隔离出来验。

断点建议(见脚本底部注释):
  src/engine/parsing.py:150  _finding_from_json    每个 JSON 块 -> Finding(看 status/refuted)
  src/engine/parsing.py:203  _dedup_findings_by_summary  latest-status-wins
  src/engine/nodes/diagnosis_agent.py:379  pass1 extract_findings 落 findings
  src/engine/nodes/diagnosis_agent.py:469  human_input 的「已排除 N 个假设」
  src/engine/nodes/diagnosis_agent.py:240  resume scratchpad 构建(step into :83)

Run:
    python debug_hitl_hypothesis.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 gbk,扛不住 ✗/✓/? 等 Unicode -> print 会抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, "src")

from langchain_core.messages import AIMessage
from langgraph.types import Command

from src.engine.checkpointer import _LazyAsyncSqliteSaver
from src.engine.nodes import diagnosis_agent as diag_mod
from src.engine.state import Evidence

RESUME_MARKER = "续查模式"


def _rh(hypothesis: str, status: str, evidence: str, refuted: bool, tc_id: str) -> AIMessage:
    """一条 record_hypothesis 工具调用(§7.2 主路径:agent 用工具记录假设,
    而非往 content 写自由文本 JSON)。预算豁免,不计入诊断 cap。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "record_hypothesis",
                "args": {
                    "hypothesis": hypothesis,
                    "status": status,
                    "evidence": evidence,
                    "refuted": refuted,
                },
                "id": tc_id,
                "type": "tool_call",
            }
        ],
    )


def _flail(tc_id: str) -> AIMessage:
    """一条诊断 tool-call(空 content),用于堆到 17 条耗尽预算。"""
    return AIMessage(
        content="", tool_calls=[{"name": "fake_tool", "args": {}, "id": tc_id, "type": "tool_call"}]
    )

CONVERGED_JSON = """```json
{
  "primary_category": "backend_error",
  "root_cause": "GetImageByUID 导出时返回空",
  "affected_file": "app/services/export_service.py",
  "affected_function": "GetImageByUID",
  "evidence_chain": ["span-9", "sig-1"],
  "confidence": 0.85
}
```"""


class FakeAgent:
    """pass1: 调 record_hypothesis 记录假设(预算豁免)+ flail 17 条诊断
    tool-call(>=16 -> 耗尽 -> HITL);pass2(resume): 收敛 JSON。打印注入续查的 scratchpad。"""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, state: dict, config=None) -> dict:
        self.calls += 1
        msgs = state.get("messages", []) if isinstance(state, dict) else []
        is_resume = any(RESUME_MARKER in str(getattr(m, "content", "")) for m in msgs)

        if is_resume:
            # 把注入到 pass2 的 scratchpad 打出来(就是 _format_scratchpad 的产物)
            for m in msgs:
                c = str(getattr(m, "content", ""))
                if RESUME_MARKER in c:
                    print("\n========== 注入到续查(pass2)的 scratchpad ==========")
                    print(c)
                    print("====================================================\n")
                    break
            return {"messages": [AIMessage(content=CONVERGED_JSON)]}

        # pass1: 3 条 record_hypothesis(预算豁免,被 extract_findings 解析成 findings)
        #        + 17 条诊断 flail(>=16 触发 early_stopped;record 不计入所以需 17 条诊断)
        records = [
            _rh("fenix 服务故障导致导出失败", "excluded", "fenix 200 OK,延迟正常", True, "rh1"),
            _rh("PixelSpacing 污染导致重建异常", "pending", "trace-xxx", False, "rh2"),
            _rh("GetImageByUID 导出时返回空", "pending", "span-9 报错", False, "rh3"),
        ]
        flail = [_flail(f"tc{i}") for i in range(17)]  # 17 条诊断 tool-call -> 耗尽
        return {"messages": records + flail}


async def _no_rag(*_a, **_k):
    """避免 RAG 真去查 embedding,脚本保持 hermetic。"""
    return []


async def main() -> None:
    # 只换 LLM;§7.2 接线(extract_findings / _format_scratchpad / 路由 / checkpointer)全是真的
    diag_mod.get_diagnosis_agent = lambda: FakeAgent()  # type: ignore[assignment]
    diag_mod.search_historical_cases = _no_rag  # type: ignore[assignment]
    # 别把假 agent 的 trace 发到真 Langfuse
    diag_mod._get_langfuse_handler_for_dict_state = lambda *_a, **_k: None  # type: ignore[assignment]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        saver = _LazyAsyncSqliteSaver(str(Path(td) / "cp.db"))
        graph = diag_mod.build_copilotkit_graph(checkpointer=saver)
        tid = "verify-hitl-1"
        cfg = {"configurable": {"thread_id": tid}}
        state = {
            "raw_evidence": Evidence(user_report="导出图片接口偶发返回空,可疑 fenix/PixelSpacing/导出链路"),
            "case_id": tid,
            "trace_id": tid,
            "session_id": tid,
        }

        # ── pass1: 应停在 human_input 等人工引导 ──
        await graph.ainvoke(state, cfg)
        snap = await graph.aget_state(cfg)
        vals = snap.values or {}
        print("[pass1] next =", snap.next, "| early_stopped =", vals.get("early_stopped"))
        assert "human_input" in snap.next, "应停在 human_input"

        # ① human_input 中断 prompt(应含「已排除 1 个假设」)
        interrupts = getattr(snap.tasks[0], "interrupts", None)
        if interrupts:
            print("\n========== human_input 中断 prompt ==========")
            print(interrupts[0].value["prompt"])
            print("=============================================")

        # ② pass1 findings 的 status 分布(extract_findings 的产物)
        findings = vals.get("findings", [])
        print("\n[pass1] findings 解析结果(extract_findings):")
        for f in findings:
            extra = f" | 反例: {f.refutation_evidence}" if f.refutation_evidence else ""
            print(f"  - status={f.status:9} refuted={str(f.refuted):5} | {f.summary}{extra}")

        # ── resume 带引导 -> pass2(会打印注入的 scratchpad)──
        await graph.ainvoke(Command(resume="重点查 GetImageByUID 导出返回空"), cfg)
        snap2 = await graph.aget_state(cfg)
        v2 = snap2.values or {}
        report = v2.get("report")
        print("\n[pass2] next =", snap2.next, "| hitl_resumed =", v2.get("hitl_resumed"))
        print("[pass2] root_cause =", getattr(report, "root_cause", None),
              "| early_stopped =", getattr(report, "early_stopped", None))
        # add reducer: pass2 的 confirmed root_cause 累加进 findings
        print("[pass2] final findings count =", len(v2.get("findings", [])))


if __name__ == "__main__":
    asyncio.run(main())
