# Bug Case 质量审查报告 & 改进方案

> 审查日期：2026-06-27  
> 审查范围：`bug-factory/recipes/gold/` 下全部 13 个配方 + 已生成的 evidence 文件  
> 审查维度：真实性 / 可用性 / 价值证明

---

## 一、总览：当前 13 个 Case 的分类与可诊断性矩阵

| Case ID | 类别 | 难度 | 可诊断性 | 核心瓶颈 |
|---------|------|------|---------|---------|
| BE-020 | backend_error | L1 | 🟢 高 | — |
| BE-021 | backend_error | L2 | 🟢 高 | — |
| BE-022 | backend_error | L3 | 🟡 中 | 需识别"仅未分配任务才崩"的因果 |
| FE-020 | frontend_crash | L1 | 🟢 高 | 当前为纯前端；**建议升级为跨层 case** |
| FE-021 | frontend_crash | L2 | 🟢 高 | — |
| PERF-020 | performance | L1 | 🟡 中 | Trace 缺少 DB 子 span，N+1 只能靠数 log 行 |
| PERF-021 | performance | L2 | 🟡 中 | 同上 |
| LOGIC-020 | logic | L2 | 🔴 低 | 证据中无非错误信号，无法区分正常/越权 |
| LOGIC-021 | logic | L3 | 🔴 低 | 同上 |
| LOGIC-022 | logic | L2 | 🔴 低 | status 被静默丢弃但 API 返回 200 |
| DATA-020 | data | L2 | 🔴 低 | 排序反向但 API 返回 200，无信号 |
| DATA-021 | data | L2 | 🔴 低 | due_date 丢弃但 API 返回 201，无信号 |
| CONFIG-020 | config | L3 | 🟡 中 | 能识别 401 但难区分"即时过期"vs"密钥错误" |

> 图例：🟢 大概率可诊断（5/13）| 🟡 有难度但可能（4/13）| 🔴 几乎不可诊断（4/13）

---

## 二、维度一：真实性（Authenticity）

### 2.1 已有优势

| 方面 | 表现 |
|------|------|
| 错误堆栈 | BE-020 含完整 asyncpg → SQLAlchemy → FastAPI 三层堆栈，带 PostgreSQL 原生 `DETAIL` 信息 |
| 前端错误 | FE-020/021 的 `browser_errors.json` 含 React component stack + Vite source URL，形态与 Sentry/DataDog RUM 一致 |
| 结构化日志 | 统一 JSON 格式 + `service_name`/`level`/`event` 字段，符合 structlog + OTel 最佳实践 |
| Trace 语义 | spans 含 `span_id`/`parent_span_id`/HTTP semantic conventions |

### 2.2 差距与改进

#### 差距 1：Trace 缺少 DB instrumentation span（影响 PERF 全部 case）

**现状：** PERF-020 的 trace 中 `GET /api/projects/{id}/tasks` 总共 33.6ms，子 span 只有 `http receive`/`http send`/`connect`。没有 `db.statement` 类型的子 span 来展示 N+1 的 50 条 `SELECT comments WHERE task_id=$1`。

**企业对标：** 生产环境中 `opentelemetry-instrumentation-sqlalchemy` 会自动为每条 SQL 生成子 span，包含 `db.statement`、`db.name`、`db.operation` 等属性。

**改进方案：**

```yaml
# 在 demo-app/backend/app/observability.py 中补上 SQLAlchemy instrumentation
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

def init_otel():
    # ... existing setup ...
    SQLAlchemyInstrumentor().instrument(
        enable_commenter=True,       # 注入 SQL 注释以关联 trace
        commenter_options={},        # 可选配置
    )
```

**验收标准：** 重新跑 PERF-020 full 流程后，`traces.json` 中每个 `SELECT comments WHERE task_id=$1` 都是一个独立子 span，`parent_span_id` 指向 `GET /tasks`，`attributes` 中带 `db.statement`。

---

#### 差距 2：前端错误未关联 trace_id（影响 FE 全部 case + 跨层诊断）

**现状：** `browser_errors.json` 的 `trace_id`/`span_id` 字段全是 `null`，无法通过 trace ID 关联到同一次后端请求。

**企业对标：** RUM + APM 联动的核心机制就是通过 `traceparent` header 把浏览器 fetch 的 span 与后端 server span 串成同一棵 trace 树。Sentry/DataDog/Elastic APM 都支持这一点。

**改进方案（两处修改）：**

1. **bug-factory Playwright 端**：采集浏览器错误时，从同页面的 `performance.getEntries()` 或 OTel-JS 的 active span context 中提取 `trace_id`：

```python
# bug-factory/src/trigger.py 中的 Playwright 错误采集部分
page.on("pageerror", lambda err: browser_errors.append({
    ...
    "trace_id": current_trace_id_from_js_context,  # 从页面 JS context 获取
    "span_id": current_span_id_from_js_context,
}))
```

2. **demo-app 前端 `error-reporter.ts`**：在 `reportClientError()` 中从 OTel active context 提取 `trace_id`/`span_id` 打入日志：

```typescript
// demo-app/frontend/src/observability/error-reporter.ts
import { trace } from '@opentelemetry/api';

export function reportClientError(error: Error, componentStack?: string) {
  const activeSpan = trace.getActiveSpan();
  const spanContext = activeSpan?.spanContext();
  const errorPayload = {
    error: `[react_render] ${error.message}`,
    stack: error.stack,
    componentStack,
    trace_id: spanContext?.traceId ?? null,   // ← 补上
    span_id: spanContext?.spanId ?? null,     // ← 补上
  };
  // ... send to backend log channel ...
}
```

**验收标准：** 重新跑 FE-020 full 流程后，`browser_errors.json` 的 `trace_id` 非空，且与 `traces.json` 中对应 `GET /api/projects/{id}/tasks` 的 trace_id 一致。

---

#### 差距 3：逻辑/数据类 bug 缺乏"差异型"证据（影响 LOGIC/DATA 全部 5 个 case）

**现状：** LOGIC-020（IDOR）、LOGIC-022（status 静默丢弃）、DATA-020（排序反向）、DATA-021（due_date 丢弃）的证据全是正常 200 响应日志。真实企业排查越权或数据错误时，还会借助：
- 审计日志（谁在什么时间访问了什么资源）
- API 响应 diff（两次请求的响应 body 对比）
- 数据库快照对比（写入前后数据是否一致）

**改进方案：** 为逻辑/数据类 case 增加"差异型"证据采集。在 `bug-factory/src/evidence_collector.py` 或 trigger 阶段增加：

```python
# 方案 A：在 trigger 步骤后，额外采集一次"对比快照"
# 例如 LOGIC-020：admin 登录后 GET /api/projects/ → 记录返回的 project 数量
# 然后对比预期（admin 应该只能看到自己的项目，实际看到了 alice 的）

# 方案 B：在 evidence 目录新增 diff_evidence.json
{
  "type": "api_response_diff",
  "request_context": {
    "current_user": "admin@example.com",
    "endpoint": "GET /api/projects/",
    "expected_owner_filter": "owner_id == current_user.id"
  },
  "observation": {
    "returned_projects_count": 7,
    "current_user_owned_count": 3,
    "cross_user_projects": ["Alice 的私密项目", "Bob 的项目"],
    "discrepancy": "返回了非当前用户的项目，疑似 owner_id 过滤缺失"
  }
}
```

**验收标准：** LOGIC-020 的 evidence 目录下新增 `diff_evidence.json`，Doctor 的 Ingest 节点能识别 `discrepancy` 字段并生成 golden_signal。

---

## 三、维度二：可用性（Usability）

### 3.1 Doctor 能诊断什么？

| 信号类型 | 当前覆盖 | Doctor 能力 |
|---------|---------|------------|
| **异常/崩溃** | BE-020/021/022, FE-020/021 | ✅ 强——堆栈直接指向文件和行号 |
| **慢查询/N+1** | PERF-020/021 | 🟡 中——日志可数重复 SQL，但 trace 缺 DB span |
| **配置错误** | CONFIG-020 | 🟡 中——能识别 401 模式，但根因定位需 code_search |
| **逻辑越权** | LOGIC-020/021 | 🔴 弱——无错误信号，需要"差异型"证据 |
| **数据正确性** | LOGIC-022, DATA-020/021 | 🔴 弱——同上 |

### 3.2 "不冒烟 bug"的诊断路径设计

对于 LOGIC 和 DATA 类 case，Doctor 的诊断不能依赖"找错误"，而需要"找矛盾"。建议在 Doctor 的 Ingest 层增加以下 golden_signal 类型：

```python
# doctor/src/graph/state.py 中的 Signal 模型扩展
class SignalType(str, Enum):
    ERROR_LOG = "error_log"           # 异常日志（现有）
    ERROR_SPAN = "error_span"         # 错误 span（现有）
    SLOW_SPAN = "slow_span"           # 慢 span（现有）
    REPEATED_QUERY = "repeated_query" # 重复查询/N+1（现有）
    # === 新增 ===
    BEHAVIOR_MISMATCH = "behavior_mismatch"  # 行为与预期不符
    DATA_INVARIANT_BROKEN = "data_invariant_broken"  # 数据约束被破坏
    ACCESS_CONTROL_ANOMALY = "access_control_anomaly"  # 访问控制异常
    SILENT_DATA_LOSS = "silent_data_loss"  # 静默数据丢弃
```

对应地在 recipe 的 `expected_evidence` 中声明所需的证据类型：

```yaml
# 以 LOGIC-020 为例
expected_evidence:
  browser_errors: none
  frontend_spans: expected
  backend_signal: access_control_anomaly   # ← 新增信号类型
  symptom_tier: backend
  root_cause_tier: backend
  required_signal_types:                   # ← 新增：必有的 golden_signal 类型
    - access_control_anomaly
```

---

## 四、维度三：价值证明（Value Proof）

### 4.1 当前 Case 的难度分布问题

```
L1 (一眼看出)：3/13 = 23%  ← 偏高
L2 (需要推理)：7/13 = 54%
L3 (有迷惑性)：3/13 = 23%  ← 偏低
```

L1 case 的问题：BE-020 的堆栈直接写着 `ForeignKeyViolationError`，FE-020 的 browser_errors 直接给出 `TaskBoardPage.tsx:148`。**这些 case 用 `grep` + 肉眼 2 分钟就能解决，无法证明 AI Agent 的增量价值。**

### 4.2 缺失的"高价值"Case 类型

以下是目前完全没有覆盖、但在面试/演示中极具说服力的 case 类型：

| # | Case 类型 | 为什么有价值 | 建议 recipe ID |
|---|---------|------------|---------------|
| 1 | **跨层根因** | 前端崩溃但真因在后端 API 缺少字段。证明 Doctor 能跨前后端定位根因，而非给症状打补丁（如"加可选链"） | FE-020 升级 |
| 2 | **竞态条件** | 两个请求并发修改同一 task，后到的覆盖了先到的。需要分析时间线 + trace 并发关系 | RACE-020 |
| 3 | **间歇性故障** | 只在特定条件（高峰/特定数据/30%概率）才触发。需要统计模式识别 | INTER-020 |
| 4 | **级联故障** | 一个慢 SQL → task 列表超时 → 前端重试 3 次 → 放大后端负载。需要 trace 树 + 因果链分析 | CASCADE-020 |
| 5 | **缓存不一致** | Redis 缓存了旧的 task 状态，用户看到的是过期数据。需要对比缓存 + DB + API 响应 | CACHE-020 |
| 6 | **内存泄漏** | 长时间运行后响应变慢最终 OOM。需要分析时间序列指标 | MEM-020 |

### 4.3 最优先改进：FE-020 升级为跨层 Case

**当前定义：** FE-020 被归类为纯 `frontend_crash`，golden truth 的 `root_cause_tier: frontend`。

**问题：** 前端白屏是因为 `task.tags` 为 undefined，但**真正的根因**是后端 `GET /api/projects/{id}/tasks` 的 `TaskResponse` schema 不含 `tags` 字段。正确的修复应该是**在后端列表接口补齐 tags 字段**，而不是只在前端加可选链。

**改进后的 recipe 定义：**

```yaml
id: FE-020
title: 打开任务看板整页白屏，控制台报 Cannot read properties of undefined
category: frontend_crash       # 保留（用户感知是前端崩）
severity: critical
tags:
  - react
  - typeerror
  - undefined
  - cross-layer               # ← 新增
  - task-board
  - api-contract              # ← 新增
  - difficulty:L4             # ← 升级：从 L1 升到 L4

expected_diagnosis:
  root_cause: |
    前端 SortableTaskCard 渲染 task.tags.length 时 task.tags 为 undefined，
    其根因是后端 GET /api/projects/{id}/tasks 返回的 TaskResponse 列表中不含 tags 字段。
    这是 API 契约缺陷 —— 列表接口未返回关联数据，前端却假设其存在。
  affected_file: demo-app/backend/app/schemas/task.py  # ← 改：真因在后端 schema
  # 同时列出前端症状文件
  secondary_files:
    - demo-app/frontend/src/pages/TaskBoardPage.tsx
  fix_suggestion: |
    方案 A（治本）：在 GET /api/projects/{id}/tasks 的 TaskResponse 列表 schema 中补齐 tags 字段
    方案 B（治标）：前端渲染前判空 task.tags?.length ?? 0
    建议两者都做：后端补齐契约 + 前端防御性判空
  fix_keywords:
    - TaskResponse
    - tags
    - 契约
    - selectinload
    - 判空
    - 可选链

# expected_evidence 更新
expected_evidence:
  browser_errors: expected
  frontend_spans: expected
  backend_signal: 'TaskResponse schema missing tags field in list endpoint'
  symptom_tier: frontend      # 用户看到的是白屏
  root_cause_tier: backend    # ← 改：真因在后端
  cross_layer: true           # ← 新增

# categories 更新为多标签
categories:
  - frontend_crash            # 症状分类
  - backend_error             # ← 真因分类

evaluation:
  must_mention_keywords:
    - task.tags
    - undefined
    - TaskResponse            # ← 新增
  should_mention_keywords:
    - 可选链
    - 判空
    - 契约                   # ← 新增
    - selectinload
    - 后端补齐               # ← 新增
  llm_judge_criteria: |
    诊断必须区分「症状」与「根因」：
    - 症状：前端 SortableTaskCard 读取 task.tags.length 时 undefined 崩溃
    - 根因：后端列表接口 TaskResponse schema 不含 tags 字段（API 契约缺陷）
    - 仅给出"加可选链"的症状补丁、未定位到后端 schema 的，记低分（≤ 0.4）
    - 同时给出后端补齐 + 前端判空双重建议的，记高分（≥ 0.8）
  min_confidence: 0.6
  cross_layer_required: true  # ← 新增：必须跨层诊断才算通过
```

**验收标准：**
- Doctor 诊断 FE-020 时，`affected_file` 包含后端 schema 文件（而不仅是前端文件）
- `finding` 中标记 `cross_layer=True`
- LLM Judge 能区分"只修前端"和"定位到后端根因"的得分差异

### 4.4 建议的优先级路线图

```
Phase 1（本周，D15 前）———— 修证据基础设施
├── 补 SQLAlchemy DB instrumentation span（解决 PERF case 可诊断性）
├── 前端 browser_errors 补 trace_id/span_id 关联
└── 为 LOGIC/DATA case 新增 diff_evidence.json 采集

Phase 2（下周，D20 前）———— 调整已有 case
├── FE-020 升级为跨层 case（旗舰 demo case）
├── BE-022 保持 L3（red-herring 已有价值）
├── LOGIC-020/021 补 diff_evidence → 可诊断性从 🔴→🟡
└── 降低 L1 case 在评测中的权重，或不作为正式评测 case

Phase 3（Sprint 3，D21-D30）———— 新增高价值 case
├── RACE-020：竞态条件（两个用户同时更新同一 task）
├── CASCADE-020：级联故障（慢 SQL → 超时 → 前端重试风暴）
└── INTER-020：间歇性故障（30% 概率触发，需统计模式识别）

Phase 4（Sprint 4，D31+）———— 补齐到 30+ case
├── CACHE-020：缓存不一致
├── 更多跨层 case（前端现象 + 后端根因的组合矩阵）
└── 环境差异导致的配置漂移 case
```

---

## 五、Recipe 级别的具体修改清单

### 5.1 需要修改证据采集的 Case

| Case | 问题 | 具体修改 |
|------|------|---------|
| PERF-020 | Trace 缺 DB span | 在 `demo-app/backend/app/observability.py` 启用 `SQLAlchemyInstrumentor` |
| PERF-021 | 同上 | 同上 |
| FE-020 | browser_errors 缺 trace_id | `error-reporter.ts` 中从 active span context 提取 |
| FE-021 | 同上 | 同上 |
| LOGIC-020 | 无差异信号 | trigger 后追加 API 响应对比采集，生成 `diff_evidence.json` |
| LOGIC-021 | 同上 | 同上 |
| LOGIC-022 | 同上 | trigger 后 PATCH → GET 对比 status 是否真正变更 |
| DATA-020 | 同上 | 对比返回列表的顺序是否与创建顺序一致 |
| DATA-021 | 同上 | POST 后 GET 对比 due_date 是否被保存 |

### 5.2 需要调整 YAML 定义的 Case

| Case | 字段 | 旧值 | 新值 | 原因 |
|------|------|------|------|------|
| FE-020 | `expected_diagnosis.affected_file` | `TaskBoardPage.tsx` | `app/schemas/task.py` | 真因在后端 |
| FE-020 | `expected_evidence.root_cause_tier` | `frontend` | `backend` | 同上 |
| FE-020 | `categories` | `[frontend_crash]` | `[frontend_crash, backend_error]` | 跨层多标签 |
| FE-020 | `difficulty` | `L1` | `L4` | 跨层诊断是核心难点 |
| FE-020 | `evaluation.min_confidence` | `0.6` | `0.5` | 跨层更难，降低门槛 |
| BE-020 | `difficulty` | `L1` | `L1` | 保留（作为 smoke case） |
| LOGIC-020 | `expected_evidence.backend_signal` | `unexpected behavior` | `access_control_anomaly` | 精确化信号类型 |
| LOGIC-021 | 同上 | 同上 | `access_control_anomaly` | 同上 |
| LOGIC-022 | 同上 | 同上 | `silent_data_loss` | 精确化 |
| DATA-020 | 同上 | 同上 | `data_invariant_broken` | 精确化 |
| DATA-021 | 同上 | 同上 | `silent_data_loss` | 精确化 |

### 5.3 建议移除或降级为 smoke-only 的 Case

| Case | 原因 | 建议 |
|------|------|------|
| BE-020 | 错误堆栈直接暴露根因，grep 级难度 | 保留在 smoke 集，不纳入 blind 评测集 |
| FE-020（当前版） | 同上，browser_errors 直接给出文件+行号 | 升级为跨层版后保留 |

---

## 六、评测体系影响

### 6.1 评测集分层

建议将评测集分为三层：

```
benchmark/cases/
├── smoke/          # 冒烟集（≤8 case）
│   ├── BE-020.yaml     # L1 - 快速验证流水线
│   ├── FE-021.yaml     # L2 - 前端基础
│   └── PERF-020.yaml   # L1 - 性能基础
│
├── train/          # 训练集（用于调优 Prompt，可过拟合）
│   ├── BE-021.yaml
│   ├── BE-022.yaml
│   ├── CONFIG-020.yaml
│   └── ...
│
└── blind/          # 盲测集（门禁真值，调优期不可见）
    ├── FE-020-cross-layer.yaml  # 旗舰跨层 case
    ├── RACE-020.yaml            # 竞态（Phase 3）
    ├── CASCADE-020.yaml         # 级联（Phase 3）
    └── ...
```

### 6.2 门禁口径调整

```yaml
# 门禁指标权重建议（当前 vs 调整后）
overall_score:
  # 旧：所有 case 等权
  # 新：按 case 难度加权
  weights:
    L1: 0.1     # 简单 case 降低影响
    L2: 0.3
    L3: 0.4
    L4: 0.2     # 跨层/复杂 case

  # 关键：cross_layer case 必须达标
  gates:
    - cross_layer_recall >= 0.5   # 跨层 case 中能定位到真因层的比例
    - overall_weighted >= 0.70    # 加权总分
```

---

## 七、总结：改进行动项

| # | 行动项 | 优先级 | 预估工时 | 影响范围 |
|---|--------|--------|---------|---------|
| 1 | 启用 SQLAlchemy instrumentation，trace 中生成 DB 子 span | 🔴 P0 | 2h | PERF-020/021 可诊断性 🟡→🟢 |
| 2 | `error-reporter.ts` 中补 `trace_id`/`span_id` 到 browser_errors | 🔴 P0 | 1h | FE 全部 case 的跨层关联能力 |
| 3 | FE-020 升级为跨层 case（修改 YAML + expected_evidence） | 🔴 P0 | 1h | 旗舰 demo case |
| 4 | 为 LOGIC/DATA 5 个 case 新增 `diff_evidence.json` 采集 | 🟡 P1 | 4h | 逻辑/数据类 case 可诊断性 🔴→🟡 |
| 5 | 在 Ingest 层新增 `BehaviorMismatch`/`AccessControlAnomaly` 等信号类型 | 🟡 P1 | 3h | Doctor 能处理"不冒烟"bug |
| 6 | 评测集分层（smoke/train/blind）+ 难度加权 | 🟢 P2 | 2h | 评测公平性 |
| 7 | 新增 RACE-020（竞态）、CASCADE-020（级联故障） | 🟢 P2 | 6h | 丰富高价值 case |
| 8 | 补 `browser_errors.json` 的 `component_stack`/`breadcrumbs` 字段（当前为 null） | 🟢 P2 | 2h | 前端错误上下文更丰富 |

> **关键原则：先把 P0 三项修完再跑评测，否则评测分数不能反映 Doctor 的真实能力上限。**
