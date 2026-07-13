"""DiagDoctor 诊断引擎 — Agent-LLM 核心编排。

包含：
- agent.py: Agent 构建 + 缓存 + middleware 注册
- state.py: DoctorState + Pydantic schema
- run_context.py: ContextVar per-invocation 状态
- context/: 上下文工程（预算追踪、截断、压缩、动态 prompt）
- budget/: 监测预算（常量、追踪、守卫中间件）
- middleware/: 中间件管线（6 个）
- nodes/: Graph 节点（bug_info、diagnosis_agent）
"""
