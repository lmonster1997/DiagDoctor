"""诊断预算硬常量。

被 BudgetGuardMiddleware 和 forced_call 引用。
"""

MAX_TOOL_CALLS: int = 16  # 标定后(§5.3)：15-case P90=12，+4buffer=16 覆盖 14/15；FE-021 flail 靠 §7.2 不加轮
BUDGET_WARNING_THRESHOLD: int = 8
MAX_TOKENS_BUDGET: int = 100_000
MAX_TIME_SECONDS: int = 300
