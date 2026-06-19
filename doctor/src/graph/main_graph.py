"""
Main LangGraph definition for DiagDoctor diagnosis pipeline.

Currently implements a minimal graph with two dummy nodes:
- dummy_triage_node: classifies the bug category via LLM
- dummy_reporter_node: generates a fixed diagnosis report

This will be expanded in subsequent tasks with real Agent subgraphs.
"""

from langgraph.graph import END, StateGraph

from src.config import settings
from src.graph.state import DoctorState, Finding, DiagnosisReport


async def dummy_triage_node(state: DoctorState) -> dict:
    """
    Dummy triage node: calls LLM to classify bug category.

    Returns updated bug_category and a Finding.
    """
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=0.1,
    )

    user_report = state.evidence.user_report

    prompt = f"""你是一个 Bug 分类专家。基于以下用户报告，判断 bug 的类别。

【用户报告】
{user_report}

【可能的类别】
- frontend_crash: 前端运行时崩溃（白屏、JS 错误）
- backend_error: 后端异常（5xx、未处理异常）
- performance: 性能问题（慢、超时）
- logic: 业务逻辑错误（数据不对、流程错乱）
- data: 数据问题（编码、精度、时区）
- config: 配置或环境问题

请只输出类别名称（如 frontend_crash），不要输出其他内容。"""

    response = await llm.ainvoke(prompt)
    category = str(response.content).strip().lower()

    valid_categories = {
        "frontend_crash", "backend_error", "performance", "logic", "data", "config",
    }
    if category not in valid_categories:
        category = "backend_error"  # default fallback

    finding = Finding(
        agent="TriageAgent",
        summary=f"Bug classified as: {category}",
        confidence=0.7,
    )

    return {
        "bug_category": category,
        "findings": [finding],
    }


async def dummy_reporter_node(state: DoctorState) -> dict:
    """
    Dummy reporter node: generates a diagnosis report based on triage result.
    """
    report = DiagnosisReport(
        bug_category=state.bug_category or "unknown",
        root_cause=f"初步分析：此问题属于 {state.bug_category} 类型，需要进一步排查。",
        fix_suggestion="建议检查相关日志和 Trace 以定位具体根因。",
        evidence_chain=["TriageAgent 分类"],
        confidence=0.5,
    )

    return {"report": report}


def build_graph() -> StateGraph:
    """Build and compile the DiagDoctor diagnosis graph."""
    graph = StateGraph(DoctorState)

    # Add nodes
    graph.add_node("triage", dummy_triage_node)
    graph.add_node("reporter", dummy_reporter_node)

    # Add edges: START → triage → reporter → END
    graph.set_entry_point("triage")
    graph.add_edge("triage", "reporter")
    graph.add_edge("reporter", END)

    return graph.compile()
