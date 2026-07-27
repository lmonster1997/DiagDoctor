"""诊断预算硬常量（单一来源）。

被 BudgetGuardMiddleware（运行时硬门）、tracker.is_budget_exceeded（事后
early_stopped 判定）和 ContextBudget（phase 阈值）共同引用。三处必须读这里，
不得在 config.py / ContextBudget 里另存副本（§6.1 split-brain 的根治）。

命名：``MAX_MODEL_CALLS`` 实计 LLM 调用数（``model_call_count``），非工具调用数。
``create_agent`` 每轮 model 调用后才决定是否调工具，故 model_call 是真正的
iteration 粒度，也是 BudgetGuard 实际门控的量（§6.1 正名）。
"""

MAX_MODEL_CALLS: int = 16  # 标定后(§5.3)：P90=12 +4buffer，FE-021 flail 靠 §7.2 不加轮
BUDGET_WARNING_THRESHOLD: int = 8
MAX_TOKENS_BUDGET: int = 100_000
MAX_TIME_SECONDS: int = 300
