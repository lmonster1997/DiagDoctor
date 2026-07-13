# Doctor 后端模块重组设计

> 创建日期：2026-07-13
> 状态：设计阶段，待实施

---

## 1. 背景与目标

当前 doctor 后端的核心诊断逻辑散落在 `src/graph/`、`src/ingest/`、`src/tools/` 等多个目录下。特别是 Harness（Agent-LLM 诊断循环）的核心关注点——上下文工程、监测预算、工具调用——混合在同一个 `context_engine.py` 文件和 `graph/nodes/diagnosis_agent/` 子目录中，边界模糊。

**目标**：按功能域拆分为清晰的模块，同时为后续以下两个能力预留位置：
- **长期记忆 RAG**：记录优质历史 Bug，RAG 检索相似案例辅助诊断
- **状态持久化（Checkpoint）**：支持诊断中断后恢复，继续问答

---

## 2. 当前架构分析

### 2.1 当前目录结构

```
src/
├── main.py                       # ⚠️ 过重 (~280行)：OTel初始化 + FastAPI工厂 + 路由注册 + CopilotKit兼容层(200行)
├── config.py                     # Settings (Pydantic)
├── llm_factory.py                # LLM 工厂
│
├── api/                          # REST API 路由
│   ├── diagnose.py               # /api/diagnose
│   └── health.py                 # /api/health
│
├── graph/                        # ⚠️ 混合：编排 + 状态 + 上下文工程 + 节点
│   ├── state.py                  # DoctorState + 所有 Pydantic schema
│   ├── context_engine.py         # ⚠️ 上下文工程 (≈500行，4件事混在一起)
│   ├── copilotkit_graph.py       # CopilotKit 2节点 graph
│   └── nodes/
│       ├── bug_info.py           # BugInfo 提取 + auto-prefetch
│       └── diagnosis_agent/
│           ├── subgraph.py       # create_agent 构建 + 缓存 + middleware 注册
│           ├── budget.py         # 预算常量 + 追踪
│           ├── evidence.py       # NormalizedEvidence → HumanMessage 格式化
│           ├── forced_call.py    # 强制结构化输出
│           ├── parsing.py        # LLM 输出 JSON 解析
│           ├── run_context.py    # ContextVar per-invocation 状态
│           └── middleware/
│               ├── agent_lifecycle.py   # 生命周期：预算初始化 + 计数器重置
│               ├── budget_guard.py      # 预算守卫：三维度 cap → jump_to="end"
│               ├── tool_dedup.py        # 工具去重：跳过相同调用
│               ├── tool_truncation.py   # 工具结果截断
│               ├── langfuse_tracing.py  # Langfuse 追踪生命周期
│               └── forced_call.py       # 循环结束后强制 JSON 调用
│
├── ingest/                       # 证据标准化管线
│   ├── normalizer.py             # 9步管线主入口
│   ├── denoiser.py               # 噪声去除
│   ├── deduplicator.py           # 去重
│   ├── correlator.py             # 跨层关联
│   ├── signal_extractor.py       # 黄金信号提取
│   └── tier_aware.py             # 前后端层级标记
│
├── tools/                        # Agent 工具集
│   ├── observability_unified.py  # 🔑 统一可观测查询 (Loki+Tempo)
│   ├── observability_tools.py    # 底层 Loki/Tempo 查询函数
│   ├── trace_query.py            # Span 树构建 + N+1/瓶颈分析
│   ├── code_search.py            # ripgrep 精确代码搜索
│   ├── db_query.py               # 只读 SQL 执行
│   ├── file_reader.py            # 文件内容读取
│   ├── frontend_inspect.py       # 前端错误检查
│   ├── frontend_tools.py         # 浏览器错误解析 + 栈提取
│   └── source_map_resolve.py     # Source map 解析
│
├── prompts/                      # Prompt 模板
│   ├── registry.py               # Jinja2 模板注册
│   └── templates/
│       ├── diagnosis_agent.j2    # 诊断 Agent 系统提示
│       ├── triage.j2             # 分类提示
│       ├── tools_reference.md    # 工具参考文档
│       └── scorers/              # LLM Judge 评分器 Prompt
│
├── security/                     # 安全
│   ├── sanitizer.py              # 路径沙箱 + PII 脱敏
│   ├── secrets.py                # SecretStr 掩码
│   └── sql_guard.py              # SQL 只读守卫
│
└── observability/                # 可观测性
    ├── __init__.py               # OTel 初始化 + FastAPI 插桩
    ├── tracing.py                # @traced 装饰器
    ├── langfuse_tracing.py       # Langfuse 客户端
    ├── cost.py                   # Token 用量 + 成本追踪
    └── logger.py                 # structlog 配置
```

### 2.2 当前问题

| 问题 | 说明 |
|------|------|
| `main.py` 过重 | 280 行承担 4 件事：OTel 初始化、FastAPI 工厂、路由注册、CopilotKit 兼容层（~200 行占 70%），应拆分入口层 |
| `context_engine.py` 职责过载 | 一个文件承载了 token 预算追踪、工具截断、消息降级、动态 prompt 组装、自动压缩 5 件事 |
| `graph/` 目录边界模糊 | 既有状态定义、又有节点、又有上下文引擎，还有 CopilotKit 图——不像一个"图编排"目录 |
| 预算追踪分散两处 | `diagnosis_agent/budget.py`（常量+追踪）+ `middleware/budget_guard.py`（守卫逻辑），逻辑上属于同一关注点 |
| `ingest/` 命名偏技术 | 对外是"证据标准化"，ingest 更多是内部实现细节 |
| ~~`knowledge/`~~ 已删除 | 经全局搜索确认：VectorKB / StructKB / HybridService / Embeddings 从未被任何生产代码 import；`RetrievalRecord` + `retrieval_trace` 字段也未使用。已整体删除（含 `scripts/init_kb.py` + 4 个测试文件）。长期记忆 RAG 将在 `memory/long_term/` 中直接使用 `langchain-qdrant`。 |
| 缺少 checkpoint | `config.py` 中已预留 `checkpoint_db_path`，但未实现 |

---

## 3. 目标模块架构

### 3.1 总览

```
src/
├── main.py                       # 🆕 精简入口 (~20行)：初始化 → 创建app → 注册路由 → 挂载CopilotKit
├── create_app.py                 # 🆕 FastAPI app 工厂：create_app() → FastAPI
├── config.py                     # Settings (不变)
├── llm_factory.py                # LLM 工厂 (不变)
│
├── api/                          # REST 路由
│   ├── routes.py                 # 🆕 集中注册所有 REST 路由
│   ├── diagnose.py
│   └── health.py
│
├── copilotkit/                   # 🆕 CopilotKit 集成 (从 main.py 拆出 ~200行)
│   ├── __init__.py
│   ├── agent.py                  # _DiagDoctorAgent (execute + get_state 智能恢复)
│   ├── middleware.py             # CORS preflight + Info compat + SSE 修正
│   └── mount.py                  # mount_copilotkit(app) 入口
│
├── engine/                       # 🆕 诊断引擎
│   ├── agent.py                  # Agent 构建 + 缓存 + middleware 注册
│   ├── state.py                  # DoctorState + Pydantic schema
│   ├── run_context.py            # ContextVar per-invocation 状态
│   ├── context/                  # 上下文工程
│   │   ├── budget.py             # ContextBudget + token 估算 + phase
│   │   ├── truncation.py         # 工具结果截断
│   │   ├── compaction.py         # 自动压缩 + 历史消息降级
│   │   └── dynamic_prompt.py     # 按 phase 动态组装 system prompt
│   ├── budget/                   # 监测预算
│   │   ├── constants.py          # MAX_TOOL_CALLS / TOKENS / TIME 常量
│   │   ├── tracker.py            # BudgetState + update + is_exceeded
│   │   └── guard.py              # BudgetGuardMiddleware
│   ├── middleware/                # 中间件管线
│   │   ├── lifecycle.py          # AgentLifecycleMiddleware
│   │   ├── tool_dedup.py         # ToolDedupMiddleware
│   │   ├── tool_truncation.py    # ToolTruncationMiddleware
│   │   ├── langfuse_tracing.py   # LangfuseTracingMiddleware
│   │   ├── budget_guard.py       # → 已归入 engine/budget/guard.py
│   │   └── forced_call.py        # ForcedFinalCallMiddleware
│   └── nodes/                    # Graph 节点
│       ├── bug_info.py           # BugInfo 提取 + auto-prefetch
│       └── diagnosis_agent.py    # 诊断节点
│
├── tools/                        # 工具层 (不变)
│   └── ...
│
├── evidence/                     # 🆕 证据采集与标准化
│   ├── normalizer.py             # 9步管线主入口
│   ├── denoiser.py
│   ├── deduplicator.py
│   ├── correlator.py
│   ├── signal_extractor.py
│   ├── tier_aware.py
│   └── formatter.py              # NormalizedEvidence → HumanMessage
│
├── memory/                       # 🆕 长期记忆 RAG
│   └── long_term/
│       ├── case_store.py         # 优质案例入库 → Qdrant（直接使用 langchain-qdrant）
│       ├── case_retriever.py     # RAG 检索相似历史 Bug
│       └── quality_gate.py       # 入库质量门禁
│
├── persistence/                  # 🆕 状态持久化 (Checkpoint)
│   ├── checkpoint_store.py       # LangGraph checkpointer 封装
│   ├── session_manager.py        # 会话管理 (thread_id ↔ checkpoint)
│   └── resume_handler.py         # 恢复逻辑
│
├── prompts/                      # Prompt 模板 (不变)
├── security/                     # 安全 (不变)
└── observability/                # 可观测性 (不变)
```

### 3.2 模块职责矩阵

```
                    ┌──────────┐
                    │   API    │  薄路由层，只做请求/响应
                    └────┬─────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
        ┌─────────┐ ┌───────┐ ┌──────────┐
        │Evidence │ │Engine │ │Persistence│
        │证据采集  │ │诊断引擎│ │状态持久化  │
        └─────────┘ └─┬───┬─┘ └──────────┘
                       │   │
          ┌────────────┘   └────────────┐
          ▼                             ▼
    ┌──────────┐              ┌──────────────┐
    │ Tools    │              │ Memory(LT)   │
    │ 5 工具   │              │ 长期记忆 RAG  │
    └──────────┘              └──────────────┘
                                (直接使用 Qdrant)

    ┌───────────┐
    │  Engine 内部依赖  │
    ├───────────┬───────┤
    │ context/  │ budget/│
    │ 上下文工程 │ 监测预算│
    ├───────────┴───────┤
    │   middleware/     │
    │   中间件管线       │
    ├───────────────────┤
    │     nodes/        │
    │    Graph 节点      │
    └───────────────────┘
```

### 3.3 各模块一句话职责

| 模块 | 比喻 | 职责 |
|------|------|------|
| `main.py` + `create_app.py` | 🚀 启动 | 初始化 OTel → 创建 FastAPI → 生命周期管理 |
| `api/` | 🌐 HTTP 接口 | REST 路由（health + diagnose），薄层不包含业务逻辑 |
| `copilotkit/` | 💬 对话通道 | CopilotKit SDK 集成：Agent 桥接、协议兼容、SSE 流修正 |
| `engine/` | 🧠 大脑 | 诊断循环编排：组装 agent、上下文工程、预算管控、中间件管线、graph 节点 |
| `tools/` | 🦾 手脚 | 5 个可调用工具：observability 查询、代码搜索、DB 查询、文件读取、前端检查 |
| `evidence/` | 👁️ 眼睛 | 证据采集→去噪→去重→信号提取→关联→格式化，输出 NormalizedEvidence |
| `memory/` | 📚 长期经验 | RAG 检索历史优质 Bug 案例，辅助当前诊断（直接使用 langchain-qdrant） |
| `persistence/` | ⏸️ 暂停/恢复 | LangGraph checkpoint 持久化，支持诊断中断后恢复继续问答 |
| `prompts/` | 📝 提示词 | Jinja2 模板管理，system prompt + tools reference |
| `security/` | 🔒 安全 | SQL 只读守卫、路径沙箱、密钥掩码 |
| `observability/` | 📊 可观测 | OTel tracing + Langfuse + cost tracking + 结构化日志 |

---

## 4. 核心模块详细设计

### 4.1 `engine/` — 诊断引擎

这是重构的核心，当前分散在 `graph/` + `context_engine.py` + `diagnosis_agent/budget.py` 中的逻辑全部归拢。

```
engine/
├── agent.py                   # from graph/nodes/diagnosis_agent/subgraph.py
│                               # - build_diagnosis_agent()
│                               # - get_diagnosis_agent() (缓存)
│                               # - _build_system_prompt()
│                               # - _get_llm() / _get_tools()
│
├── state.py                   # from graph/state.py
│                               # - DoctorState (LangGraph TypedDict)
│                               # - Evidence, NormalizedEvidence
│                               # - LogEntry, TraceSpan
│                               # - Signal, Correlation
│                               # - DiagnosisReport, Finding, BudgetState
│
├── run_context.py             # from nodes/diagnosis_agent/run_context.py
│                               # - DiagnosisRunContext dataclass
│                               # - set/get/clear_run_context()
│
├── context/                   # 从 context_engine.py 拆分
│   ├── budget.py              # - ContextBudget dataclass
│   │                           # - estimate_tokens()
│   │                           # - ContextPhase enum
│   ├── truncation.py          # - truncate_tool_result()
│   │                           # - TOOL_CHAR_LIMITS
│   │                           # - _KEY_LINE_PATTERNS
│   ├── compaction.py          # - maybe_compact_context()
│   │                           # - degrade_old_tool_results()
│   └── dynamic_prompt.py      # - build_dynamic_system_prompt(phase)
│
├── budget/                    # 监测预算 (budget.py + budget_guard.py 合并)
│   ├── constants.py           # MAX_TOOL_CALLS=12, MAX_TOKENS_BUDGET=100k,
│   │                           # MAX_TIME_SECONDS=300, BUDGET_WARNING_THRESHOLD=8
│   ├── tracker.py             # BudgetState, update_budget(), is_budget_exceeded()
│   └── guard.py               # BudgetGuardMiddleware
│                               # - abefore_model: 检查三维度 cap → jump_to="end"
│                               # - awrap_tool_call: token 记账
│
├── middleware/                 # 6 个中间件 (从 middleware/ 迁移)
│   ├── lifecycle.py           # AgentLifecycleMiddleware
│   ├── tool_dedup.py          # ToolDedupMiddleware
│   ├── tool_truncation.py     # ToolTruncationMiddleware
│   ├── langfuse_tracing.py    # LangfuseTracingMiddleware
│   ├── budget_guard.py        # → 为空，逻辑归入 engine/budget/guard.py
│   └── forced_call.py         # ForcedFinalCallMiddleware
│
└── nodes/                     # Graph 节点
    ├── bug_info.py            # BugInfo 提取 + auto-prefetch
    └── diagnosis_agent.py     # 诊断节点 (当前在 copilotkit_graph.py 中的
                                # _diagnosis_agent_node 逻辑)
```

**中间件注册顺序**（保持不变）：
```
AgentLifecycle → ToolDedup → LangfuseTracing → ToolTruncation → BudgetGuard → ForcedFinalCall
```

### 4.2 `evidence/` — 证据采集与标准化

从 `ingest/` 升级，名称更直观，同时纳入证据格式化。

```
evidence/
├── normalizer.py              # from ingest/normalizer.py
│                               # ingest() 主入口：raw_evidence → NormalizedEvidence
├── denoiser.py                # 噪声去除（health-check、info 日志过滤）
├── deduplicator.py            # 去重（N+1 重复 SQL 折叠）
├── correlator.py              # 跨层关联（trace_id 链接 frontend→backend→DB）
├── signal_extractor.py        # 黄金信号提取（error stack、error span、slow span）
├── tier_aware.py              # 前后端层级标记
└── formatter.py               # from diagnosis_agent/evidence.py
                                # format_evidence_for_agent() → HumanMessage
```

### 4.3 `memory/` — 长期记忆 RAG

全新模块，为"记录优质历史 Bug + RAG 检索相似案例"设计。直接使用 `langchain-qdrant` 操作 Qdrant，不依赖中间抽象层。

```
memory/
└── long_term/
    ├── case_store.py           # 将诊断报告 + 证据向量化后存入 Qdrant
    │                           # collection: "historical_cases"（已有）
    ├── case_retriever.py       # 当前 bug 特征 → embedding → Qdrant top-K 检索
    │                           # 返回相似案例的 (root_cause, fix_suggestion, evidence)
    └── quality_gate.py         # 案例质量判断：
                                # - confidence ≥ 阈值
                                # - 人工确认标记（可选）
                                # - 去重检查（与已有案例相似度过高则跳过）
```

**集成方式**：
- 诊断开始时，`case_retriever` 检索 Top-3 相似案例
- 将相似案例注入 `diagnosis_agent.j2` system prompt 的 few-shot 段
- 诊断结束后，`quality_gate` 判断是否入库 → `case_store` 写入

### 4.4 `persistence/` — 状态持久化

全新模块，封装 LangGraph checkpoint 机制。不为"记忆"，而是纯状态持久化。

```
persistence/
├── checkpoint_store.py         # 封装 LangGraph checkpointer
│                               # - 开发/测试：SqliteSaver (config.py 中 checkpoint_db_path)
│                               # - 生产：PostgresSaver
│                               # - 工厂函数：get_checkpointer()
├── session_manager.py          # 会话管理
│                               # - create_session(thread_id) → checkpoint
│                               # - list_sessions() → [(thread_id, updated_at, summary)]
│                               # - delete_session(thread_id)
└── resume_handler.py           # 恢复逻辑
                                # - resume_diagnosis(thread_id) → 加载 checkpoint
                                # - 将历史消息注入 agent state
                                # - 继续 ReAct 循环
```

**集成方式**：
- `engine/agent.py` 中的 `create_agent` 编译时传入 `checkpointer`
- `api/diagnose.py` 新增强制接受 `thread_id` 参数（可选，不传则新建）
- 前端 CopilotKit 已天然支持 `thread_id`——传递即可恢复

### 4.5 入口层拆分 — `main.py` / `create_app.py` / `copilotkit/`

当前 `main.py` (~280行) 承担 4 个独立职责，拆分为：

```
main.py                         # 精简入口 (~20行)
├── 初始化 OTel + logging
├── from create_app import create_app
├── from api.routes import register_routes
├── from copilotkit.mount import mount_copilotkit
└── app = create_app(); register_routes(app); mount_copilotkit(app)

create_app.py                   # FastAPI app 工厂 (~50行)
├── lifespan: 预构建 diagnosis agent
├── CORS middleware
├── OTel instrumentation
└── create_app() → FastAPI

api/routes.py                   # 集中路由注册 (~5行)
├── register_routes(app)
├── app.include_router(health_router)
└── app.include_router(diagnose_router)

copilotkit/                     # CopilotKit 集成 (~200行，从 main.py 拆出)
├── agent.py                    # _DiagDoctorAgent 子类
│                               #   - execute(): CopilotKit ↔ LangGraph 桥接
│                               #   - get_state(): 智能恢复（已完成→新建，中断→续接）
├── middleware.py               # 协议兼容中间件
│                               #   - _CorsPreflightMiddleware (OPTIONS preflight)
│                               #   - _CopilotKitInfoCompatMiddleware
│                               #     * GET /info agents 格式改写 (array→object)
│                               #     * GET /threads 空响应兼容
│                               #     * /agent/{name}/connect SSE 握手
│                               #     * SSE content-type 修正
└── mount.py                    # mount_copilotkit(app)
                                #   创建 agent → SDK → 注册中间件 → add_fastapi_endpoint
```

### 4.6 不变模块

| 模块 | 说明 |
|------|------|
| `tools/` | 5 主力工具 + 辅助工具，import 路径不变 |
| `prompts/` | Jinja2 模板，不变 |
| `security/` | 安全工具，不变 |
| `observability/` | OTel + Langfuse + cost + logger，不变 |
| `config.py` | Settings，不变 |
| `llm_factory.py` | LLM 工厂，不变 |

---

## 5. 迁移路径

### 5.1 迁移原则

1. **每次迁移一个模块，保持可运行**（不要一次全改）
2. **先建新目录 + 复制文件 + 更新自身 import → 再更新外部引用 → 最后删除旧文件**
3. **优先迁移被依赖最少的模块**（叶子节点先迁）

### 5.2 推荐顺序

| 阶段 | 模块 | 预估改动 |
|------|------|---------|
| **Phase 0** | ~~删除 `knowledge/`~~ | ✅ 已完成 (2026-07-13) |
| **Phase 0.5** | 拆分 `main.py` | 拆为 `create_app.py` + `copilotkit/` + `api/routes.py`；只影响入口层，不涉及诊断逻辑 |
| **Phase 1** | `evidence/` | 从 `ingest/` 迁出，改名；外部引用少，风险最低 |
| **Phase 2** | `engine/state.py` + `engine/run_context.py` | 从 `graph/state.py` + `run_context.py` 迁出，被大量引用，先迁基础 |
| **Phase 3** | `engine/context/` | 拆分 `context_engine.py` → 4 个子文件 |
| **Phase 4** | `engine/budget/` | `budget.py` + `budget_guard.py` 合并 |
| **Phase 5** | `engine/middleware/` | 从 `diagnosis_agent/middleware/` 迁移，更新 import |
| **Phase 6** | `engine/agent.py` + `engine/nodes/` | 聚合 `subgraph.py` + `copilotkit_graph.py` + `bug_info.py` |
| **Phase 7** | 清理旧目录 | 删除 `graph/`、`ingest/` 等旧路径 |
| **Phase 8** | `memory/` + `persistence/` | 新建模块，实现长期记忆 RAG 和 checkpoint |

### 5.3 外部引用更新清单

迁移完成后需要更新 import 的文件（非 exhaust）：

| 原 import | 新 import |
|-----------|-----------|
| `from src.graph.state import ...` | `from src.engine.state import ...` |
| `from src.graph.context_engine import ...` | `from src.engine.context.budget import ...` 等 |
| `from src.graph.nodes.diagnosis_agent.subgraph import ...` | `from src.engine.agent import ...` |
| `from src.graph.nodes.diagnosis_agent.run_context import ...` | `from src.engine.run_context import ...` |
| `from src.graph.nodes.bug_info import ...` | `from src.engine.nodes.bug_info import ...` |
| `from src.graph.nodes.diagnosis_agent.evidence import ...` | `from src.evidence.formatter import ...` |
| `from src.graph.nodes.diagnosis_agent.budget import ...` | `from src.engine.budget.constants import ...` |
| `from src.graph.nodes.diagnosis_agent.middleware import ...` | `from src.engine.middleware import ...` |
| `from src.ingest.normalizer import ...` | `from src.evidence.normalizer import ...` |
| `from src.graph.copilotkit_graph import ...` | `from src.engine.nodes.diagnosis_agent import ...` |
| *(main.py 拆分)* CopilotKit 兼容代码 | `from src.copilotkit.agent` / `from src.copilotkit.mount` |

受影响的外部文件估算：
- `src/main.py`
- `src/api/diagnose.py`
- `src/tools/observability_unified.py`（引用了 `context_engine.truncate_tool_result`）
- `scripts/` 下所有调试/评测脚本
- `tests/` 下所有测试

---

## 6. 决策记录

| 决策 | 理由 |
|------|------|
| `harness/` → `engine/` | "harness" 是偏技术黑话，`engine`（诊断引擎）更直观 |
| `memory/short_term/` 独立为 `persistence/` | checkpoint 持久化不是"短期记忆"，是纯状态存储与恢复 |
| `ingest/` → `evidence/` | 对外语义更清晰（证据采集 vs 数据摄入） |
| ~~`knowledge/`~~ **删除** | 全局搜索确认：VectorKB / StructKB / HybridService / Embeddings 从未被任何生产代码 import。`RetrievalRecord` + `retrieval_trace` 也未使用。等做 RAG 时在 `memory/long_term/` 直接使用 `langchain-qdrant`，不需要这套中间抽象 |
| `main.py` 拆分为入口层 4 文件 | 280 行承担 4 件事（初始化、工厂、路由、CopilotKit），CopilotKit 兼容层 ~200 行占 70%，独立为 `copilotkit/` 更清晰 |
| `context_engine.py` 拆分为 4 文件 | 500 行单文件承担 5 件事，按职责拆分后每个 < 200 行 |
| middleware 归入 `engine/middleware/` | middleware 是 agent 构建的核心部分，属于引擎层 |
| `budget_guard.py` middleware 逻辑归入 `engine/budget/guard.py` | 它本质是预算守卫，不是独立中间件 |
| 迁移从叶子节点开始 | `evidence/` 被依赖最少，风险最低，先迁移验证流程 |
