"""Diagnose endpoint — receives user report, invokes LLM for initial analysis."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.config import settings

router = APIRouter(prefix="/api", tags=["diagnose"])


class DiagnoseRequest(BaseModel):
    """Request body for the diagnose endpoint."""

    user_report: str = Field(
        ...,
        min_length=1,
        description="User's description of the bug or issue they encountered.",
    )


class DiagnoseResponse(BaseModel):
    """Response from the diagnose endpoint."""

    user_report: str
    analysis: str
    model: str


async def _call_llm(user_report: str) -> str:
    """Call the LLM with the user report and return the analysis."""
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_completion_tokens=settings.llm_max_tokens,
    )

    prompt = f"""你是一个 Web 应用 Bug 诊断助手。用户报告了以下问题，请给出初步分析：

【用户报告】
{user_report}

请分析：
1. 这个 bug 可能属于什么类型？（前端崩溃/后端错误/性能问题/业务逻辑/数据问题/配置问题）
2. 可能的原因是什么？
3. 建议的排查方向？

请用中文回答，保持简洁专业。"""

    response = await llm.ainvoke(prompt)
    return str(response.content)


@router.post("/diagnose", response_model=DiagnoseResponse)
async def diagnose(request: DiagnoseRequest) -> DiagnoseResponse:
    """
    Diagnose a bug based on user report.

    Currently uses LLM directly for initial analysis.
    Will be replaced by the full LangGraph pipeline in subsequent tasks.
    """
    try:
        analysis = await _call_llm(request.user_report)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"LLM invocation failed: {e}",
        ) from e

    return DiagnoseResponse(
        user_report=request.user_report,
        analysis=analysis,
        model=settings.llm_model,
    )
