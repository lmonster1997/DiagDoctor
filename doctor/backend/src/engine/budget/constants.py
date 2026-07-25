"""诊断预算硬常量。

被 BudgetGuardMiddleware 和 forced_call 引用。
"""

MAX_TOOL_CALLS: int = 12
BUDGET_WARNING_THRESHOLD: int = 8
MAX_TOKENS_BUDGET: int = 100_000
MAX_TIME_SECONDS: int = 300
