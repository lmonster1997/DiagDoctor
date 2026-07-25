"""P1-a tool-ified root-cause recall (design §6.4).

P0 injects symptom-similar cases statically (node-side, before the agent runs;
``diagnosis_agent._build_similar_cases_message``). P1-a exposes root-cause-
similar recall as an agent TOOL: once the agent has formed a root-cause
hypothesis, it queries the independent ``root_cause`` named vector for cases
that share the root cause -- breaking the symptom-similarity ceiling (#8
ablation: same-root-diff-symptom under-recalled, diff-root-same-symptom over-
recalled by the symptom vector).

Gated by ``settings.rag_root_cause_tool_enabled`` (independent of
``rag_injection_enabled``, which gates the P0 symptom static injection). RAG is
a gain, not a dependency: disabled / empty hypothesis / empty recall / any
failure -> a short string, never raises (the agent proceeds without historical
reference).

Index/query symmetry (§5.1): the index side (``case_store._build_point``)
embeds ``report.root_cause`` into the ``root_cause`` vector; the query side
(here) embeds the agent's ``hypothesis`` string. Both are root-cause free text,
so the two vectors live in one root-cause subspace (no symptom dilution).
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from src.config import settings
from src.memory.long_term.case_retriever import format_similar_cases, search_by_root_cause
from src.observability.logger import get_logger

logger = get_logger(__name__)


async def search_historical_root_cause(hypothesis: str, k: int = 3) -> str:
    """Retrieve historically-solved cases sharing a root-cause hypothesis.

    Call this AFTER you have formed a concrete root-cause hypothesis (e.g.
    "N+1 查询: list_tasks 逐条查 comments", "空值未判空: assignee_id 为 null 时调 .hex",
    "IDOR 越权: 缺 owner_id 过滤"). It searches the project's 👍-indexed case
    memory by root-cause similarity (not symptom similarity) and returns up to
    ``k`` historically-solved bugs with the same underlying root cause -- as a
    reference of diagnostic approach, not a verdict to copy.

    Args:
        hypothesis: One-sentence root-cause hypothesis (Chinese is fine). Be
            specific about the mechanism, not just the symptom.
        k: Max cases to return (default 3).

    Returns:
        A markdown reference block of similar historical cases, or a short
        string explaining why none were returned (empty library / disabled /
        no match / failure). Never raises.
    """
    if not settings.rag_root_cause_tool_enabled:
        return "历史根因检索未启用(rag_root_cause_tool_enabled=False)。"

    hyp = (hypothesis or "").strip()
    if not hyp:
        return (
            "请提供根因假设文本(如 'N+1 查询: list_tasks 对每个 task 单独查 comments'),"
            "再调用 search_historical_root_cause。"
        )

    try:
        scored = await search_by_root_cause(hyp, k_final=max(1, k))
    except Exception:
        logger.warning("root_cause_recall_tool_failed", exc_info=True)
        return "历史根因检索失败,请继续基于当前证据调查。"

    if not scored:
        return f"未找到与该根因假设相似的历史 case(top-{k} 为空,可能库未积累同类 👍 case)。"

    # Reuse the §6.5 reference formatter -- it's a "diagnostic approach
    # reference, judge independently" block, applicable to root-cause-similar
    # recalls just as to symptom-similar ones.
    return format_similar_cases(scored)


ROOT_CAUSE_RECALL_TOOL = StructuredTool.from_function(
    coroutine=search_historical_root_cause,
    name="search_historical_root_cause",
    description=(
        "按【根因假设】检索历史相似已解决 Bug(走独立的根因向量,非症状相似)。"
        "当你已形成一个具体根因假设(如 'N+1 查询'/'空值未判空'/'IDOR 越权'/'配置项写死')时调用,"
        "拿回根因相似的历史诊断思路作参考。输入:一句话根因假设(中文即可,说清机制而非症状);"
        "返回:历史相似 case 列表(根因/修复/类别),仅供参考、勿机械套用。"
        "仅在该项目知识库已积累 👍 入库 case 时有效;空库或无匹配会明确告知。"
    ),
)
