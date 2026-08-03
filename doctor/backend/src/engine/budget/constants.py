"""诊断预算硬常量（单一来源）。

被 BudgetGuardMiddleware（运行时硬门）和 ContextBudget（phase 阈值）共同引用。
两处必须读这里，不得在 config.py / ContextBudget 里另存副本（§6.1 split-brain
的根治）。

命名：``MAX_MODEL_CALLS`` 实计 LLM 调用数（``model_call_count``），非工具调用数。
``create_agent`` 每轮 model 调用后才决定是否调工具，故 model_call 是真正的
iteration 粒度，也是 BudgetGuard 实际门控的量（§6.1 正名）。
"""

MAX_MODEL_CALLS: int = 12  # 标定后(§5.3)：P90=12 +4buffer，FE-021 flail 靠 §7.2 不加轮
BUDGET_WARNING_THRESHOLD: int = 8
MAX_TOKENS_BUDGET: int = 100_000
MAX_TIME_SECONDS: int = 300

# P1 主动澄清次数上限(bounded,§2.1 不 unlimited)。agent 主动问用户最多 N 次;
# 超限后 route 直奔 END(采纳当前 best-effort),防无限澄清烧钱+收敛模糊。
MAX_CLARIFICATIONS: int = 2

# 图步安全网(必须 > MAX_MODEL_CALLS × 单轮步数,否则 recursion 先于 BudgetGuard 触顶)。
# langchain create_agent 里每个 before_model/after_model 钩子都是独立图节点=1 步:
# 本栈单轮 = ContextElision.bm + BudgetGuard.bm + model + BudgetGuard.am + tools ≈ 5 步
# (并行工具调用每路 +1)。§7.1 加 ContextElision.bm 前是 4 步/轮,旧值 80=20 轮足够;
# 加完变 5 步/轮,80=16 轮恰好不容许第 17 次 before_model(BudgetGuard 在此 fire),
# 导致 RecursionError 抢停、BudgetGuard 永不触发。取充裕值,让 BudgetGuard(16 轮)
# 始终是真正的停止条件,此值仅兜底。改中间件结构(增/删 before_model/after_model)
# 时须重估单轮步数,见 tests/graph/test_recursion_budget.py。
RECURSION_LIMIT: int = 200
