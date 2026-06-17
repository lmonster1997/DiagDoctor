# DiagDoctor 详细执行手册（8 周逐日开发指南）

> 文档日期：2026-06-17  
> 配套文档：[diagdoctor-from-scratch.md](diagdoctor-from-scratch.md)（架构与方案）  
> 文档定位：**面向 AI 弱模型的、可直接执行的开发手册**
> 
> **使用方式**：把每个任务卡片直接喂给 AI 模型（Cursor / Copilot / Claude），按提示词与验收标准执行即可。

---

## 总览：8 周 = 4 Sprint = 40 个工作日

| Sprint | 周次 | 工作日 | 阶段目标 |
|--------|------|------|---------|
| S1 | W1-W2 | D1-D10 | 基础设施：demo-app + 可观测性栈 + Doctor 骨架 |
| S2 | W3-W4 | D11-D20 | Bug Factory + Harness 评测雏形 |
| S3 | W5-W6 | D21-D30 | 多 Agent 系统完整实现 |
| S4 | W7-W8 | D31-D40 | 代码定位 + 部署 + 演示物料 |

每个任务卡片包含：
- **目标**：本次任务要达成的成果
- **前置**：依赖的前序任务
- **AI 提示词**：可直接复制给 AI 的 Prompt 模板
- **验收**：如何确认任务完成
- **常见坑**：弱模型容易出错的地方

---

## 通用约定

### 全局技术规范

所有代码必须遵守：

```yaml
Python:
  version: "3.11+"
  package_manager: uv
  formatter: ruff format
  linter: ruff check
  type_checker: mypy --strict
  test: pytest
  
TypeScript:
  version: "5.x"
  package_manager: pnpm
  framework: React 18 + Vite
  ui: shadcn/ui + Tailwind
  
Naming:
  python_files: snake_case
  python_classes: PascalCase
  python_functions: snake_case
  typescript_files: PascalCase for components, kebab-case for others
  
Commit:
  style: Conventional Commits
  examples:
    - "feat(doctor): add triage agent"
    - "fix(demo-app): correct task assignment logic"
    - "docs: update architecture diagram"
```

### 仓库初始化命令（D1 第一件事）

```powershell
# 创建项目根目录
New-Item -ItemType Directory -Path DiagDoctor; Set-Location DiagDoctor
git init
git branch -M main

# 创建顶层结构
mkdir demo-app, bug-factory, doctor, benchmark, infra, scripts, docs
mkdir '.github/workflows'

# 顶层 README + LICENSE + .gitignore
'# DiagDoctor' | Set-Content README.md
'MIT License' | Set-Content LICENSE
Invoke-WebRequest -Uri 'https://www.toptal.com/developers/gitignore/api/python,node,vscode' -OutFile .gitignore

# Workspace pyproject.toml（用 uv workspace）
@'
[tool.uv.workspace]
members = ["doctor", "bug-factory", "benchmark"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.mypy]
python_version = "3.11"
strict = true
'@ | Set-Content pyproject.toml

# 首次提交
git add .; git commit -m "chore: initial project skeleton"
```

---

# Sprint 1：基础设施（D1-D10）

## Week 1：Demo App + 可观测性栈

### D1（周一）：项目初始化 + Demo App 后端骨架

#### 任务 1.1：仓库初始化（上午 2 小时）

按上面 [仓库初始化命令](#仓库初始化命令d1-第一件事) 执行。

#### 任务 1.2：Demo App 后端骨架生成（下午 6 小时）

**AI 提示词：**

> 我要在 `demo-app/backend/` 下创建一个 FastAPI 项目，作为 TaskFlow 任务管理应用的后端。请生成以下内容：
> 
> 1. `pyproject.toml`，依赖包括：fastapi、uvicorn、sqlalchemy[asyncio]、asyncpg、alembic、pydantic、pydantic-settings、python-jose（JWT）、passlib[bcrypt]、structlog、opentelemetry-api、opentelemetry-sdk、opentelemetry-instrumentation-fastapi、opentelemetry-instrumentation-sqlalchemy、opentelemetry-exporter-otlp
> 2. 项目结构：
>    ```
>    backend/
>    ├── app/
>    │   ├── __init__.py
>    │   ├── main.py              # FastAPI 入口
>    │   ├── config.py            # Pydantic Settings
>    │   ├── database.py          # SQLAlchemy 异步 session
>    │   ├── observability.py     # OTel 初始化
>    │   ├── models/              # SQLAlchemy 模型
>    │   ├── schemas/             # Pydantic schema
>    │   ├── api/                 # 路由
>    │   ├── services/            # 业务逻辑
>    │   └── auth/                # 认证
>    ├── alembic/
>    ├── alembic.ini
>    └── tests/
>    ```
> 3. 最小可运行：`uvicorn app.main:app` 能启动，`GET /health` 返回 `{"status":"ok"}`
> 4. OTel 集成：启动时初始化，发送到 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境变量
> 
> 暂时不需要业务路由，下一步再加。代码要用 Python 3.11+ 语法，全部 type hints，async 优先。

**验收标准：**

```powershell
Set-Location demo-app/backend
uv sync
uv run uvicorn app.main:app --reload
# 另开终端测试：
curl.exe http://localhost:8000/health  # 应返回 {"status":"ok"}
```

**常见坑：**
- 弱模型可能用 SQLAlchemy 1.x 的同步语法，必须强调"用 2.x async 语法"
- OTel 初始化代码顺序很重要，必须在 FastAPI 实例化之前

---

### D2（周二）：Demo App 数据模型 + 业务接口

#### 任务 2.1：数据模型设计（上午 3 小时）

**AI 提示词：**

> 在 `demo-app/backend/app/models/` 下创建 SQLAlchemy 2.x 异步模型，包含以下表（使用 `AsyncAttrs` + `DeclarativeBase`）：
> 
> 1. **User**：id (UUID)、email (unique)、hashed_password、display_name、created_at、is_active
> 2. **Project**：id (UUID)、name、description、owner_id (FK to User)、created_at
> 3. **Task**：id (UUID)、project_id (FK)、title、description、status (enum: todo/doing/done)、priority (int)、assignee_id (FK to User, nullable)、due_date、created_at、updated_at
> 4. **Comment**：id (UUID)、task_id (FK)、author_id (FK to User)、content、created_at
> 5. **Tag**：id、name、color；**TaskTag** 多对多关联表
> 
> 同时在 `schemas/` 下生成对应的 Pydantic v2 schema（Create / Update / Response 三类）。
> 
> 然后初始化 Alembic 并生成首次迁移（`alembic init alembic`、修改 `alembic/env.py` 用异步引擎、运行 `alembic revision --autogenerate -m "initial"`）。

**验收：**
- `alembic upgrade head` 在本地 Postgres 上跑通
- 所有模型有 `__repr__` 方法
- Pydantic schema 有 `model_config = ConfigDict(from_attributes=True)`

#### 任务 2.2：业务 API 实现（下午 5 小时）

**AI 提示词：**

> 在 `demo-app/backend/app/api/` 下实现 REST API，**按以下规范**：
> 
> 1. 认证模块（`auth.py`）：
>    - `POST /api/auth/register`：注册
>    - `POST /api/auth/login`：登录返回 JWT
>    - 一个 `get_current_user` Depends，验证 JWT
> 
> 2. 项目模块（`projects.py`）：标准 CRUD `/api/projects/`
> 
> 3. 任务模块（`tasks.py`）：
>    - `GET /api/projects/{pid}/tasks` 列表
>    - `POST /api/projects/{pid}/tasks` 创建
>    - `PATCH /api/tasks/{tid}` 更新
>    - `DELETE /api/tasks/{tid}` 删除
>    - `GET /api/tasks/{tid}` 详情（包含评论列表 + 标签）
> 
> 4. 评论模块（`comments.py`）：
>    - `POST /api/tasks/{tid}/comments`
> 
> **重要：** 每个端点都必须：
> - 用 `async def`
> - 用 SQLAlchemy 2.x `select()` 语法
> - 输入用 Pydantic schema 校验
> - 返回用 Pydantic Response schema
> - 错误用 HTTPException 抛出
> - 包含 docstring，写明用途
> 
> **注意：** 在 `GET /api/projects/{pid}/tasks` 中**正常实现**（用 `selectinload(Task.comments)` 预加载评论数量），这是"健康版"代码，后续 bug-factory 会把它改坏。

**验收：**
- 启动后端，访问 `http://localhost:8000/docs` 看到 Swagger
- 手动测试：注册 → 登录 → 拿 token → 创建项目 → 创建任务，全流程通

---

### D3（周三）：Demo App 前端骨架

#### 任务 3.1：前端项目初始化（上午 2 小时）

```powershell
cd demo-app
pnpm create vite frontend --template react-ts
cd frontend
pnpm install
pnpm add @tanstack/react-query zustand axios react-router-dom
pnpm add -D @types/node tailwindcss postcss autoprefixer
pnpm dlx shadcn@latest init
```

#### 任务 3.2：前端实现（下午 6 小时）

**AI 提示词：**

> 在 `demo-app/frontend/src/` 下实现 React + TypeScript 前端，使用 shadcn/ui 组件库：
> 
> 1. 路由（`react-router-dom v6`）：
>    - `/login`、`/register`、`/projects`、`/projects/:id`（任务看板）、`/tasks/:id`（详情）
> 
> 2. 状态管理：
>    - `stores/authStore.ts`：用 zustand 管理 token + currentUser，localStorage 持久化
>    - `services/api.ts`：axios 实例，自动注入 token，401 自动跳转登录
> 
> 3. 页面组件：
>    - LoginPage / RegisterPage：表单提交
>    - ProjectsPage：项目列表 + 新建
>    - TaskBoardPage：3 列看板（todo/doing/done），用 `@dnd-kit/core` 支持拖拽改状态
>    - TaskDetailPage：任务详情 + 评论
> 
> 4. 集成 Sentry SDK（用 `@sentry/react`），DSN 从环境变量读取，未配置时不报错
> 
> 5. 错误边界：根组件包裹 ErrorBoundary，捕获渲染错误
> 
> 6. **重要：在 console.error/console.warn 处加上结构化标识**，例如：
>    ```ts
>    console.error('[TASK_LOAD_FAIL]', { taskId, error: error.message });
>    ```
>    便于后续 Doctor 解析。

**验收：**
- `pnpm dev` 启动
- 完整跑通：注册 → 登录 → 创建项目 → 添加任务 → 拖拽改状态 → 添加评论

---

### D4（周四）：Docker 化 demo-app + 数据库

#### 任务 4.1：Dockerfile（上午 2 小时）

**AI 提示词：**

> 在 `demo-app/` 下创建两个 Dockerfile：
> 
> 1. `backend/Dockerfile`：多阶段构建
>    - 基础镜像：`python:3.11-slim`
>    - 使用 uv 装依赖（先 copy pyproject.toml + uv.lock，再 copy 源码）
>    - 暴露 8000 端口
>    - 启动命令：`uvicorn app.main:app --host 0.0.0.0 --port 8000`
> 
> 2. `frontend/Dockerfile`：多阶段构建
>    - 构建阶段：`node:20-alpine` + `pnpm build`
>    - 运行阶段：`nginx:alpine` 服务 dist 目录
>    - 添加 nginx.conf：API 请求 proxy 到 `http://demo-backend:8000`
> 
> 同时创建：
> - `demo-app/.dockerignore`：忽略 node_modules、__pycache__、.venv、dist
> - `demo-app/.env.example`：示例环境变量
> 
> Dockerfile 必须用 layer cache 友好的写法（先 copy 依赖文件，再 copy 源码）。

#### 任务 4.2：根 docker-compose.yml（下午 3 小时）

在项目根创建 `docker-compose.yml`，包含：

**AI 提示词：**

> 在项目根创建 `docker-compose.yml`（version 3.9），包含以下服务：
> 
> 1. `postgres`：postgres:16，volume 持久化，初始化脚本创建 `taskflow` 数据库
> 2. `redis`：redis:7，开启 AOF
> 3. `demo-backend`：构建自 `./demo-app/backend`，依赖 postgres + redis + otel-collector，环境变量包括 DATABASE_URL、JWT_SECRET、OTEL_EXPORTER_OTLP_ENDPOINT
> 4. `demo-frontend`：构建自 `./demo-app/frontend`，端口 3000:80
> 
> 写一个 `Makefile`（或 `justfile`），提供：
> - `make up` / `make down` / `make logs` / `make ps`
> - `make demo-migrate`：在 demo-backend 容器内跑 alembic upgrade
> - `make demo-seed`：种入 demo 数据（一个 admin 用户 + 一个示例项目）

#### 任务 4.3：数据库种子脚本（下午 3 小时）

**AI 提示词：**

> 在 `demo-app/backend/scripts/seed.py` 创建数据种入脚本：
> - 创建用户：`admin@example.com / Admin123!`
> - 创建用户：`alice@example.com / Alice123!`
> - 创建项目：`Demo Project`（owner=admin）
> - 在该项目下创建 30 个任务，分散在 todo/doing/done 状态，部分指派给 alice，部分有评论
> 
> 脚本必须幂等（重复跑不出错），用 SQLAlchemy 2.x async session。

**验收：**
- 新机器执行 `git clone; make up; make demo-migrate; make demo-seed`，整个 demo-app 跑起来
- 浏览器访问 http://localhost:3000 看到完整应用

---

### D5（周五）：可观测性栈搭建

#### 任务 5.1：OpenTelemetry Collector + Loki + Tempo + Grafana（全天）

**AI 提示词：**

> 在 `infra/` 下创建可观测性栈配置：
> 
> 1. `infra/otel/collector.yaml`：OpenTelemetry Collector 配置
>    - receivers: otlp (grpc + http)
>    - exporters: loki (for logs)、otlp/tempo (for traces)、debug
>    - processors: batch、attributes（添加 service.name）
>    - service pipelines: traces → tempo; logs → loki
> 
> 2. `infra/loki/config.yaml`：Loki 单实例配置（filesystem 存储）
> 
> 3. `infra/tempo/config.yaml`：Tempo 单实例配置（filesystem 存储 + OTLP receiver）
> 
> 4. `infra/grafana/`：
>    - `provisioning/datasources/datasources.yaml`：自动配置 Loki + Tempo 数据源
>    - `provisioning/dashboards/dashboards.yaml`：加载 dashboard
>    - `dashboards/demo-app.json`：一个简单的 dashboard 展示 demo-app 的请求量 + 错误率
> 
> 5. 在根 `docker-compose.yml` 新增 services：
>    - `otel-collector`（端口 4317、4318）
>    - `loki`（端口 3100）
>    - `tempo`（端口 3200、4319 用于 OTLP）
>    - `grafana`（端口 3001）

**验收：**

```powershell
make up
# 浏览器访问 http://localhost:3001（admin/admin）
# 进入 Explore，选择 Loki 数据源，查询 {service_name="demo-backend"}
# 应该能看到 demo-backend 输出的日志
# 切换到 Tempo，能搜索到 trace
```

**常见坑：**
- OTel Collector 配置文件 yaml 缩进易错，弱模型常生成无效配置
- Loki 的 promtail 收集配置在不同版本写法不同，建议直接用 OTel Collector → Loki 这条路径
- Tempo 的 OTLP receiver 端口要与 OTel Collector 的 exporter 配对

---

## Week 2：Doctor 项目骨架 + LangGraph 基础

### D6（周一）：Doctor 后端骨架

#### 任务 6.1：Doctor 项目初始化（上午 3 小时）

**AI 提示词：**

> 在 `doctor/` 下创建 Doctor 项目骨架，技术栈与 demo-app 后端类似但是诊断 Agent 主体：
> 
> 1. `pyproject.toml` 依赖：
>    - fastapi、uvicorn、pydantic-settings
>    - langgraph、langchain-core、langchain-openai
>    - langchain-community（用于一些工具）
>    - qdrant-client、langchain-qdrant
>    - sentence-transformers 或 langchain-huggingface（Embedding）
>    - structlog、tenacity、aiocache、aiohttp
>    - jinja2（Prompt 模板）
>    - opentelemetry-api、opentelemetry-instrumentation-fastapi
> 
> 2. 项目结构：
>    ```
>    doctor/
>    ├── src/
>    │   ├── __init__.py
>    │   ├── main.py
>    │   ├── api/
>    │   │   ├── __init__.py
>    │   │   ├── health.py
>    │   │   └── diagnose.py
>    │   ├── config.py
>    │   ├── observability.py
>    │   ├── graph/
>    │   │   ├── state.py
>    │   │   ├── main_graph.py
>    │   │   ├── nodes/
>    │   │   └── subgraphs/
>    │   ├── tools/
>    │   ├── knowledge/
>    │   ├── prompts/
>    │   │   ├── templates/
>    │   │   └── registry.py
>    │   ├── observability/
>    │   │   ├── logger.py
>    │   │   ├── cost.py
>    │   │   └── tracing.py
>    │   └── security/
>    ├── tests/
>    └── Dockerfile
>    ```
> 
> 3. 实现 Hello World 接口：
>    - `GET /health` → `{"status":"ok"}`
>    - `POST /api/diagnose` 接收 `{"user_report": "..."}`，暂时调用 LLM 返回响应（用 langchain_openai.ChatOpenAI）
> 
> 4. Config 类用 pydantic-settings，从 .env 读取：LLM_API_KEY、LLM_BASE_URL、LLM_MODEL

**验收：**
- `cd doctor; uv sync; uv run uvicorn src.main:app --reload`
- `curl.exe -X POST http://localhost:8000/api/diagnose -d '{"user_report":"测试"}' -H 'Content-Type: application/json'` 返回 LLM 响应

#### 任务 6.2：基础设施 wire-up（下午 5 小时）

**AI 提示词：**

> 在 `doctor/src/` 下完善基础设施模块：
> 
> 1. `observability/logger.py`：基于 structlog 的日志器，包含：
>    - JSON 格式输出
>    - 自动添加 trace_id、session_id（从 contextvars 读取）
>    - 时间戳 ISO 格式
> 
> 2. `observability/cost.py`：TokenAccountant 类
>    - `record(model: str, prompt_tokens: int, completion_tokens: int, cost_usd: float = 0)`
>    - `get_summary() -> dict`：按 model 汇总
>    - 用 contextvars 维护当前 session 的 accountant
> 
> 3. `observability/tracing.py`：
>    - 初始化 OpenTelemetry，向 collector 上报
>    - 提供 `@traced` 装饰器，自动给函数加 span
> 
> 4. `security/sanitizer.py`：
>    - `sanitize_path(user_input: str, allowed_roots: list[Path]) -> Path`：路径沙箱
>    - `safe_subprocess_args(args: list[str]) -> list[str]`：参数校验
>    - `sanitize_for_llm(text: str) -> str`：脱敏（手机号、邮箱、IP 等）
> 
> 5. `security/secrets.py`：
>    - 强制要求 SecretStr 包装的字段
>    - `mask(secret: SecretStr) -> str`：仅显示前 3 后 3 字符
> 
> 每个模块必须有对应的单元测试在 `tests/` 下，覆盖率 ≥ 80%。

**验收：**
- `uv run pytest` 通过
- `uv run mypy src/` 无错误

---

### D7（周二）：LangGraph 第一个 Graph

#### 任务 7.1：State 定义（上午 2 小时）

**AI 提示词：**

> 在 `doctor/src/graph/state.py` 定义 LangGraph 的 State：
> 
> ```python
> from typing import TypedDict, Annotated, Literal, Optional
> from operator import add
> from langgraph.graph.message import add_messages
> from pydantic import BaseModel
> from datetime import datetime
> 
> # ---- 子模型 ----
> class LogEntry(BaseModel):
>     timestamp: datetime
>     level: str
>     service: str
>     message: str
>     trace_id: Optional[str] = None
>     attributes: dict = {}
> 
> class TraceSpan(BaseModel):
>     span_id: str
>     parent_id: Optional[str]
>     name: str
>     service: str
>     start: datetime
>     duration_ms: float
>     attributes: dict = {}
>     status: Literal["ok", "error", "unset"] = "unset"
> 
> class Evidence(TypedDict, total=False):
>     user_report: str
>     logs: list[LogEntry]
>     traces: list[TraceSpan]
>     error_screenshot_url: Optional[str]
>     request: Optional[dict]
>     response: Optional[dict]
> 
> class Finding(BaseModel):
>     agent: str
>     summary: str
>     evidence_refs: list[str]
>     confidence: float
> 
> class Hypothesis(BaseModel):
>     summary: str
>     confidence: float
>     affected_files: list[str]
>     proposed_by: str
> 
> class DiagnosisReport(BaseModel):
>     bug_category: str
>     root_cause: str
>     affected_file: Optional[str]
>     affected_line: Optional[int]
>     fix_suggestion: str
>     evidence_chain: list[str]
>     confidence: float
> 
> # ---- 主 State ----
> class DoctorState(TypedDict):
>     # 输入
>     evidence: Evidence
>     case_id: Optional[str]
>     
>     # Triage 结果
>     bug_category: Optional[Literal[
>         "frontend_crash", "backend_error",
>         "performance", "logic", "data", "config"
>     ]]
>     
>     # 累积发现
>     findings: Annotated[list[Finding], add]
>     hypotheses: Annotated[list[Hypothesis], add]
>     
>     # 最终报告
>     report: Optional[DiagnosisReport]
>     
>     # 消息历史
>     messages: Annotated[list, add_messages]
>     
>     # 元数据
>     trace_id: str
> ```
> 
> 注意：使用 Python 3.11+ 语法，所有 BaseModel 用 Pydantic v2。

#### 任务 7.2：第一个可运行的 Graph（下午 6 小时）

**AI 提示词：**

> 在 `doctor/src/graph/main_graph.py` 实现最小可运行的 LangGraph：
> 
> 1. 一个节点 `dummy_triage_node`：
>    - 接收 State
>    - 调用 LLM 判断 bug_category（用结构化输出）
>    - 返回 `{"bug_category": "...", "messages": [...]}`
> 
> 2. 一个节点 `dummy_reporter_node`：
>    - 基于 bug_category 生成一个固定的诊断报告
>    - 返回 `{"report": DiagnosisReport(...)}`
> 
> 3. 主图：START → dummy_triage_node → dummy_reporter_node → END
> 
> 4. 使用 `SqliteSaver` 作为 Checkpointer，db 文件路径从 config 读取
> 
> 5. 在 `api/diagnose.py` 中：
>    - `POST /api/diagnose` 接收 Evidence
>    - 调用 graph，用 thread_id（来自请求或自动生成）
>    - 返回 final state 中的 report
>    - 支持 `stream=true` query 参数，用 `astream_events("v2")` 流式返回

**验收：**

```powershell
curl.exe -X POST http://localhost:8000/api/diagnose `
  -H "Content-Type: application/json" `
  -d '{
    "evidence": {
      "user_report": "登录后页面崩溃",
      "logs": [],
      "traces": []
    }
  }'
# 应返回 DiagnosisReport 结构
```

**常见坑：**
- `SqliteSaver` 在新版 LangGraph 需要 `from langgraph.checkpoint.sqlite import SqliteSaver`，需要 `pip install langgraph-checkpoint-sqlite`
- Pydantic 模型放进 TypedDict 时，序列化需要小心；如果遇到 Checkpoint 序列化问题，把 BaseModel 改成 TypedDict

---

### D8（周三）：Doctor Dockerfile + 集成到 Compose

#### 任务 8.1：Doctor Dockerfile（上午 2 小时）

参考 demo-app/backend/Dockerfile 写一个类似的，多阶段构建。

#### 任务 8.2：Qdrant + 加入 compose（上午 1 小时）

**AI 提示词：**

> 在根 `docker-compose.yml` 新增：
> 1. `qdrant`：qdrant/qdrant 镜像，端口 6333、6334，volume 持久化
> 2. `doctor-api`：构建自 `./doctor`，依赖 qdrant + loki + tempo，端口 8001:8000，环境变量包括：
>    - LLM_API_KEY、LLM_BASE_URL、LLM_MODEL
>    - QDRANT_URL=http://qdrant:6333
>    - LOKI_URL=http://loki:3100
>    - TEMPO_URL=http://tempo:3200
> 
> 同时更新 Makefile：
> - `make doctor-logs`、`make doctor-restart`

**验收：**
- `make up; make ps` 看到 doctor-api 健康
- `curl.exe http://localhost:8001/health` 返回 ok
- `curl.exe http://localhost:8001/api/diagnose -d '{...}'` 跑通

#### 任务 8.3：Doctor 自动拉取可观测性数据的工具（下午 5 小时）

**AI 提示词：**

> 在 `doctor/src/tools/observability_tools.py` 实现以下工具（async 函数，带 `@traced` 装饰器）：
> 
> 1. `async def query_loki_logs(logql: str, start: datetime, end: datetime, limit: int = 1000) -> list[LogEntry]`
>    - 调用 Loki HTTP API `/loki/api/v1/query_range`
>    - 转换为 LogEntry 列表
> 
> 2. `async def query_tempo_trace(trace_id: str) -> list[TraceSpan]`
>    - 调用 Tempo API `/api/traces/{trace_id}`
>    - 解析 OTLP 格式，转换为 TraceSpan 列表
> 
> 3. `async def search_tempo_traces(service: str, start: datetime, end: datetime, min_duration_ms: Optional[float] = None) -> list[dict]`
>    - 调用 Tempo `/api/search` 查询符合条件的 trace 列表
> 
> 4. 配套的 LangChain `StructuredTool` 包装（在 `tools/__init__.py` 暴露）
> 
> 使用 aiohttp 客户端，配置超时 30s，自动重试 3 次。
> URL 从 config 读取。

**验收：**
- 写单元测试：mock aiohttp 响应，验证解析正确
- 写集成测试：用真实 Loki 验证（可选）

---

### D9（周四）：知识库基础设施

#### 任务 9.1：向量知识库（全天）

**AI 提示词：**

> 在 `doctor/src/knowledge/` 下实现知识库基础设施：
> 
> 1. `vector_kb.py`：VectorKnowledgeBase 类
>    - 基于 langchain-qdrant 封装
>    - 方法：
>      - `__init__(qdrant_url, embeddings)`
>      - `get_collection(name: str) -> Qdrant` 自动 create_if_not_exists
>      - `add_documents(collection: str, docs: list[Document])`
>      - `search(collection: str, query: str, k: int = 5, filters: Optional[dict] = None) -> list[Document]`
>      - `delete_collection(name: str)`
> 
> 2. `embeddings.py`：Embedding 模型加载
>    - 优先用 OpenAI 兼容 API（从 config 读取 EMBEDDING_BASE_URL 等）
>    - 备选用 sentence-transformers 本地加载 bge-m3
> 
> 3. `struct_kb.py`：结构化知识库
>    - 用 SQLite 存储
>    - 表设计：
>      - `http_status_codes`：code, category, description
>      - `error_patterns`：pattern (regex), category, description
>      - `framework_practices`：framework, version, practice_type, description
>    - 方法：`add()`、`query()`、`bulk_load_from_yaml(file_path)`
> 
> 4. `hybrid_service.py`：KnowledgeService 类
>    - 组合 vector_kb + struct_kb
>    - 接口：
>      - `async def search_historical_cases(query: str, k: int = 3) -> list[dict]`
>      - `async def search_practices(framework: str, problem: str) -> list[dict]`
>      - `async def classify_error_pattern(error_message: str) -> Optional[str]`
>      - `async def index_diagnosis(report: DiagnosisReport, evidence: Evidence)`
> 
> 5. 初始化脚本 `scripts/init_kb.py`：
>    - 创建 Qdrant collections
>    - 从 `doctor/seed_data/` 加载初始知识（先空着，后续补）

**验收：**
- `uv run python scripts/init_kb.py` 跑通
- 单元测试覆盖 hybrid_service 所有方法

---

### D10（周五）：W1-W2 收尾 + Sprint Review

#### 任务 10.1：端到端冒烟测试（上午 4 小时）

**AI 提示词：**

> 在 `tests/integration/` 下写一个端到端冒烟测试：
> 
> 1. 启动 docker compose
> 2. 在 demo-app 创建一个用户、一个项目、3 个任务
> 3. 模拟一个错误：调用一个不存在的 API
> 4. 等待 5 秒让日志写入 Loki
> 5. 调用 doctor 的 `/api/diagnose`，传入：
>    ```python
>    {
>      "evidence": {
>        "user_report": "调用 /api/nonexistent 返回 404",
>        "logs": [],  # Doctor 会自己去 Loki 拉取
>        "traces": []
>      }
>    }
>    ```
> 6. 断言：Doctor 返回了一个 DiagnosisReport，且 bug_category 不为空
> 
> 这是一个端到端测试，不要求 Doctor 给出正确诊断（此时 Doctor 还很弱），只要求**整个流水线跑通**。

#### 任务 10.2：CI 雏形（下午 2 小时）

**AI 提示词：**

> 在 `.github/workflows/ci.yml` 创建 CI workflow：
> 
> 1. 触发：push 到 main、PR
> 2. 步骤：
>    - checkout
>    - 安装 uv
>    - `uv sync` 安装所有 workspace 依赖
>    - `uv run ruff check`
>    - `uv run ruff format --check`
>    - `uv run mypy doctor/src bug-factory/src benchmark/src`
>    - `uv run pytest`
> 3. 矩阵：Python 3.11、3.12

#### 任务 10.3：Sprint 1 文档（下午 2 小时）

更新 `README.md`，包含：
- 项目简介（来自 diagdoctor-from-scratch.md）
- 一键启动命令
- 当前已实现的功能列表
- 待办列表（Sprint 2-4 计划）

**Sprint 1 验收（必须全部通过才能进入 Sprint 2）：**

- [ ] `make up` 启动所有服务（postgres、redis、demo-backend、demo-frontend、otel-collector、loki、tempo、grafana、qdrant、doctor-api）
- [ ] 浏览器访问 http://localhost:3000 完整使用 TaskFlow
- [ ] Grafana 中能看到 demo-backend 的日志和 trace
- [ ] `curl.exe /api/diagnose` 端到端跑通，返回结构化报告
- [ ] CI 全绿
- [ ] mypy strict 模式无错误

---

# Sprint 2：Bug Factory + Harness 评测雏形（D11-D20）

## Week 3：Bug Factory 核心

### D11（周一）：Bug 配方设计 + Schema

#### 任务 11.1：Bug 配方 Schema 定义（上午 3 小时）

**AI 提示词：**

> 在 `bug-factory/src/schema.py` 定义 Pydantic v2 模型，对应 Bug 配方 YAML 结构：
> 
> ```python
> from typing import Literal, Optional
> from pydantic import BaseModel, Field
> 
> class ExpectedDiagnosis(BaseModel):
>     root_cause: str
>     affected_file: str
>     affected_line: Optional[int] = None
>     fix_suggestion: str
>     fix_keywords: list[str]  # 修复建议中必须出现的关键字
> 
> class Injection(BaseModel):
>     strategy: Literal["code_replace", "code_insert", "code_delete", "config_change", "env_change"]
>     target_file: str
>     ai_instruction: str  # 给 AI 的改写指令
>     # 或精确 diff
>     diff_patch: Optional[str] = None
> 
> class TriggerStep(BaseModel):
>     action: Literal["login", "api_call", "ui_click", "create_data", "wait"]
>     params: dict
> 
> class ExpectedObservation(BaseModel):
>     log_patterns: list[dict] = []  # [{pattern: regex, min_occurrences: int}]
>     trace_attributes: dict = {}  # {span_name: {duration_ms: ">2000"}}
>     api_response: Optional[dict] = None
> 
> class Trigger(BaseModel):
>     type: Literal["e2e_action", "api_call", "scheduled"]
>     steps: list[TriggerStep]
>     expected_observation: ExpectedObservation
> 
> class Evaluation(BaseModel):
>     must_mention_keywords: list[str]
>     should_mention_keywords: list[str] = []
>     llm_judge_criteria: str
>     min_confidence: float = 0.6
> 
> class BugRecipe(BaseModel):
>     id: str = Field(pattern=r"^[A-Z]+-\d{3}$")
>     title: str
>     category: Literal[
>         "frontend_crash", "backend_error",
>         "performance", "logic", "data", "config"
>     ]
>     severity: Literal["low", "medium", "high", "critical"]
>     expected_diagnosis: ExpectedDiagnosis
>     injection: Injection
>     trigger: Trigger
>     evaluation: Evaluation
>     tags: list[str] = []
> ```
> 
> 同时实现：
> - `load_recipe(path: Path) -> BugRecipe`：从 YAML 加载
> - `validate_all_recipes(recipes_dir: Path) -> list[ValidationError]`：批量校验

#### 任务 11.2：编写前 4 个示例配方（下午 5 小时）

逐个编写：

**配方 1：BE-001 N+1 查询**

文件：`bug-factory/recipes/be_001_n_plus_1.yaml`

直接参考 [diagdoctor-from-scratch.md §4.3](diagdoctor-from-scratch.md) 的示例。

**配方 2：FE-001 空指针访问**

```yaml
id: FE-001
title: "任务详情页访问 null assignee 导致崩溃"
category: frontend_crash
severity: high
expected_diagnosis:
  root_cause: "TaskDetailPage 渲染 assignee.displayName 时未做 null 检查"
  affected_file: "demo-app/frontend/src/pages/TaskDetailPage.tsx"
  fix_suggestion: "使用可选链 assignee?.displayName 或在渲染前判断"
  fix_keywords: ["?.", "null check", "optional chaining"]

injection:
  strategy: code_replace
  target_file: "demo-app/frontend/src/pages/TaskDetailPage.tsx"
  ai_instruction: |
    把所有 `assignee?.displayName` 改成 `assignee.displayName`
    把所有 `assignee?.email` 改成 `assignee.email`
    不要修改其他代码

trigger:
  type: e2e_action
  steps:
    - action: login
      params: {email: "admin@example.com", password: "Admin123!"}
    - action: create_data
      params: {entity: "task", data: {title: "Unassigned Task", assignee_id: null}}
    - action: ui_click
      params: {selector: "[data-testid='task-link-Unassigned Task']"}
  expected_observation:
    log_patterns:
      - pattern: "TypeError|Cannot read"
        min_occurrences: 1

evaluation:
  must_mention_keywords: ["null", "assignee"]
  should_mention_keywords: ["可选链", "?.", "TypeScript"]
  llm_judge_criteria: |
    诊断必须明确指出 assignee 为 null 时访问其属性导致 TypeError，
    并建议使用可选链或显式 null 检查。
  min_confidence: 0.7
```

**配方 3 & 4：** 同理编写 PERF-001（缺索引）、LOGIC-001（权限越界）。

**验收：**
- 4 个 YAML 文件存在且通过 schema 校验
- 跑 `uv run python -m bug_factory.cli validate-recipes` 全部通过

---

### D12（周二）：Bug Injector 实现

#### 任务 12.1：Git 分支管理（上午 3 小时）

**AI 提示词：**

> 在 `bug-factory/src/git_manager.py` 实现 Git 操作封装：
> 
> ```python
> from pathlib import Path
> from git import Repo  # gitpython
> 
> class GitManager:
>     def __init__(self, repo_path: Path): ...
>     def create_bug_branch(self, recipe_id: str) -> str:
>         """从 main 创建分支 bug/{recipe_id}，如已存在则强制重置"""
>     def commit_changes(self, message: str): ...
>     def reset_to_main(self): ...
>     def get_current_branch(self) -> str: ...
>     def diff_against_main(self) -> str: ...
> ```
> 
> 关键：所有操作必须有日志，失败抛 GitOperationError。

#### 任务 12.2：AI 代码改写器（下午 5 小时）

**AI 提示词：**

> 在 `bug-factory/src/ai_rewriter.py` 实现：
> 
> ```python
> class AIRewriter:
>     def __init__(self, llm):
>         self.llm = llm
>     
>     async def rewrite_file(
>         self,
>         file_content: str,
>         instruction: str,
>         file_language: str = "python",
>     ) -> str:
>         """根据指令重写文件内容，返回新内容"""
> ```
> 
> 实现细节：
> 1. 使用结构化的 Prompt：
>    ```
>    你是一个代码改写助手。请根据下面的指令修改给定代码。
>    
>    【原始代码】
>    ```{language}
>    {content}
>    ```
>    
>    【改写指令】
>    {instruction}
>    
>    【要求】
>    - 只输出修改后的完整文件内容
>    - 不要添加任何解释或注释
>    - 用 ```{language} ... ``` 包裹
>    ```
> 
> 2. 解析 LLM 响应，提取 code block 内容
> 3. 校验：新内容不能为空、不能完全相同（说明 AI 没改）
> 4. 失败时重试最多 3 次
> 
> 同时实现 `DiffPatchApplier`：如果 recipe 直接提供 `diff_patch`，跳过 AI 直接 apply。

#### 任务 12.3：Injector 主类（下午 3 小时）

**AI 提示词：**

> 在 `bug-factory/src/injector.py` 实现 BugInjector：
> 
> ```python
> class BugInjector:
>     def __init__(self, repo_path: Path, llm):
>         self.git = GitManager(repo_path)
>         self.rewriter = AIRewriter(llm)
>     
>     async def inject(self, recipe: BugRecipe) -> InjectionResult:
>         """注入 bug 到目标仓库，返回结果"""
>         # 1. 创建分支
>         branch = self.git.create_bug_branch(recipe.id)
>         
>         # 2. 应用注入
>         target = Path(recipe.injection.target_file)
>         original = target.read_text(encoding="utf-8")
>         
>         if recipe.injection.diff_patch:
>             modified = apply_patch(original, recipe.injection.diff_patch)
>         else:
>             lang = detect_language(target)
>             modified = await self.rewriter.rewrite_file(
>                 original, recipe.injection.ai_instruction, lang
>             )
>         
>         # 3. 写入 + 校验
>         if modified == original:
>             raise InjectionError(f"AI 未改动文件：{recipe.id}")
>         target.write_text(modified, encoding="utf-8")
>         
>         # 4. 提交
>         self.git.commit_changes(f"Inject bug: {recipe.id} - {recipe.title}")
>         
>         return InjectionResult(
>             recipe_id=recipe.id,
>             branch=branch,
>             diff=self.git.diff_against_main(),
>             modified_files=[str(target)],
>         )
> ```

**验收：**
- 跑 `uv run python -m bug_factory.cli inject BE-001`，看到：
  - 在 demo-app 仓库创建 `bug/BE-001` 分支
  - `app/tasks/views.py` 被修改（N+1 query 被引入）
  - 自动 commit

---

### D13（周三）：Trigger Runner

#### 任务 13.1：E2E Action 执行器（全天）

**AI 提示词：**

> 在 `bug-factory/src/trigger.py` 实现 TriggerRunner：
> 
> ```python
> class TriggerRunner:
>     def __init__(self, demo_app_base_url: str = "http://localhost:8000"):
>         self.base_url = demo_app_base_url
>         self.session: dict = {}  # 存储 token 等会话状态
>     
>     async def run(self, trigger: Trigger) -> TriggerResult:
>         for step in trigger.steps:
>             handler = self._get_handler(step.action)
>             await handler(step.params)
>         await asyncio.sleep(3)  # 等日志落地
>         return TriggerResult(success=True, session=self.session)
>     
>     async def _action_login(self, params): ...      # 调 /api/auth/login，保存 token
>     async def _action_api_call(self, params): ...   # 用 aiohttp 调任意接口
>     async def _action_ui_click(self, params): ...   # 用 playwright 点击
>     async def _action_create_data(self, params): ...# 调对应的 create API
>     async def _action_wait(self, params): ...       # asyncio.sleep
> ```
> 
> 关键实现细节：
> 1. UI action 用 playwright async API
> 2. API action 自动注入 Bearer token
> 3. 每一步执行后记录耗时与响应（用于后续 evidence_collector）
> 4. 任何步骤失败立即中止，记录失败原因

**验收：**
- 单元测试 mock 各 action 类型
- 集成测试：用 BE-001 的 trigger 步骤跑通

---

### D14（周四）：Evidence Collector + Case Generator

#### 任务 14.1：Evidence Collector（上午 4 小时）

**AI 提示词：**

> 在 `bug-factory/src/evidence_collector.py` 实现 EvidenceCollector：
> 
> ```python
> class EvidenceCollector:
>     def __init__(self, loki_url: str, tempo_url: str):
>         self.loki = loki_url
>         self.tempo = tempo_url
>     
>     async def collect(
>         self,
>         start: datetime,
>         end: datetime,
>         services: list[str] = ["demo-backend", "demo-frontend"],
>     ) -> CollectedEvidence:
>         """收集时间窗内的所有证据"""
>         logs = await self._fetch_logs(start, end, services)
>         traces = await self._fetch_traces(start, end, services)
>         return CollectedEvidence(
>             logs=logs,
>             traces=traces,
>             time_window=(start, end),
>         )
>     
>     async def _fetch_logs(...): ...  # 调 Loki API
>     async def _fetch_traces(...): ...  # 调 Tempo API
> ```
> 
> 实现要求：
> 1. 自动分页（Loki 单次最大 5000 条）
> 2. trace 取至少 10 个最慢的 + 10 个错误的
> 3. 全部保存到 `bug-factory/output/{recipe_id}/evidence/`

#### 任务 14.2：Case Generator（下午 4 小时）

**AI 提示词：**

> 在 `bug-factory/src/case_generator.py` 实现：
> 
> ```python
> class CaseGenerator:
>     async def generate(
>         self,
>         recipe: BugRecipe,
>         injection_result: InjectionResult,
>         trigger_result: TriggerResult,
>         evidence: CollectedEvidence,
>     ) -> EvaluationCase:
>         """根据完整执行结果生成评测案例 YAML"""
> ```
> 
> 生成的 EvaluationCase 结构：
> ```yaml
> case_id: BE-001
> generated_at: "2026-06-25T10:30:00"
> recipe_id: BE-001
> input:
>   user_report: "..."  # 从 recipe.title 派生 + AI 改写得更自然
>   evidence:
>     logs_file: "evidences/BE-001/logs.json"
>     traces_file: "evidences/BE-001/traces.json"
>     trigger_summary: "..."
> expected:
>   category: backend_performance
>   root_cause_summary: "..."  # 从 recipe.expected_diagnosis 派生
>   affected_files: ["app/tasks/views.py"]
>   fix_keywords: ["joinedload", "N+1"]
>   llm_judge_criteria: "..."
> ```
> 
> 关键：
> 1. user_report 用 AI 改写：基于 recipe.title 生成"普通用户视角的描述"，不能直接用技术术语
> 2. evidence 文件单独存放，case YAML 只存路径
> 3. 输出到 `benchmark/cases/{recipe_id}.yaml`

#### 任务 14.3：CLI 串联（晚上 2 小时）

**AI 提示词：**

> 在 `bug-factory/src/cli.py` 实现 click CLI：
> 
> ```powershell
> # 校验所有配方
> uv run python -m bug_factory.cli validate
> 
> # 注入单个 bug
> uv run python -m bug_factory.cli inject BE-001
> 
> # 触发并收集证据
> uv run python -m bug_factory.cli trigger BE-001
> 
> # 一键完整流程：注入 → 部署 → 触发 → 收集 → 生成 case
> uv run python -m bug_factory.cli full BE-001
> 
> # 批量
> uv run python -m bug_factory.cli full-all --filter category=backend_*
> ```
> 
> 每个命令都有详细的 `--help`，输出友好的进度提示（用 rich）。

**验收：**
- 跑 `python -m bug_factory.cli full BE-001`
- 检查生成的：
  - `bug/BE-001` 分支已创建
  - `bug-factory/output/BE-001/` 下有 evidence 文件
  - `benchmark/cases/BE-001.yaml` 已生成

---

### D15（周五）：完成第一批 4 个配方的端到端验证

#### 任务 15.1：跑通 4 个配方（全天）

逐个跑 BE-001、FE-001、PERF-001、LOGIC-001 的 full 流程。

**预期发现的问题（弱模型必然遇到）：**

| 问题 | 解决方法 |
|------|---------|
| AI 改写代码后语法错误 | 加 syntax check，失败重试 |
| Trigger 找不到 UI 元素 | 完善 `data-testid` 标记规范 |
| Loki 查不到日志（service.name 不对） | 统一 OTel resource attributes |
| 容器需要重建才能加载新代码 | 加 `make demo-rebuild` 命令 |

**Sprint 2 中期验收（D15 末）：**
- [ ] 4 个 bug 配方完整跑通
- [ ] 4 个评测案例 YAML 生成
- [ ] evidence 文件可读、信息充足

---

## Week 4：Harness 评测体系

### D16（周一）：Harness Runner

#### 任务 16.1：Case Loader（上午 2 小时）

**AI 提示词：**

> 在 `benchmark/src/loader.py` 实现：
> 
> ```python
> class CaseLoader:
>     def load_one(self, case_id: str) -> EvaluationCase: ...
>     def load_suite(self, filter: dict = None) -> list[EvaluationCase]: ...
>     def filter_by_tags(self, cases: list, tags: list[str]) -> list: ...
> ```

#### 任务 16.2：BatchRunner（下午 5 小时）

**AI 提示词：**

> 在 `benchmark/src/runner.py` 实现：
> 
> ```python
> class BatchRunner:
>     def __init__(
>         self,
>         doctor_api_url: str,
>         max_concurrency: int = 4,
>     ): ...
>     
>     async def run_one(self, case: EvaluationCase) -> RunResult:
>         """跑单个 case，返回结果"""
>         # 1. 加载 evidence 文件
>         # 2. POST 到 doctor_api_url/api/diagnose
>         # 3. 记录耗时、cost、tool_calls 等
>         # 4. 返回 RunResult(case_id, diagnosis_report, metadata)
>     
>     async def run_batch(
>         self,
>         cases: list[EvaluationCase],
>         progress_callback=None,
>     ) -> BatchRunResult:
>         """并行跑多个 case，返回汇总"""
> ```
> 
> 实现要求：
> 1. 用 asyncio.Semaphore 控制并发
> 2. 进度展示用 rich Progress
> 3. 失败的 case 不阻塞其他，记录失败原因
> 4. 输出到 `benchmark/runs/{timestamp}/` 目录

#### 任务 16.3：Evaluators 第一版（晚上 1 小时）

**AI 提示词：**

> 在 `benchmark/src/evaluators/` 创建：
> 
> 1. `base.py`：BaseEvaluator 抽象类
>    ```python
>    class BaseEvaluator(ABC):
>        name: str
>        @abstractmethod
>        async def evaluate(
>            self, case: EvaluationCase, result: RunResult
>        ) -> EvaluationScore: ...
>    ```
> 
> 2. `exact_match.py`：ExactMatchEvaluator
>    - 检查 result.diagnosis.bug_category == case.expected.category
>    - 检查 result.diagnosis.affected_file 在 case.expected.affected_files 中
>    - 返回 0 或 1 分
> 
> 3. `keyword_match.py`：KeywordMatchEvaluator
>    - 检查 case.expected.fix_keywords 在 result.diagnosis.fix_suggestion 中的覆盖率
>    - 返回 0-1 浮点数
> 
> 4. `efficiency.py`：EfficiencyEvaluator
>    - 评分 = f(tool_calls, tokens, latency)
>    - 返回 0-1 浮点数

---

### D17（周二）：LLM Judge Evaluator

#### 任务 17.1：LLM Judge 实现（全天）

**AI 提示词：**

> 在 `benchmark/src/evaluators/llm_judge.py` 实现：
> 
> ```python
> class LLMJudgeEvaluator(BaseEvaluator):
>     name = "llm_judge_correctness"
>     
>     def __init__(self, judge_llm):
>         self.llm = judge_llm  # 用强模型，如 GPT-4 或 Claude
>     
>     async def evaluate(self, case, result) -> EvaluationScore:
>         prompt = self._build_prompt(case, result)
>         response = await self.llm.ainvoke(
>             prompt,
>             response_format=JudgeResponse,
>         )
>         return EvaluationScore(
>             score=response.score,
>             reasoning=response.reasoning,
>             metadata={"judge_model": ..., "judge_tokens": ...},
>         )
> ```
> 
> Judge Prompt 必须包含：
> 1. 案例描述（user_report、expected）
> 2. Doctor 给出的诊断
> 3. 评分标准（llm_judge_criteria 来自 case）
> 4. 输出格式：score (0-1)、reasoning
> 
> 用 Pydantic + LangChain structured output 保证输出格式。
> 
> 添加缓存：同一对 (case_id, diagnosis_hash) 不重复评测。

**验收：**
- 用 4 个已生成的 case，跑 `uv run python -m benchmark.cli run --suite all`
- 输出包含 4 个 case 的所有 evaluator 得分

---

### D18（周三）：Report Generators

#### 任务 18.1：Markdown 报告（上午 4 小时）

**AI 提示词：**

> 在 `benchmark/src/reporters/markdown.py` 实现：
> 
> ```python
> class MarkdownReporter:
>     def generate(self, batch_result: BatchRunResult) -> str: ...
> ```
> 
> 生成结构：
> ```markdown
> # Evaluation Report - {timestamp}
> 
> ## Summary
> - Total Cases: 4
> - Passed: 3 (75%)
> - Failed: 1
> - Total Cost: $0.32
> - Avg Latency: 12.4s
> 
> ## By Category
> | Category | Pass Rate | Avg Latency |
> | ... | ... | ... |
> 
> ## By Evaluator
> | Evaluator | Avg Score |
> | ... | ... |
> 
> ## Failed Cases Details
> ### BE-001
> - User Report: ...
> - Expected: ...
> - Actual: ...
> - Judge Reasoning: ...
> 
> ## Token Usage
> ...
> ```

#### 任务 18.2：HTML Dashboard（下午 4 小时）

**AI 提示词：**

> 在 `benchmark/src/reporters/html.py` 用 Jinja2 + 一个简单 HTML 模板生成 dashboard。
> 
> 模板包含：
> 1. 顶部 KPI 卡片（总数、通过率、平均成本、平均耗时）
> 2. 按 category 的柱状图（用 Chart.js CDN）
> 3. case 详情表格（可展开看 diagnosis vs expected 对比）
> 4. 趋势图（如果存在多次历史 run，从 `benchmark/runs/` 读取）
> 
> 输出到 `benchmark/runs/{timestamp}/report.html`，自包含（CSS/JS 用 CDN）。

---

### D19（周四）：Harness CLI 完善 + CI 集成

#### 任务 19.1：完善 CLI（上午 4 小时）

**AI 提示词：**

> 在 `benchmark/src/cli.py` 完善 CLI（用 click）：
> 
> ```powershell
> # 跑评测
> uv run diagdoctor-bench run --suite all --concurrency 4
> 
> # 按类别跑
> uv run diagdoctor-bench run --category X
> 
> # 单个案例
> uv run diagdoctor-bench run --case BE-001
> 
> # 对比两次 run
> uv run diagdoctor-bench compare --baseline <run_id> --candidate <run_id>
> 
> # 列出历史 run
> uv run diagdoctor-bench list-runs
> 
> # 查看某次 run 详情
> uv run diagdoctor-bench show <run_id>
> 
> # 导出
> uv run diagdoctor-bench export <run_id> --format markdown
> ```
> 
> 添加 entry point 到 `pyproject.toml`：
> ```toml
> [project.scripts]
> diagdoctor-bench = "benchmark.cli:main"
> ```

#### 任务 19.2：第一份正式评测报告（下午 4 小时）

跑全量评测（此时只有 4 个 case + Doctor 还很弱），生成报告。

**预期结果：** 准确率应该很低（30-50%），这是基线，后续 Sprint 3/4 要把它提上去。

---

### D20（周五）：扩充配方到 15+ + Sprint 2 收尾

#### 任务 20.1：批量编写配方（全天）

按以下表格扩充配方（每个约 30-45 分钟用 AI 辅助生成）：

| 类别 | 数量 | 示例 |
|------|------|------|
| frontend_crash | 3 | 已有 FE-001，新增 FE-002 未捕获 Promise、FE-003 状态错乱 |
| backend_error | 3 | 已有 BE-001 N+1，新增 BE-002 校验绕过、BE-003 JWT 过期 |
| performance | 3 | 已有 PERF-001 缺索引，新增 PERF-002 缓存穿透、PERF-003 循环调用 API |
| logic | 3 | 已有 LOGIC-001 越权，新增 LOGIC-002 竞态、LOGIC-003 时区错误 |
| data | 2 | DATA-001 编码错误、DATA-002 精度丢失 |
| config | 1 | CONFIG-001 CORS 错配 |

**最少达到 15 个配方。**

**Sprint 2 最终验收：**
- [ ] ≥ 15 个 bug 配方，全部通过 schema 校验
- [ ] ≥ 15 个评测案例完整生成（跑通 full 流程）
- [ ] 第一份评测报告生成（HTML + Markdown）
- [ ] CI 跑 smoke suite（≥ 4 个 case）通过

---

# Sprint 3：核心 Agent 系统（D21-D30）

## Week 5：分类 + 日志 Agent

### D21（周一）：TriageAgent

#### 任务 21.1：TriageAgent 节点实现（全天）

**AI 提示词：**

> 在 `doctor/src/graph/nodes/triage.py` 实现 TriageAgent：
> 
> 1. 系统提示词（放在 `prompts/templates/triage.j2`）：
>    ```jinja2
>    你是一个 Bug 分类专家。基于以下信息判断 bug 的类别：
>    
>    【用户报告】
>    {{ user_report }}
>    
>    【日志摘要】（前 50 条）
>    {{ logs_summary }}
>    
>    【Trace 摘要】（错误或慢 span）
>    {{ traces_summary }}
>    
>    【可能的类别】
>    - frontend_crash: 前端运行时崩溃（白屏、JS 错误）
>    - backend_error: 后端异常（5xx、未处理异常）
>    - performance: 性能问题（慢、超时）
>    - logic: 业务逻辑错误（数据不对、流程错乱）
>    - data: 数据问题（编码、精度、时区）
>    - config: 配置或环境问题
>    
>    【类似历史案例】
>    {{ similar_cases }}
>    
>    请输出 JSON：
>    {
>      "category": "...",
>      "confidence": 0.0-1.0,
>      "reasoning": "..."
>    }
>    ```
> 
> 2. 节点函数：
>    ```python
>    async def triage_node(state: DoctorState) -> dict:
>        # 1. 准备 context
>        logs_summary = summarize_logs(state["evidence"].get("logs", []))
>        traces_summary = summarize_traces(state["evidence"].get("traces", []))
>        
>        # 2. RAG 召回相似案例
>        similar = await knowledge_service.search_historical_cases(
>            state["evidence"]["user_report"], k=3
>        )
>        
>        # 3. 调 LLM
>        prompt = render_prompt("triage", {...})
>        response = await llm.with_structured_output(TriageOutput).ainvoke(prompt)
>        
>        # 4. 更新 state
>        return {
>            "bug_category": response.category,
>            "findings": [Finding(
>                agent="TriageAgent",
>                summary=response.reasoning,
>                confidence=response.confidence,
>                evidence_refs=[],
>            )],
>        }
>    ```
> 
> 3. 在 main_graph 中替换 dummy_triage_node

**验收：**
- 跑评测，TriageAgent 的分类准确率（与 expected.category 对比）≥ 80%
- 单元测试：mock evidence 输入，验证输出格式

---

### D22-D23（周二-周三）：BackendLogAgent

#### 任务 22.1：BackendLogAgent 子图（D22 全天）

**AI 提示词：**

> 在 `doctor/src/graph/subgraphs/backend_log_agent.py` 实现 BackendLogAgent SubGraph：
> 
> 这是一个 ReAct Agent，使用 `create_react_agent`：
> 
> ```python
> from langgraph.prebuilt import create_react_agent
> 
> def build_backend_log_agent(llm, tools):
>     return create_react_agent(
>         model=llm,
>         tools=tools,
>         state_modifier=BACKEND_LOG_SYSTEM_PROMPT,
>     )
> ```
> 
> 可用工具（在 `doctor/src/tools/log_tools.py` 实现）：
> 1. `query_loki_logs(logql, start, end, limit)`：已实现
> 2. `filter_logs_by_level(logs, levels)`
> 3. `extract_error_patterns(logs)`：用正则提取常见错误模式
> 4. `search_log_by_keyword(logs, keyword)`
> 5. `group_logs_by_trace_id(logs)`
> 6. `summarize_log_window(logs, max_lines=50)`
> 
> 系统提示词（templates/backend_log_agent.j2）：
> ```
> 你是后端日志分析专家。给定一个 bug 现象，通过以下步骤定位根因：
> 
> 1. 查询时间窗内的 ERROR 级别日志
> 2. 识别异常类型和触发位置
> 3. 沿着 trace_id 查找完整调用链
> 4. 提取关键信息：文件名、行号、异常消息、关键变量
> 
> 最终输出 Finding：
> - summary: 问题摘要
> - evidence_refs: 引用的日志 ID
> - confidence: 置信度
> ```

#### 任务 22.2：包装为 LangGraph Node（D23 上午）

**AI 提示词：**

> 在 `doctor/src/graph/nodes/backend_log.py` 实现：
> 
> ```python
> async def backend_log_node(state: DoctorState) -> dict:
>     agent = get_backend_log_agent()  # cached
>     
>     # 准备 agent 输入
>     agent_input = {
>         "messages": [
>             HumanMessage(content=format_evidence_for_agent(state))
>         ]
>     }
>     
>     # 调用 ReAct Agent
>     agent_result = await agent.ainvoke(agent_input)
>     
>     # 提取 Finding
>     finding = parse_agent_output_to_finding(agent_result)
>     
>     return {"findings": [finding]}
> ```
> 
> 关键：format_evidence_for_agent 要把 state 转成 agent 能消化的文本（不要传整个 evidence，太大）。

#### 任务 22.3：集成到主图（D23 下午）

更新 `main_graph.py`：

```python
g.add_conditional_edges("triage", route_by_category, {
    "backend_error": "backend_log_agent",
    "performance": "backend_log_agent",  # 暂时都走这个
    "logic": "backend_log_agent",
    # 其他暂未实现，默认走 backend_log_agent
})
g.add_node("backend_log_agent", backend_log_node)
g.add_edge("backend_log_agent", "reporter")
```

**验收（D23 末）：**
- 跑评测，至少 BE-001、PERF-001 类型 case 的 finding 不为空且相关
- 评测整体准确率 ≥ 55%

---

### D24（周四）：FrontendLogAgent

#### 任务 24.1：实现 FrontendLogAgent（全天）

**AI 提示词：**

> 与 BackendLogAgent 类似，在 `doctor/src/graph/subgraphs/frontend_log_agent.py` 实现 FrontendLogAgent。
> 
> 特殊工具：
> 1. `parse_browser_console_logs(logs)`：解析 console.error 输出（认识 `[XXX_TAG]` 标记）
> 2. `parse_sentry_event(event_data)`：解析 Sentry 事件结构
> 3. `extract_stack_trace(error_msg)`：从错误消息提取堆栈
> 4. `resolve_source_map(file, line, sourcemap_url)`：用 source map 还原（如果有）
> 
> 系统提示词强调：
> - 优先关注 console.error 中的 `[XXX_TAG]` 结构化标记
> - 识别 React 组件错误（"The above error occurred in the X component"）
> - 识别 Promise rejection（"Uncaught (in promise)"）

更新 main_graph：frontend_crash 类型路由到 frontend_log_agent。

**验收：**
- FE-001 case 跑通，finding 包含 "TypeError" 和 "assignee"

---

### D25（周五）：TraceAgent

#### 任务 25.1：TraceAgent 实现（全天）

**AI 提示词：**

> 在 `doctor/src/graph/nodes/trace.py` 实现 TraceAgent（直接用 LangGraph 节点，不需要 ReAct，因为流程固定）：
> 
> ```python
> async def trace_node(state: DoctorState) -> dict:
>     traces = state["evidence"].get("traces", [])
>     
>     if not traces:
>         # 尝试根据 findings 中的 trace_id 主动拉取
>         trace_ids = extract_trace_ids_from_findings(state["findings"])
>         for tid in trace_ids:
>             traces.extend(await query_tempo_trace(tid))
>     
>     # 分析
>     critical_path = find_critical_path(traces)
>     bottlenecks = find_bottlenecks(traces, threshold_ms=500)
>     errors = find_error_spans(traces)
>     
>     # 调用 LLM 总结
>     summary = await llm.ainvoke(
>         render_prompt("trace_analysis", {
>             "critical_path": critical_path,
>             "bottlenecks": bottlenecks,
>             "errors": errors,
>         })
>     )
>     
>     return {"findings": [Finding(
>         agent="TraceAgent",
>         summary=summary,
>         evidence_refs=[t.span_id for t in critical_path],
>         confidence=0.8 if errors else 0.6,
>     )]}
> ```
> 
> 实现工具函数（在 `doctor/src/tools/trace_analysis.py`）：
> - `find_critical_path(traces) -> list[TraceSpan]`：构建 span 树，找最长路径
> - `find_bottlenecks(traces, threshold_ms) -> list[TraceSpan]`：找慢 span
> - `find_error_spans(traces) -> list[TraceSpan]`：找 status=error 的 span

更新 main_graph，让 backend/frontend 完成后都汇聚到 trace_node。

**验收：**
- PERF-001 case 中，trace_node 应识别出慢 SQL span

---

## Week 6：性能 + 逻辑 Agent + 评测调优

### D26（周一）：PerfAgent

#### 任务 26.1：PerfAgent 实现（全天）

**AI 提示词：**

> 在 `doctor/src/graph/subgraphs/perf_agent.py` 实现 PerfAgent（ReAct）。
> 
> 特殊工具：
> 1. `analyze_sql_queries(logs)`：识别 SQL 查询日志，统计 N+1、慢查询
> 2. `analyze_api_latency(traces)`：按端点统计 P50/P95/P99
> 3. `detect_repeated_calls(traces, threshold=10)`：检测短时间重复调用（缓存缺失征兆）
> 4. `analyze_resource_usage(logs)`：从日志识别 CPU/Memory 高使用迹象
> 
> 系统提示词强调：
> - 性能问题必须给出**量化数据**（耗时多少、查询多少次等）
> - 优先识别这几种模式：N+1、缺索引、缓存穿透、外部 API 阻塞、大对象处理

**验收：**
- PERF-001（N+1）case 准确识别为 N+1 问题
- 整体评测准确率 ≥ 65%

---

### D27（周二）：LogicAgent

#### 任务 27.1：LogicAgent 实现（全天）

**AI 提示词：**

> 在 `doctor/src/graph/subgraphs/logic_agent.py` 实现 LogicAgent（ReAct）。
> 
> 特殊工具：
> 1. `compare_expected_vs_actual(user_report, evidence)`：用 LLM 对比"应该发生什么"与"实际发生什么"
> 2. `trace_data_flow(traces, target_field)`：追溯某个数据字段在调用链中的变化
> 3. `query_db_state(query)`：直接查 demo-app 的 PG 数据库验证当前数据状态（**只读**，要加 SQL 白名单）
> 4. `check_permissions(user_id, resource_id)`：检查权限关系
> 
> 系统提示词强调：
> - 业务逻辑问题需要**对比"用户预期"与"系统实际行为"**
> - 权限问题要明确"谁访问了什么、应该不能访问什么"
> - 数据不一致问题要给出"数据 A 是 X，但应该是 Y"

**安全注意：** `query_db_state` 必须只接受 SELECT 语句，用 sqlparse 校验。

**验收：**
- LOGIC-001（越权）case 能识别出权限问题
- 整体评测准确率 ≥ 70%

---

### D28（周三）：评测调优 Day 1

#### 任务 28.1：失败案例分析（上午）

跑全量评测，把所有失败 case 列出来，逐个分析：

```powershell
uv run diagdoctor-bench run --suite all
# 看 report.md 中的 "Failed Cases Details"
```

对每个失败 case：
1. 看 Doctor 的输出哪里不对
2. 看 LangSmith trace（或本地日志）找出问题节点
3. 判断是 Prompt 问题、工具问题、还是 RAG 召回问题

#### 任务 28.2：Prompt 优化（下午）

针对失败模式，优化对应 Agent 的 Prompt。

**AI 提示词：**

> 我有一个失败的 case：
> 
> 【期望诊断】{...}
> 【Doctor 实际诊断】{...}
> 【失败原因】{...}
> 【当前 Prompt】{...}
> 
> 请提出 Prompt 优化建议，让 Doctor 能在这类 case 上给出正确诊断。
> 注意：不要让 Prompt 过度拟合这一个 case，要保持泛化性。

---

### D29（周四）：评测调优 Day 2 + 工具增强

#### 任务 29.1：工具增强

根据 D28 发现的问题，增强工具：
- 日志摘要更精炼（避免 LLM 上下文超载）
- Trace 可视化输出更清晰
- 添加缺失的工具（如果发现某些信息 Agent 拿不到）

#### 任务 29.2：再次评测

```powershell
uv run diagdoctor-bench run --suite all
uv run diagdoctor-bench compare --baseline <yesterday> --candidate <today>
```

**目标：** 准确率达到 75%+

---

### D30（周五）：Sprint 3 收尾

#### 任务 30.1：扩充评测集到 30+（上午）

继续补充配方，目标 30+ case。

#### 任务 30.2：Sprint 3 报告（下午）

更新 README + 写 Sprint 3 总结文档：`docs/sprint-3-summary.md`

**Sprint 3 最终验收：**
- [ ] 4 个核心 Agent 都实现：Triage、BackendLog、FrontendLog、Perf、Logic（+ TraceAgent 汇聚）
- [ ] 评测集 ≥ 30 case
- [ ] 整体准确率 ≥ 70%
- [ ] LangGraph 流程图可视化（mermaid）放进文档

---

# Sprint 4：代码定位 + 部署 + 演示（D31-D40）

## Week 7：CodeFixAgent + ReporterAgent + 历史案例

### D31（周一）：代码索引化

#### 任务 31.1：代码切片与索引（全天）

**AI 提示词：**

> 在 `doctor/src/knowledge/code_index.py` 实现 demo-app 代码的向量化索引：
> 
> 1. 用 tree-sitter 解析 Python 和 TypeScript：
>    ```python
>    from tree_sitter import Language, Parser
>    ```
> 
> 2. 按函数/类粒度切片：
>    ```python
>    class CodeChunk(BaseModel):
>        file_path: str
>        start_line: int
>        end_line: int
>        chunk_type: Literal["function", "class", "method"]
>        name: str
>        content: str
>        language: str
>    ```
> 
> 3. 索引流水线：
>    ```python
>    async def index_codebase(repo_path: Path):
>        chunks = []
>        for file in walk_source_files(repo_path):
>            chunks.extend(chunk_file(file))
>        
>        docs = [
>            Document(
>                page_content=f"{c.file_path}:{c.start_line}\n{c.content}",
>                metadata=c.model_dump(),
>            )
>            for c in chunks
>        ]
>        await vector_kb.add_documents("code_index", docs)
>    ```
> 
> 4. CLI 命令：`uv run python -m doctor.scripts.index_code --repo demo-app/`

**验收：**
- 索引完成后 Qdrant 中 `code_index` collection 有数据
- 查询 "list tasks" 能召回 `app/tasks/views.py` 的 list_tasks 函数

---

### D32-D33（周二-周三）：CodeFixAgent

#### 任务 32.1：CodeFixAgent 实现（D32 全天）

**AI 提示词：**

> 在 `doctor/src/graph/nodes/code_fix.py` 实现 CodeFixAgent：
> 
> ```python
> async def code_fix_node(state: DoctorState) -> dict:
>     # 1. 从 findings 中提取关键线索（文件名、函数名、错误模式）
>     clues = extract_code_clues(state["findings"])
>     
>     # 2. 代码 RAG 召回
>     candidate_chunks = []
>     for clue in clues:
>         results = await knowledge_service.search_code(clue, k=5)
>         candidate_chunks.extend(results)
>     
>     # 3. LLM 分析：哪段代码最可能是 bug 源
>     ranked = await llm_rank_candidates(candidate_chunks, state["findings"])
>     
>     # 4. 生成修复建议
>     fix_suggestion = await llm_generate_fix(ranked[0], state["findings"])
>     
>     # 5. 输出 Hypothesis
>     return {
>         "hypotheses": [Hypothesis(
>             summary=fix_suggestion.summary,
>             confidence=fix_suggestion.confidence,
>             affected_files=[ranked[0].file_path],
>             proposed_by="CodeFixAgent",
>         )]
>     }
> ```
> 
> 关键 Prompt（`templates/code_fix.j2`）：
> ```
> 你是代码定位专家。基于以下分析：
> 
> 【发现的问题】
> {{ findings }}
> 
> 【候选代码片段】
> {% for chunk in candidates %}
> --- {{ chunk.file_path }}:{{ chunk.start_line }}-{{ chunk.end_line }} ---
> {{ chunk.content }}
> {% endfor %}
> 
> 请：
> 1. 判断哪一段代码最可能是问题源（输出索引）
> 2. 指出具体的问题行
> 3. 给出修复建议（伪代码或具体代码 diff）
> 
> 输出 JSON: {chosen_index, problem_line, fix_summary, fix_code, confidence}
> ```

#### 任务 33.1：集成到主图（D33 上午）

更新 main_graph，所有专业 Agent → trace → code_fix → reporter

#### 任务 33.2：评测 + 调优（D33 下午）

跑评测，目标：affected_file 准确率 ≥ 70%

---

### D34（周四）：ReporterAgent + 历史案例

#### 任务 34.1：ReporterAgent（上午）

**AI 提示词：**

> 在 `doctor/src/graph/nodes/reporter.py` 实现 ReporterAgent：
> 
> ```python
> async def reporter_node(state: DoctorState) -> dict:
>     # 综合所有 findings 和 hypotheses 生成最终 DiagnosisReport
>     report = await llm_compose_report(
>         findings=state["findings"],
>         hypotheses=state["hypotheses"],
>         category=state["bug_category"],
>     )
>     return {"report": report}
> ```
> 
> Prompt 强调：
> - 输出必须结构化（DiagnosisReport schema）
> - root_cause 一句话讲清楚
> - fix_suggestion 给具体的、可执行的建议
> - evidence_chain 列出诊断依据

#### 任务 34.2：历史案例自动入库（下午）

**AI 提示词：**

> 在 `doctor/src/graph/nodes/case_store.py` 实现：
> 
> ```python
> async def case_store_node(state: DoctorState) -> dict:
>     report = state["report"]
>     if report and report.confidence > 0.6:
>         doc = Document(
>             page_content=f"{state['evidence']['user_report']}\n\n{report.root_cause}",
>             metadata={
>                 "case_id": state.get("case_id"),
>                 "category": report.bug_category,
>                 "affected_file": report.affected_file,
>                 "confidence": report.confidence,
>                 "timestamp": datetime.now().isoformat(),
>             }
>         )
>         await knowledge_service.index_diagnosis(doc)
>     return {}
> ```
> 
> 注意：避免污染——不要把低质量诊断入库（confidence < 0.6 跳过）。
> 
> 未来评测的 case 不应入库（避免数据泄漏），加 `skip_index=True` 标志判断。

---

### D35（周五）：W7 总评测 + 全面调优

跑全量评测，针对失败 case 持续优化 Prompt 和工具。

**目标：** 准确率 ≥ 75%，至少 40 个 case 通过。

---

## Week 8：部署 + 演示物料

### D36（周一）：K8s 部署配置

#### 任务 36.1：Helm Chart（全天）

**AI 提示词：**

> 在 `infra/helm/diagdoctor/` 创建 Helm Chart：
> 
> 1. `Chart.yaml`：版本 0.1.0
> 2. `values.yaml`：默认配置（镜像、端口、副本数、资源限制）
> 3. `templates/`：
>    - `deployment.yaml`：doctor-api Deployment
>    - `service.yaml`：ClusterIP Service
>    - `ingress.yaml`：可选 Ingress
>    - `configmap.yaml`：非敏感配置
>    - `secret.yaml`：敏感配置（注释引导用户用 sealed-secrets 或 external-secrets）
>    - `hpa.yaml`：HPA
>    - `_helpers.tpl`：标准 helper
> 4. NOTES.txt：部署后提示
> 
> 同时为 qdrant、loki、tempo 等基础设施提供 values overlay（复用社区 chart）。

#### 任务 36.2：部署文档（下午 2 小时）

写 `docs/deployment.md`：
- Docker Compose 部署（5 分钟）
- K8s 部署（用 Helm，15 分钟）
- 配置说明（所有环境变量）
- 升级与回滚

---

### D37（周二）：CI/CD Pipeline 完善

#### 任务 37.1：完整 CI/CD（全天）

**AI 提示词：**

> 在 `.github/workflows/` 完善 workflows：
> 
> 1. `ci.yml`（已有，补全）：
>    - 矩阵：Python 3.11、3.12
>    - 步骤：lint → typecheck → unit test → build images
>    - 缓存：uv cache、docker layer cache
> 
> 2. `eval.yml`：评测专用
>    - 触发：PR 修改 doctor/prompts/ 或 doctor/src/graph/
>    - 步骤：
>      - 启动 minikube 或 kind
>      - 部署 demo-app + 可观测性栈 + doctor
>      - 跑 smoke 评测
>      - 上传 report 到 PR 评论
>      - 如准确率下降超过 5%，失败
> 
> 3. `release.yml`：
>    - 触发：tag push（v*.*.*）
>    - 构建镜像并推送到 ghcr.io
>    - 打包 helm chart
>    - 创建 GitHub Release

---

### D38（周三）：完善文档

#### 任务 38.1：完整文档树（全天）

需要的文档：

```
docs/
├── README.md（顶层导航）
├── getting-started.md         # 快速开始
├── architecture.md            # 架构总览（含所有 Mermaid 图）
├── development.md             # 开发指南
│   ├── setup-dev-env.md
│   ├── adding-a-new-bug-recipe.md
│   ├── adding-a-new-agent.md
│   └── testing-guide.md
├── deployment.md              # 部署指南
├── api-reference.md           # API 参考
├── bug-recipe-guide.md        # Bug 配方编写指南
├── evaluation-guide.md        # 评测指南
├── prompt-engineering.md      # Prompt 工程实践
├── security-guide.md          # 安全说明
└── faq.md
```

**AI 提示词模板：**

> 我要写 `docs/architecture.md`，请基于以下信息生成完整文档：
> 
> 【项目代码结构】{...}
> 【已实现的 Agent】{...}
> 【数据流】{...}
> 
> 文档结构：
> 1. 概述
> 2. 系统架构图（Mermaid）
> 3. 各组件职责
> 4. 数据流（时序图）
> 5. 技术决策（为什么选 LangGraph、Qdrant 等）
> 6. 扩展点

---

### D39（周四）：演示视频 + PPT

#### 任务 39.1：演示视频录制（上午 4 小时）

脚本（10-12 分钟）：

| 时间 | 内容 |
|------|------|
| 0:00-1:00 | 项目动机：参考 DiagnosticAgent 痛点，DiagDoctor 想解决什么 |
| 1:00-2:30 | 整体架构：三个子系统 + 数据流 |
| 2:30-4:00 | 演示 1 - 后端 N+1：手动注入 → 跑 trigger → 查 Grafana 看证据 → Doctor 诊断 |
| 4:00-5:30 | 演示 2 - 前端崩溃：截图+console log 输入 → Doctor 定位代码行 |
| 5:30-7:00 | 演示 3 - 性能问题：用户报告慢 → Doctor 沿 Trace 找出慢 SQL |
| 7:00-8:30 | 评测体系：跑全量评测 → 看 HTML dashboard |
| 8:30-10:00 | 部署：一键 docker compose → 浏览器看到完整系统 |
| 10:00-11:30 | 技术亮点总结 + 与 DiagnosticAgent 对比 |
| 11:30-12:00 | 后续规划 + 致谢 |

**录制工具：** OBS Studio + Loom（备份）

#### 任务 39.2：PPT 制作（下午 4 小时）

按 [diagdoctor-from-scratch.md 附录 B](diagdoctor-from-scratch.md) 大纲制作。

---

### D40（周五）：最终验收 + 发布

#### 任务 40.1：全量测试（上午）

```powershell
# 完整跑一遍
make clean
make up
make demo-migrate; make demo-seed
uv run python -m bug_factory.cli full-all
uv run python -m benchmark.cli run --suite all
```

#### 任务 40.2：发布（下午）

- [ ] 打 tag v0.1.0
- [ ] 发布到 GitHub（如果是 public 仓库）
- [ ] 内部技术分享会

**Sprint 4 最终验收：**

- [ ] 评测集 ≥ 50 case，准确率 ≥ 75%
- [ ] Docker Compose 一键启动 ≤ 15 分钟（从 git clone 算起）
- [ ] K8s Helm Chart 完整
- [ ] 7+ 份完整文档
- [ ] 演示视频 10+ 分钟
- [ ] CI 全绿 + 评测 CI 集成

---

# 附录

## 附录 A：弱模型常见错误与规避方案

| 错误类型 | 表现 | 规避 |
|---------|------|------|
| 用旧版 API | SQLAlchemy 1.x 语法、Pydantic v1 | 在 Prompt 中明确版本要求 |
| 异步混乱 | 同步函数里 await、忘记 await | 提供模板代码 |
| 类型错误 | 大量 Any 或缺失 type hint | 配置 mypy strict + pre-commit |
| 配置硬编码 | 把 URL 写死在代码里 | 强制要求从 config 读取 |
| 错误吞掉 | except 后只 print | 强制用 raise from e |
| 测试缺失 | 只写功能不写测试 | 任务卡片明确要求测试覆盖率 |
| Prompt 过长 | 拼接巨型字符串 | 用 Jinja2 模板 |
| 文件路径硬编码 | "/home/user/..." | 用 Path + Settings |

## 附录 B：每日 Standup 模板

```markdown
## D{N} - YYYY-MM-DD

### 今日任务
- [ ] 任务 X.1
- [ ] 任务 X.2

### 完成情况
- [x] 已完成
- [ ] 未完成原因

### 评测数据（如有）
- 准确率：X%
- Token 消耗：$X
- 评测集大小：X

### 阻塞
- 

### 明日计划
- 
```

## 附录 C：AI Prompt 工程小贴士

向 AI 派发任务时，务必：

1. **明确技术栈版本**：Python 3.11+、SQLAlchemy 2.x、Pydantic v2、LangGraph latest
2. **提供项目结构上下文**：贴上 `tree` 命令的输出
3. **明确文件路径**：精确到要创建/修改的文件
4. **要求测试覆盖**：每个模块都要求配套单元测试
5. **要求 type hints**：强调 mypy strict 模式
6. **要求 docstring**：每个公开函数/类必须有 docstring
7. **要求错误处理**：明确异常类型、不允许吞掉异常
8. **要求日志**：关键路径必须打 structlog
9. **拒绝模糊回答**：要求完整的、可运行的代码（不要 "..." 省略）
10. **小步快跑**：每个任务卡片粒度 4-8 小时，太大就拆

## 附录 D：风险监控指标

每周五检查：

| 指标 | 阈值 | 应对 |
|------|------|------|
| Token 累计成本 | < $50/week | 超出立即开 LLM 缓存 + 减少评测频率 |
| 评测准确率 | 按 Sprint 目标 | 不达标延期 + 调优而非加新功能 |
| 评测 case 数 | 按 Sprint 目标 | 不达标增加 AI 辅助生成时间 |
| CI 通过率 | > 90% | 不达标暂停新功能修 CI |
| 文档完整度 | 见 D38 清单 | 不达标 D40 加班补 |

---

## 最后再次提醒

> **8 周时间不长，但有 AI 辅助足够。**
> 
> 关键执行原则：
> 1. **每日一个任务卡片**：不超过 1 天就要完成一个可验收的东西
> 2. **每周一次 demo**：周五必须有可以演示给别人看的东西
> 3. **拒绝完美主义**：80 分能演示比 100 分演示不了强
> 4. **遇到卡 4 小时就求助**：换思路、求助 AI、求助同事
> 5. **保持文档同步**：代码改了，文档同时改
> 
> 祝顺利！
