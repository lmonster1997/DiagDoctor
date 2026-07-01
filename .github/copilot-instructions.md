# DiagDoctor — AI 辅助编程全局约定

> 此文件会在每次对话中自动注入上下文。保持精简（< 200 行），只放全局适用信息。
> 详细任务见 `docs/diagdoctor-depth-handbook-v2.md`，深度方向见 `docs/diagdoctor-depth-directions-v2.md`。

---

## 项目身份

**DiagDoctor** = 通用 Web 应用 Bug 诊断助手。核心能力：给定出错的 Web 应用 + 错误现象 + 日志/Trace，自动定位根因并给出修复建议。

由 **3 个独立子系统** 组成：

| 子系统 | 路径 | 职责 |
|--------|------|------|
| **demo-app** | `demo-app/` | 被诊断的目标 Web 应用（TaskFlow 任务管理） |
| **bug-factory** | `bug-factory/` | Bug 生成与注入工厂 |
| **doctor** | `doctor/` | 诊断 Agent 主体（LangGraph + RAG） |

---

## 技术栈速查

### Python（demo-app/backend + doctor + bug-factory + benchmark）
```yaml
version: "3.11+"
package_manager: uv
formatter: ruff format
linter: ruff check
# mypy strict on core logic modules (ingest, tools/, evaluators/, security/, schema).
# Graph orchestration layer (graph/nodes, subgraphs) relaxed to non-strict.
# Third-party libs (LangGraph/LangChain) use ignore_missing_imports=true.
# Coverage ≥80% only for core logic; overall repo ≥60% suffices.
type_checker: mypy --strict  # core modules only; graph layer relaxed (B2 policy)
test: pytest + pytest-asyncio
framework: FastAPI + Pydantic v2 + SQLAlchemy 2.x + Alembic
observability: OpenTelemetry + structlog
# Doctor LLM 可观测 + 评测：Langfuse（自托管）
agent_framework: LangGraph
vector_db: Qdrant
```

### TypeScript（demo-app/frontend + doctor UI）
```yaml
version: "5.x"
package_manager: pnpm
framework: React 18 + Vite
ui: shadcn/ui + Tailwind CSS
state: Zustand
data_fetching: TanStack Query
e2e: Playwright
```

### 基础设施
```yaml
database: PostgreSQL 16
cache: Redis 7
observability: Loki + Tempo + Grafana + OpenTelemetry Collector
# Doctor LLM 评测 + 追踪：Langfuse（自托管，langfuse-langchain）
deploy: Docker Compose（Dev/Demo）→ K8s + Helm（Prod）
ci: GitHub Actions
```

---

## 命名规范

| 语言 | 类型 | 规范 | 示例 |
|------|------|------|------|
| Python | 文件 | `snake_case` | `task_service.py` |
| Python | 类 | `PascalCase` | `TaskService` |
| Python | 函数/变量 | `snake_case` | `get_task_by_id` |
| TypeScript | 组件文件 | `PascalCase` | `TaskBoard.tsx` |
| TypeScript | 工具/服务文件 | `kebab-case` | `api-client.ts` |
| TypeScript | 组件 | `PascalCase` | `TaskCard` |
| TypeScript | 函数/变量 | `camelCase` | `fetchTasks` |

### Commit 规范（Conventional Commits）
```
feat(scope): description    # 新功能
fix(scope): description     # 修复
docs: description           # 文档
chore: description          # 杂务
refactor(scope): description # 重构
test(scope): description    # 测试
```

Scope: `doctor`, `demo-app`, `bug-factory`, `benchmark`, `infra`

---

## 项目结构速查

```
DiagDoctor/
├── demo-app/                  # 被诊断系统
│   ├── backend/               # FastAPI（TaskFlow API）
│   │   ├── app/
│   │   │   ├── main.py        # FastAPI 入口
│   │   │   ├── config.py      # Pydantic Settings
│   │   │   ├── database.py    # SQLAlchemy async session
│   │   │   ├── observability.py # OTel 初始化
│   │   │   ├── models/        # SQLAlchemy 模型
│   │   │   ├── schemas/       # Pydantic schema
│   │   │   ├── api/           # 路由（projects, tasks, comments, auth）
│   │   │   ├── services/      # 业务逻辑
│   │   │   └── auth/          # JWT 认证
│   │   ├── alembic/           # 数据库迁移
│   │   └── tests/
│   └── frontend/              # React + shadcn/ui + Vite
│       └── src/
│           ├── components/    # 组件（含 ui/ shadcn 组件）
│           ├── pages/         # 页面
│           ├── stores/        # Zustand stores
│           ├── services/      # API 调用层
│           ├── observability/ # OTel-JS 遥测通道
│           └── types/         # TypeScript 类型
├── bug-factory/               # Bug 生成系统 ✅ 已实现
│   ├── recipes/               # Bug 配方 YAML（28 个）
│   └── src/                   # injector, trigger, evidence collector, case generator
├── doctor/                    # 诊断 Agent ✅ V3 基线已实现
│   └── src/                   # LangGraph + RAG + FastAPI（ingest→unified_agent→reporter）
├── benchmark/                 # 评测系统（已迁移至 Langfuse，仅保留导入脚本）
├── infra/                     # 部署配置
│   ├── docker-compose.yml
│   ├── otel/collector.yaml
│   └── postgres/init-db.sql
├── docs/                      # 设计文档（权威：depth-handbook-v2 + depth-directions-v2）
└── scripts/                   # 辅助脚本
```

---

## 权威端口表 & dev origin

> 全文凡涉及端口/CORS/服务地址，**一律以本表为准**。

| 服务 | 容器内端口 | 宿主映射 | 备注 |
|------|-----------|---------|------|
| demo-frontend（nginx） | 80 | 3000 | 生产形态；反代 `/v1/traces`、`/v1/logs` 到 collector |
| demo-frontend（Vite dev） | 5173 | 5173 | 本地开发；直连 collector(4318) |
| demo-backend | 8000 | 8000 | FastAPI |
| doctor-api | 8000 | **8001** | 避免与 demo-backend 宿主 8000 冲突 |
| postgres | 5432 | 5432 | — |
| redis | 6379 | 6379 | — |
| otel-collector | 4317(gRPC) / 4318(HTTP) | 4317/4318 | HTTP 收浏览器上报，CORS 配 `localhost:5173,localhost:3000` |
| loki | 3100 | 3100 | 3.x，OTLP 入口 `/otlp` |
| tempo | 3200 / 4319(OTLP) | 3200 / 4319 | 2.6 |
| grafana | 3000 | 3001 | admin/admin |
| qdrant | 6333/6334 | 6333/6334 | — |
| langfuse-server | 3000 | **3002** (IPv4 only) | 自托管，LLM 可观测 & 评测。⚠ 用 `http://127.0.0.1:3002` 不要用 `localhost` |
| langfuse-postgres | 5432 | 5433 | Langfuse 专用 DB |

---

## 全局约束

1. **所有 Python 代码** 必须：async 优先、完整 type hints、Pydantic v2 语法
2. **所有前端组件** 使用 shadcn/ui（已有组件在 `src/components/ui/`），不要引入其他 UI 库
3. **环境变量** 通过 Pydantic Settings 管理，不要硬编码配置
4. **错误处理** 后端用结构化日志（structlog），前端用 ErrorBoundary
5. **API 设计** RESTful，JWT 认证，OpenAPI 文档自动生成
6. **测试** 后端 pytest + pytest-asyncio，前端 Vitest，E2E Playwright
7. **不要修改已有数据库迁移文件**，新增迁移用 `alembic revision -m "description"`
8. **Docker Compose** 是唯一的一键启动方式，新服务必须加入编排
9. **评测门禁** 以 `overall` 指标为准，通过 Langfuse Experiment + CI 门禁执行

---

## 当前开发阶段

**V3 基线 ✅ 已实现**（3 节点：ingest → unified_agent → reporter）

**架构要点**：
- **Ingest**：两阶段（① auto-prefetch 并行采集 Loki/Tempo 后端+前端数据 → ② 9 步标准化管线），输出 `NormalizedEvidence`
- **UnifiedAgent**：手动 ReAct 循环 + 5 工具，纯 LLM 诊断（不负责数据获取）
- **前端错误双通道**：`console.error` → Loki、`window.onerror` → Tempo（client_error span）
- **search_observability** 支持 `include_frontend=True` 查询前端 client_error span

**已实现**：
- [x] Demo App 前后端骨架（TaskFlow）
- [x] OpenTelemetry 集成（后端 + 前端 OTel-JS + console.error 拦截）
- [x] 可观测性栈（Loki/Tempo/Grafana/Collector）
- [x] Doctor Agent（LangGraph 手动循环 + 5 工具 + RAG + SQL 只读守卫）
- [x] Bug Factory（28 配方 + injector + trigger + evidence + case generator）
- [x] 评测体系已迁移至 Langfuse（自托管，替代自研 benchmark）
- [x] Ingest auto-prefetch（后端+前端并行查询）
- [x] 前端错误实时采集（双通道：console.error → Loki / onerror → Tempo）

**当前 Phase：深度化（见执行手册）**
- Phase 0（2d）：Langfuse 部署 + 基线验证
- Phase 1（10d）：手动循环 + 上下文工程 + Ingest/search 深度
- Phase 2（13d）：Langfuse 评测体系 + Prompt 策略 + TodoWrite + Bug 扩展
- Phase 3（11d）：安全 + 自省 + Hook + Subagent

> 详细任务卡片见 `docs/diagdoctor-depth-handbook-v2.md`
> 14 个深度方向见 `docs/diagdoctor-depth-directions-v2.md`

---

## 关键设计文档索引

| 文档 | 路径 | 何时使用 |
|------|------|---------|
| **执行手册（权威）** | `docs/diagdoctor-depth-handbook-v2.md` | 逐日任务卡片、当前状态、Phase 规划 |
| **深度方向（权威）** | `docs/diagdoctor-depth-directions-v2.md` | 14 个方向的方案细节与代码级改动 |
| Bug 配方规范 | `docs/bug-authoring-and-observability-guide.md` | 编写新 Bug 配方 |
| Case 质量审查 | `docs/bug-case-quality-review-and-improvements.md` | Bug case 质量参考 |
| Docker 排错 | `docs/docker-network-fixes.md` | Docker 网络问题 |
| AI 编程技巧 | `docs/ai-assisted-dev-tips.md` | AI 辅助编程最佳实践 |
| mini-swe-agent 分析 | `docs/mini-swe-agent-architecture-analysis.md` | Agent 架构参考 |

> ⚠️ 以下文档已删除（过时/冲突）：`_V1.md` / `_V3.md` / `execution-handbook_V1,V2,V3.md` / `architecture-diff-and-changes.md`。
> 唯一权威来源：`depth-handbook-v2.md` + `depth-directions-v2.md`。
