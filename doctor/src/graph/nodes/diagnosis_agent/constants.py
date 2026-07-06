"""Budget constants + token estimator for the DiagnosisAgent node.

These are module-level so they can be imported by other submodules
(budget.py, react_loop.py, forced_call.py) and by external callers
without reaching into the node itself.
"""

from __future__ import annotations

import tiktoken

# ── Token 编码器（cl100k_base，模块级缓存，避免重复构造）──────────
_encoder = tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str) -> int:
    """精确估算 token 数（cl100k_base 编码，适用于 OpenAI 兼容模型）。"""
    return len(_encoder.encode(text))


# ── Budget constants ─────────────────────────────────────────────────

MAX_TOOL_CALLS = 12
# legacy, unused in production code — referenced only by disabled
# _test_diagnosis_agent_budget.py. Kept to avoid touching the disabled test.
BUDGET_WARNING_THRESHOLD = 8  # Start considering best-effort at 8 calls
MAX_TOKENS_BUDGET = 100_000  # Soft cap on total tokens
MAX_TIME_SECONDS = 300  # 5-minute timeout per diagnosis
