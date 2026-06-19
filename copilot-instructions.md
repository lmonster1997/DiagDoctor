# DiagDoctor — AI 辅助编程全局约定

> 此文件会在每次对话中自动注入上下文。保持精简（< 200 行），只放全局适用信息。
> 详细架构见 `docs/diagdoctor-from-scratch.md`，逐日任务见 `docs/diagdoctor-execution-handbook.md`。

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
type_checker: mypy --strict
test: pytest + pytest-asyncio
framework: FastAPI + Pydantic v2 + SQLAlchemy 2.x + Alembic
observability: OpenTelemetry + structlog
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
│           └── types/         # TypeScript 类型
├── bug-factory/               # Bug 生成系统（尚未实现）
│   ├── recipes/               # Bug 配方 YAML
│   └── src/                   # injector, trigger, evidence collector
├── doctor/                    # 诊断 Agent（尚未实现）
│   └── src/                   # LangGraph + RAG + FastAPI
├── benchmark/                 # 评测系统（尚未实现）
├── infra/                     # 部署配置
│   ├── docker-compose.yml
│   ├── otel/collector.yaml
│   └── postgres/init-db.sql
├── docs/                      # 设计文档
└── scripts/                   # 辅助脚本
```

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

---

## 当前开发阶段

**Sprint 1（W1-W2）：基础设施**
- [x] Demo App 前后端骨架（TaskFlow）
- [x] 数据库模型 + 迁移
- [x] JWT 认证
- [x] OpenTelemetry 集成
- [x] Docker Compose 编排
- [ ] 可观测性栈（Loki/Tempo/Grafana）
- [ ] Doctor 项目骨架
- [ ] LangGraph 基础设施

> 详细逐日任务见 `docs/diagdoctor-execution-handbook.md`

---

## 关键设计文档索引

| 文档 | 路径 | 何时使用 |
|------|------|---------|
| 架构总览 | `docs/diagdoctor-from-scratch.md` | 架构讨论、重大决策 |
| 执行手册 | `docs/diagdoctor-execution-handbook.md` | 逐日开发任务 |
| Docker 排错 | `docs/docker-network-fixes.md` | Docker 网络问题 |
| AI 编程技巧 | `docs/ai-assisted-dev-tips.md` | AI 辅助编程最佳实践 |
