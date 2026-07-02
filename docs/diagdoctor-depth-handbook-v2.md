# DiagDoctor 深度化执行手册

> 配套文档：[diagdoctor-depth-directions-v2.md](./diagdoctor-depth-directions-v2.md)（14 个深度方向）
> 架构参考：[learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)（harness 工程方法论）
> 文档定位：**基于当前已实现的 V3 基线，按优先级渐进式深度化的逐日任务卡片**

---

## 当前实现状态（基线盘点）

V3 基线架构**已完整实现**，本手册不重复 V3 重构工作，而是聚焦于深度化。

### ✅ 已完成

| 模块 | 文件 | 状态 |
|------|------|------|
| 图拓扑 | `graph/main_graph.py` | ✅ 3 节点（ingest → unified_agent → reporter） |
| Ingest（采集+标准化） | `graph/nodes/ingest.py` + `ingest/normalizer.py` | ✅ 两阶段：① auto-prefetch 并行采集 Loki/Tempo（后端+前端）→ ② 9 步标准化管线 |
| 前端错误采集 | `main.tsx` + `error-reporter.ts` + `otel-logs.ts` | ✅ 双通道：`console.error` → Loki（OTLP 日志）、`window.onerror` → Tempo（client_error span） |
| UnifiedAgent | `graph/subgraphs/unified_agent.py` + `graph/nodes/unified_agent.py` | ✅ 手动 ReAct 循环 + 5 工具 + 证据格式化（不负责数据获取） |
| 5 个工具 | `tools/__init__.py` | ✅ search_observability（含 include_frontend）/ code_search（ripgrep）/ db_query / inspect_frontend_error / get_file_content |
| 信号提取 | `ingest/signal_extractor.py` | ✅ 黄金信号分类（error_log / error_span / slow_span / repeated_query / browser_error）+ Span 级 N+1 检测 |
| 安全层 | `security/` | ✅ sql_guard（SELECT-only）+ sanitizer（路径沙箱）+ secrets（脱敏） |
| System Prompt | `prompts/templates/unified_agent.j2` | ✅ 静态 3 步策略 + 工具选择表 |
| 评测器 | `benchmark/evaluators/` | ✅ 自研 exact_match / keyword_match / llm_judge / efficiency → **迁移至 Langfuse** |
| Bug 配方 | `bug-factory/recipes/gold/` | ✅ 15 个 YAML（7 类别） |
| API | `api/diagnose.py` | ✅ POST /api/diagnose + SSE 流式 |
| LLM 工厂 | `llm_factory.py` | ✅ 分层模型（triage / specialist / default） |

### ❌ 待深度化（本手册覆盖）

| 缺口 | 影响 | 对应方向 |
|------|------|---------|
| 评测体系自研维护成本高 | 评测维度扩展困难、无可视化 Dashboard | **方向 5：Langfuse 替代自研 benchmark** |
| Agent 循环是黑盒 | 无法注入 harness 机制 | 方向 0：手动 Agent 循环 |
| 工具结果零管控 | context 爆炸 → 推理质量退化 | 方向 4：上下文工程 |
| code_search 用向量检索 | 精确匹配能力弱 | 方向 3：ripgrep |
| search_observability 无异常检测 | 只给数据不给洞察 | 方向 2 |
| ~~Ingest 无置信度评分~~ | ~~Agent 无法判断信号可靠性~~ → **已移除**（设计决策：置信度判断应交由 LLM） | ~~方向 1~~ |
| 评测维度耦合 | 无法定位退化点 | 方向 5：Langfuse Scoring 解耦 |
| System Prompt 静态化 | 不随诊断进展调整策略 | 方向 6 |
| 无诊断计划 | Agent 容易漂移 | 方向 7：TodoWrite |
| 无 Hook 扩展点 | 工具调用不可拦截 | 方向 12 |
| 无自省机制 | 幻觉和误诊无纠正 | 方向 10 |
| Bug 覆盖面有限 | 评测不充分 | 方向 8 |
| 无 Subagent 隔离 | 复杂 case context 膨胀 | 方向 13 |
| Doctor LLM 调用链不可见 | token/cost/工具调用全盲 | **Doctor 可观测：Langfuse Tracing** |

---

## 数据流全景（V3 基线）

### Ingest → UnifiedAgent 渐进式信息披露

```
┌─ ingest（首轮广撒网）──────────────────────────────┐
│  trigger_time ±5min                                │
│  ┌─────────────────────────────────────────────┐   │
│  │ asyncio.gather（并行）                        │   │
│  │  ├─ Loki: {service_name=~"demo-backend"}    │   │
│  │  ├─ Loki: {service_name=~"demo-frontend"}   │   │
│  │  └─ Tempo: auto 提取 trace_id → 拉全量 span  │   │
│  └─────────────────────────────────────────────┘   │
│          ↓                                         │
│  9 步标准化管线（denoise→dedup→signals→correlate）  │
│          ↓                                         │
│  NormalizedEvidence（压缩后）→ unified_agent       │
└────────────────────────────────────────────────────┘
                        ↓
┌─ unified_agent（按需精准深挖）──────────────────────┐
│  已有 golden_signals + correlations 开局上下文       │
│  LLM 按需调工具：                                    │
│    search_observability → 查特定 trace_id/spans     │
│    code_search / get_file_content → 定位代码         │
│    inspect_frontend_error → 分析前端错误             │
│    db_query → 验证数据状态                            │
└────────────────────────────────────────────────────┘
```

> **设计意图**：Ingest 给 Agent 一个全局概览（足够判断大方向），Agent 需要细节时自己用工具深挖。
> 避免两种极端：① 全量日志塞 prompt（token 爆炸）② 完全不预取（Agent 第一轮空转）。

### 前端错误双通道

```
前端 JS 错误
  ├─ window.onerror / unhandledrejection
  │    → error-reporter.ts → client_error span → OTLP → Tempo
  │    → Doctor 通过 search_observability(source="tempo") 查询
  │
  └─ console.error(msg)   ← main.tsx monkey-patch
       → otel-logs.ts → OTLP → Loki (severity=ERROR)
       → Doctor 通过 search_observability(source="loki",
            query='{service_name=~"demo-frontend"}') 查询
```

> - `window.onerror`/`unhandledrejection` → Tempo `client_error` span（与 API trace_id 关联，可跨层追踪）
> - `console.error` → Loki ERROR 日志（React 渲染期间的显式 console.error 也被捕获）
> - ingest auto-prefetch 并行拉取两条通道的数据，`search_observability` 工具的 `include_frontend=True` 参数可主动查询前端 `client_error` span

---

## 设计原则

1. **循环优先** — 方向 0（手动 Agent 循环）是所有 harness 机制的前提，必须最先完成
2. **每项改动必须可度量** — 没有验收标准的任务不立项
3. **渐进式叠加** — 每个 Phase 完成后系统仍可运行，不破坏已有能力
4. **harness 优先** — 上下文工程、Hook、TodoWrite 等 harness 机制是 Agent 质量的地基
5. **对照 learn-claude-code** — Agent 产品 = Model + Harness，我们的工作是建好 harness
6. **评测与监测分离** — Langfuse 负责 Doctor 评测 + LLM 可观测；OTel 负责 Doctor 服务级监测；Demo App 监控栈不动

---

## 总览：4 个 Phase

```
Phase 0 (基线验证)     → 确认当前 V3 能跑通 + 部署 Langfuse + 建立度量基线
    ↓
Phase 1 (P0 地基)      → 手动循环 + ripgrep + 上下文工程 + Ingest 深度 + Observability 深度
    ↓                      Agent 推理质量的地基，直接提升诊断准确率
Phase 2 (P1 质量与评测) → Langfuse 评测体系 + Prompt 策略化 + TodoWrite + Bug Factory 扩展
    ↓                      可量化、可回归、可追踪
Phase 3 (P2 鲁棒性)    → 安全纵深 + Agent 自省 + Langfuse 成本追踪 + Hook 系统 + Subagent
                           生产级鲁棒性
```

### Phase 总览表

| Phase | 优先级 | 工作日 | 核心目标 | 验收标志 |
|-------|--------|--------|---------|---------|
| **Phase 0** | 基线 | 2d | 确认 V3 可跑通 + 部署 Langfuse + 建立度量基线 | 15 case 全跑通，Langfuse 基线 Experiment 生成 |
| **Phase 1** | P0 | 10d | 手动循环 + 上下文工程 + Ingest/search/code_search 深度 | overall ≥ 基线 +15% |
| **Phase 2** | P1 | 13d | Langfuse 评测体系 + Prompt 策略 + TodoWrite + Bug 扩展 | 多维度 Scoring 可用 + overall ≥ 基线 +25% |
| **Phase 3** | P2 | 11d | 安全 + 自省 + Langfuse 成本追踪 + Hook + Subagent | 无幻觉 case + LLM 调用链全量可观测 |
| **总计** | | **36d** | | |

---

# Phase 0: 基线验证（D1-D2）

> **目标**：确认当前 V3 架构能端到端跑通，部署 Langfuse（自托管），建立所有后续优化的度量基线。
> **验收目标**：15 个 gold case 全部跑通（不要求准确），Langfuse 基线 Experiment 可查看。
> **监测架构变更**：Doctor 增加 Langfuse Tracing（LLM 调用链）+ 保留 OTel（服务级）；Demo App 监控栈不变。

### 监测架构说明

```
┌─────────────────────────────────────────────────┐
│                  Doctor 监测                      │
│  ┌──────────────────┐  ┌──────────────────────┐ │
│  │  Langfuse        │  │  OTel (保留)         │ │
│  │  • LLM 调用 Trace│  │  • API 延迟/错误率   │ │
│  │  • Token/Cost    │  │  • Python 进程指标   │ │
│  │  • Tool 调用追踪 │  │  • DB 查询耗时       │ │
│  │  • RAG Retrieval │  │  • → Grafana 看板   │ │
│  │  • Prompt 版本   │  │                      │ │
│  └──────────────────┘  └──────────────────────┘ │
│       LLM 级可观测           服务级可观测         │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│              Demo App 监测 (不变)                 │
│  OTel + Loki + Tempo + Grafana                  │
│  系统级指标 + 日志聚合 + 分布式调用链             │
└─────────────────────────────────────────────────┘
```

---

### 任务 0.0：部署 Langfuse + 给 Doctor 加 Tracing（优先于 D1）

**操作步骤**：

```bash
# 1. 在 docker-compose.yml 中增加 Langfuse 服务
#    （参考 https://langfuse.com/self-hosting/docker-compose）
#    langfuse-server + langfuse-postgres

# 2. 启动 Langfuse
docker compose up -d langfuse-server langfuse-postgres

# 3. 安装 Langfuse Python SDK
cd doctor && uv add langfuse langfuse-langchain

# 4. 在 doctor/src/observability/ 新增 langfuse_tracing.py
#    创建 CallbackHandler，集成到 LangGraph agent.ainvoke() 的 config.callbacks
```

**Doctor 观测架构文件**：`doctor/src/observability/langfuse_tracing.py`

```python
from langfuse.langchain import CallbackHandler
from src.config import settings

def get_langfuse_handler() -> CallbackHandler:
    return CallbackHandler(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )
```

**在 Agent 循环中注入**（`unified_agent.py`）：

```python
langfuse_handler = get_langfuse_handler()
response = await llm.ainvoke(messages, tools=tools,
    config={"callbacks": [langfuse_handler]})
```

> **说明**：Langfuse callback 自动捕获每次 LLM 调用的 input/output/token/cost/model，
> 以及 tool 调用的 name/args/result，无需修改 graph 业务逻辑。
> OTel（`observability/__init__.py`）保留不变，负责 HTTP 请求级 trace。

**验收**：
- Langfuse Dashboard 可访问（`http://localhost:3000` 或自托管端口）
- Doctor 诊断一次后，Langfuse Traces 页面出现完整调用链
- Trace 中包含：LLM 调用次数、每次 input/output token、tool 调用明细

---

## D1：端到端冒烟测试

### 任务 0.1：全栈启动 + 数据准备（上午 3h）

**操作步骤**：

```bash
# 1. 启动全栈
make up

# 2. 等待服务就绪
docker compose ps  # 确认所有服务 healthy

# 3. 数据库迁移 + 种子数据
make demo-migrate
make demo-seed

# 4. 初始化知识库（code index）
cd doctor && uv run python scripts/init_kb.py

# 5. 启动 Doctor API
cd doctor && uv run uvicorn src.main:app --port 8001 --reload
```

**验收**：
- `curl http://localhost:8001/health` 返回 200
- `curl http://localhost:8001/docs` 可访问 OpenAPI 文档
- Qdrant `code_index` collection 存在且有数据

---

### 任务 0.2：逐个跑 15 个 gold case（下午 3h）

**操作步骤**：

```bash
# 对每个 case：先注入 bug → 触发 → 收集证据 → 诊断
for case in BE-020 BE-021 BE-022 FE-020 FE-021 PERF-020 PERF-021 \
            LOGIC-020 LOGIC-021 LOGIC-022 DATA-020 DATA-021 \
            CONFIG-020 RACE-020 CASCADE-020; do
    echo "=== Running $case ==="
    python -m bug_factory.cli full "$case"
done
```

**记录每个 case 的**：
- 是否跑通（无崩溃）
- Agent 调用了哪些工具（从 retrieval_trace）
- 工具调用次数
- 诊断报告是否非空
- 初步判断是否正确（人工查看）

**验收**：
- ≥ 12/15 case 跑通（允许 3 个因环境问题失败）
- 每个 case 的 `report` 字段非空
- 无 Agent 无限循环或超时

---

## D2：Langfuse 数据集导入 + 基线 Experiment

> **关键理解**：Langfuse Dataset 只存储"考题 + 标准答案"，**不会自动运行 Doctor**。
> Doctor 的工具（`search_observability`、`code_search`、`db_query`）是**实时查询活系统**的——
> 它依赖 Loki 中有日志、Tempo 中有 Trace、demo-app 源码已被 Bug 修改。
>
> **证据收集不需要了**——bug-factory 只负责"布置考场"（inject + trigger），
> 然后把**触发时间窗口**告诉 Doctor，Doctor 自己调 `search_observability` 去 Loki/Tempo 实时查。
> 这样更贴近真实诊断场景：用户报告问题时，Doctor 现场去查日志和 Trace。
>
> ```
> ┌─ Langfuse Dataset（静态）──────────────┐  ┌─ Experiment task（动态）──────────────────────────┐
> │                                        │  │                                                   │
> │  item[0]: {                            │  │  ① git checkout main（确保干净起点）              │
> │    input:  { user_report: "..." }      │  │  ② bug_factory inject BE-020（修改源码）           │
> │    expected: { root_cause: "..." }     │  │  ③ bug_factory trigger BE-020（发请求产生日志）    │
> │    metadata: { bug_id: "BE-020" }      │  │  ④ 记录 trigger_time → 传给 Doctor               │
> │  }                                     │  │  ⑤ POST /api/diagnose {user_report, trigger_time} │
> │                                        │  │     → Doctor 调 search_observability 实时查 Loki   │
> │  item[1]: { ... }                      │  │  ⑥ Scorer 对比 expected vs 实际输出               │
> │                                        │  │  ⑦ git checkout main（恢复现场）                   │
> └────────────────────────────────────────┘  └───────────────────────────────────────────────────┘
> ```

---

### 任务 0.3：将 15 个 gold case 导入 Langfuse Dataset（上午 2h）

> **设计决策**：直接从 `bug-factory/recipes/gold/*.yaml` 导入，跳过 `output/*/case.yaml` 中间产物。
> 配方是唯一权威源：`title` 即 user_report，`expected_diagnosis` 即标准答案，`injection`/`trigger` 供 Experiment task 使用。
> 不需要先跑 bug-factory 生成 case.yaml 再导入——配方 → Langfuse，一跳直达。

**创建导入脚本**：`doctor/scripts/import_cases_to_langfuse.py`

```python
"""将 bug-factory/recipes/gold/*.yaml 直接导入 Langfuse Dataset。

配方是唯一权威源 —— 包含 title（即 user_report）+ expected_diagnosis（标准答案）。
不需要经过 output/*/case.yaml 中间产物，一跳直达。
"""
from langfuse import Langfuse
from pathlib import Path
import yaml
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.config import settings

RECIPES_DIR = PROJECT_ROOT.parent / "bug-factory" / "recipes" / "gold"

langfuse = Langfuse(
    secret_key=settings.langfuse_secret_key,
    public_key=settings.langfuse_public_key,
    host=settings.langfuse_host,
)

langfuse.create_dataset(name="diagdoctor-benchmark")

for recipe_file in sorted(RECIPES_DIR.glob("*.yaml")):
    recipe = yaml.safe_load(recipe_file.read_text(encoding="utf-8"))
    expected = recipe["expected_diagnosis"]

    langfuse.create_dataset_item(
        dataset_name="diagdoctor-benchmark",
        input={
            "user_report": recipe["title"],  # title 就是 user_report
        },
        expected_output={
            "category": recipe.get("categories", [recipe["category"]]),
            "root_cause": expected.get("root_cause", ""),
            "affected_file": expected.get("affected_file", ""),
            "fix_suggestion": expected.get("fix_suggestion", ""),
            "fix_keywords": expected.get("fix_keywords", []),
        },
        metadata={
            "bug_id": recipe["id"],
            "recipe_id": recipe["id"],
            "difficulty": recipe.get("difficulty", "L2"),
        },
        idempotency_key=f"recipe-{recipe['id']}",
    )
```

**运行**：

```bash
cd doctor && uv run python scripts/import_cases_to_langfuse.py
```

**验收**：
- Langfuse Dashboard → Datasets → `diagdoctor-benchmark` 可见 15 个 item
- 每个 item 的 input/expected_output/metadata 字段完整

---

### 任务 0.4：跑基线 Experiment + 配置初版 Scorer（下午 3h）

> **核心思路**：bug-factory 只负责"布置考场"（inject + trigger），不收集证据。
> 触发完成后，把**触发时间窗口**告诉 Doctor，Doctor 自己调 `search_observability` 去 Loki/Tempo 实时查。
> 这更贴近真实场景——用户报告问题后，Doctor 现场去查日志和 Trace。

**创建 Experiment 脚本**：`scripts/run_baseline_experiment.py`

```python
"""Langfuse 基线 Experiment：注入 Bug → 触发 → 诊断 → 打分 → 恢复。

与 bug-factory 的分工：
  - bug-factory：inject（改代码）+ trigger（发请求）——只"布置考场"
  - Doctor：search_observability（实时查 Loki/Tempo）——自己"收集证据"
  - Experiment：串联上述流程 + 打分 + 恢复现场

运行前确保：
  - demo-app backend 运行在 http://localhost:8000
  - Doctor API 运行在 http://localhost:8001
  - Loki/Tempo 可访问
  - 当前在 git main 分支且工作区干净
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from langfuse import Langfuse

# ── 路径常量 ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUG_FACTORY_DIR = PROJECT_ROOT / "bug-factory"

# ── 可配置参数 ─────────────────────────────────────────────────────────
DEMO_BACKEND_URL = "http://localhost:8000"
DOCTOR_URL = "http://localhost:8001"
RELOAD_WAIT = 5         # uvicorn reload 等待秒数
DIAGNOSE_TIMEOUT = 120   # 单次诊断超时秒数
LOKI_INDEX_DELAY = 3     # Loki/Tempo 索引延迟（触发后等这么久再调 Doctor）

langfuse = Langfuse()


# ═══════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════

def run_cmd(cmd: list[str], cwd: Path | None = None) -> None:
    """运行命令，失败时抛出异常。"""
    print(f"  > {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr[-500:] if result.stderr else ""
        raise RuntimeError(f"命令失败 (exit={result.returncode}): {stderr}")


def git_checkout_main() -> None:
    """切换到 main 分支，确保干净起点。"""
    run_cmd(["git", "checkout", "main"], cwd=PROJECT_ROOT)
    print("  ✓ 已切换到 main 分支")


def inject_bug(recipe_id: str) -> None:
    """注入 Bug：AI 修改源码 → 创建 bug/{id} 分支。"""
    run_cmd(
        ["uv", "run", "python", "-m", "bug_factory.cli", "inject", recipe_id],
        cwd=BUG_FACTORY_DIR,
    )


def trigger_bug(recipe_id: str) -> datetime:
    """触发 Bug：对 demo-app 发起请求，产生日志和 Trace。

    返回触发开始时间（UTC），供 Doctor 缩小 Loki/Tempo 查询窗口。
    """
    trigger_start = datetime.now(timezone.utc)
    run_cmd(
        [
            "uv", "run", "python", "-m", "bug_factory.cli", "trigger", recipe_id,
            "--base-url", DEMO_BACKEND_URL,
        ],
        cwd=BUG_FACTORY_DIR,
    )
    return trigger_start


async def wait_for_backend(url: str, max_wait: int = 30) -> bool:
    """等待后端就绪（Bug 注入后 uvicorn reload 需要时间）。"""
    async with aiohttp.ClientSession() as session:
        for _ in range(max_wait):
            try:
                async with session.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(1)
    return False


async def call_doctor(user_report: str, trigger_time: datetime) -> dict:
    """调用 Doctor API 执行诊断。

    传入 trigger_time，Doctor 的 search_observability 工具可用它缩小
    Loki/Tempo 查询窗口（trigger_time ± 5min），大幅减少噪音数据。
    Langfuse callback 自动记录 trace。
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{DOCTOR_URL}/api/diagnose",
            json={
                "user_report": user_report,
                # 关键：告诉 Doctor 触发时间，缩小可观测查询窗口
                "trigger_time": trigger_time.isoformat(),
            },
            timeout=aiohttp.ClientTimeout(total=DIAGNOSE_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Doctor API 返回 {resp.status}: {text[:500]}")
            return await resp.json()


# ═══════════════════════════════════════════════════════════════════════
# Experiment task（每个 case 执行一次）
# ═══════════════════════════════════════════════════════════════════════

async def diagnose_task(item: dict, trace_id: str) -> dict:
    """完整的"布置考场 → 诊断 → 清理"流水线。

    分工：
      - bug-factory：inject（改代码）+ trigger（发请求）
      - Doctor：search_observability（实时查 Loki/Tempo）+ code_search + db_query
      - Experiment：串联 + 打分 + 恢复现场
    """
    recipe_id = item["metadata"]["recipe_id"]
    user_report = item["input"]["user_report"]

    print(f"\n{'='*60}")
    print(f"  Case: {recipe_id}")
    print(f"  User Report: {user_report[:80]}...")
    print(f"{'='*60}")

    # ── Step 1: 恢复干净起点 ───────────────────────────────────────
    print("[1/4] 恢复 git main 分支...")
    git_checkout_main()

    # ── Step 2: 注入 Bug ──────────────────────────────────────────
    print(f"[2/4] 注入 Bug: {recipe_id}...")
    inject_bug(recipe_id)
    print(f"  等待 uvicorn reload ({RELOAD_WAIT}s)...")
    time.sleep(RELOAD_WAIT)

    if not await wait_for_backend(DEMO_BACKEND_URL):
        raise RuntimeError(f"Demo backend 未在 {RELOAD_WAIT+30}s 内就绪")
    print("  ✓ Demo backend 已就绪")

    # ── Step 3: 触发 Bug + 记录时间 ───────────────────────────────
    print(f"[3/4] 触发 Bug: {recipe_id}...")
    trigger_time = trigger_bug(recipe_id)
    print(f"  触发时间: {trigger_time.isoformat()}")
    print(f"  等待 Loki/Tempo 索引 ({LOKI_INDEX_DELAY}s)...")
    await asyncio.sleep(LOKI_INDEX_DELAY)

    # ── Step 4: 调用 Doctor（证据由 Doctor 自己实时查询） ─────────
    print(f"[4/4] 调用 Doctor API 诊断（传入 trigger_time={trigger_time.isoformat()}）...")
    try:
        diagnosis = await call_doctor(user_report, trigger_time)
    except Exception as exc:
        print(f"  ✗ 诊断失败: {exc}")
        diagnosis = {"error": str(exc), "report": "", "categories": [], "confidence": 0.0}

    print(f"  ✓ 诊断完成（categories={diagnosis.get('categories', [])}, confidence={diagnosis.get('confidence', 0)})")

    # ── 恢复现场 ──────────────────────────────────────────────────
    print("  恢复 git main 分支...")
    git_checkout_main()
    time.sleep(2)

    return diagnosis


# ═══════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════

async def main() -> None:
    print("="*60)
    print("  DiagDoctor 基线 Experiment (Phase 0)")
    print("="*60)

    # 前置检查
    print("\n── 前置检查 ──")
    async with aiohttp.ClientSession() as session:
        # Doctor API
        try:
            async with session.get(f"{DOCTOR_URL}/health") as resp:
                assert resp.status == 200
                print(f"  ✓ Doctor API: {DOCTOR_URL}")
        except Exception:
            print(f"  ✗ Doctor API 不可达: {DOCTOR_URL}")
            sys.exit(1)

        # Demo Backend
        try:
            async with session.get(f"{DEMO_BACKEND_URL}/health") as resp:
                assert resp.status == 200
                print(f"  ✓ Demo Backend: {DEMO_BACKEND_URL}")
        except Exception:
            print(f"  ✗ Demo Backend 不可达: {DEMO_BACKEND_URL}")
            sys.exit(1)

    # 确保在 main 分支
    git_checkout_main()

    # 获取 Dataset
    dataset = langfuse.get_dataset("diagdoctor-benchmark")
    items = list(dataset.items)
    print(f"\n  Dataset: diagdoctor-benchmark ({len(items)} items)")

    # 逐个运行（顺序执行，避免多个 case 同时修改代码冲突）
    print("\n── 开始逐个运行 case ──")
    results = []
    for i, item in enumerate(items):
        recipe_id = item.metadata.get("recipe_id", "unknown")
        print(f"\n{'─'*60}")
        print(f"  [{i+1}/{len(items)}] {recipe_id}")
        print(f"{'─'*60}")

        # 创建 Langfuse trace（关联到 Experiment）
        trace = langfuse.trace(
            name=f"baseline_phase0_{recipe_id}",
            metadata={"recipe_id": recipe_id, "run": "baseline_phase0"},
        )

        try:
            result = await diagnose_task(
                {
                    "input": {"user_report": item.input.get("user_report", "")},
                    "expected_output": item.expected_output or {},
                    "metadata": item.metadata or {"recipe_id": recipe_id},
                },
                trace_id=trace.id,
            )

            # ── 初版 Scorer ──
            expected = item.expected_output or {}

            # 1. category_accuracy
            pred_categories = set(result.get("categories", []))
            gold_categories = set(expected.get("category", []))
            if gold_categories:
                tp = len(pred_categories & gold_categories)
                precision = tp / len(pred_categories) if pred_categories else 0.0
                recall = tp / len(gold_categories)
                f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
            else:
                f1 = 1.0 if not pred_categories else 0.0

            trace.score(name="category_accuracy", value=f1)
            print(f"    category_accuracy: {f1:.2f}")

            # 2. affected_file_accuracy
            expected_file = expected.get("affected_file", "")
            actual_file = result.get("affected_file", "")
            file_hit = 1.0 if actual_file and expected_file and actual_file.endswith(expected_file) else 0.0
            trace.score(name="affected_file_accuracy", value=file_hit)
            print(f"    affected_file_accuracy: {file_hit:.2f}")

            # 3. efficiency（从 trace observations 中提取工具调用次数）
            #    注：trace.observations 在 trace 完成后由 Langfuse callback 填充
            #    此处先记录占位 score，Phase 2 中改为从 Langfuse API 拉取
            trace.score(name="efficiency", value=0.5)  # placeholder

            results.append({"recipe_id": recipe_id, "success": True, **result})

        except Exception as exc:
            print(f"  ✗ Case 失败: {exc}")
            trace.score(name="category_accuracy", value=0.0)
            trace.score(name="affected_file_accuracy", value=0.0)
            results.append({"recipe_id": recipe_id, "success": False, "error": str(exc)})

        # 确保恢复 main 分支（即使失败）
        try:
            git_checkout_main()
        except Exception:
            pass

    # ── 汇总 ──
    print(f"\n{'='*60}")
    success_count = sum(1 for r in results if r.get("success"))
    print(f"  完成: {success_count}/{len(results)} case 成功")
    print(f"  查看结果: Langfuse Dashboard → Traces")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
```

**运行**：

```bash
cd doctor && uv run python scripts/run_baseline_experiment.py
```

> **注意**：由于 Experiment 需要逐个注入/触发 bug，15 个 case 预计耗时 20-30 分钟。
> 后续 Phase 2 会引入 Langfuse `@experiment` 装饰器 + 并发控制来优化。

> **Doctor API 改造提示**：`POST /api/diagnose` 新增可选字段 `trigger_time`（ISO 8601 UTC）。
> Doctor 的 `search_observability` 工具收到此字段后，将 Loki/Tempo 查询窗口限定为
> `trigger_time ± 5min`，避免拉取全量历史数据。此改造在任务 0.0（Langfuse Tracing 集成）中一并完成。

**验收**：
- Langfuse Dashboard → Traces 中出现 15 条 trace（每条对应一个 case）
- 每条 trace 包含完整的 LLM 调用链 + tool 调用明细
- `search_observability` 的 Loki 查询日志中可见时间窗口被限定在 trigger_time 附近
- `category_accuracy` 和 `affected_file_accuracy` 已有初版分数
- 运行前后 git 分支均为 `main`（现场已清理）
- 记录基线 overall score（所有 case 的 category_accuracy 平均值）

---

### 任务 0.5：记录基线快照 + 清理旧 benchmark 代码（下午 1h）

```bash
# 在 Langfuse UI 中给 baseline_phase0 Experiment 打 tag: baseline
# 导出 Experiment 数据作为基线存档
mkdir -p output/baselines
```

**清理旧自研 benchmark 代码**：

| 处理 | 文件 |
|------|------|
| 删除 | `benchmark/runner.py` |
| 删除 | `benchmark/evaluators/` 全部 4 个文件 |
| 删除 | `benchmark/reporters/` 全部文件 |
| 删除 | `benchmark/schema.py` |
| 精简 | `benchmark/cli.py` → 保留为数据集导入脚本 |
| 保留 | `benchmark/loader.py` → 可复用于 Langfuse Dataset 导入 |

**Phase 0 验收清单**：
- [ ] 15 个 gold case ≥ 12 个跑通（bug-factory inject + trigger 成功，Doctor 实时查询返回报告）
- [ ] Langfuse 部署成功，Dashboard 可访问
- [ ] Doctor 诊断一次后 Langfuse Traces 出现完整调用链
- [ ] `diagdoctor-benchmark` Dataset 包含 15 个 item
- [ ] Doctor API 支持 `trigger_time` 参数，search_observability 查询窗口被正确限定
- [ ] 基线 Experiment `baseline_phase0` 可查看
- [ ] 记录基线 overall 分数
- [ ] 旧自研 benchmark 代码已清理
- [ ] bug-factory 不再收集证据（只用 inject + trigger 命令，不用 `full` 命令）

---

# Phase 1: P0 地基（D3-D12）

> **目标**：建立 Agent 推理质量的地基——手动循环 + 上下文工程 + Ingest 深度 + search 深度 + code_search 精确化。
> **基线增强**：Ingest auto-prefetch（采集）+ search_observability include_frontend（前端错误查询）已完成。
> **验收目标**：`overall` ≥ 基线 +15%（如基线 0.45 → 目标 0.52+），复杂 case 不再"后半程乱来"。

---

## D3-D4：手动 Agent 循环（方向 0，P0）

> **这是整个深度化计划的第一步——所有其他 harness 机制的前提。**

### D3：手动循环骨架 + 工具调用去重

#### 任务 1.1：重写 `unified_agent_node` 为手动循环

**AI 提示词**：

> 重写 `doctor/src/graph/nodes/unified_agent.py` 的 `unified_agent_node` 函数，从 `agent.ainvoke()` 改为手动驱动 Agent 循环。
>
> 核心结构：
> ```python
> async def unified_agent_node(state: DoctorState) -> dict[str, Any]:
>     evidence = state.evidence
>     evidence_text = format_evidence_for_agent(evidence)
>
>     base_prompt = _build_system_prompt()
>     messages: list[BaseMessage] = [
>         SystemMessage(content=base_prompt),
>         HumanMessage(content=evidence_text),
>     ]
>
>     llm = get_llm_for_role("diagnosis")
>     tools = get_all_tools()
>     tool_map = {t.name: t for t in tools}
>
>     # 工具调用去重缓存
>     call_history: list[tuple[str, str]] = []
>
>     for iteration in range(MAX_TOOL_CALLS):
>         response: AIMessage = await llm.ainvoke(messages, tools=tools)
>         messages.append(response)
>
>         if not response.tool_calls:
>             break
>
>         for tc in response.tool_calls:
>             tool_name = tc["name"]
>             tool_args = tc["args"]
>
>             # 工具调用去重
>             call_key = (tool_name, json.dumps(tool_args, sort_keys=True))
>             if call_key in call_history:
>                 messages.append(ToolMessage(
>                     content="[跳过：与之前调用完全相同]",
>                     tool_call_id=tc["id"],
>                     name=tool_name,
>                 ))
>                 continue
>             call_history.append(call_key)
>
>             # 执行工具（错误不中断循环）
>             try:
>                 result = await tool_map[tool_name].ainvoke(tool_args)
>             except Exception as exc:
>                 result = f"工具执行错误: {exc}"
>
>             messages.append(ToolMessage(
>                 content=str(result),
>                 tool_call_id=tc["id"],
>                 name=tool_name,
>             ))
>
>     # 解析输出（复用现有函数）
>     report = parse_diagnosis_report({"messages": messages})
>     findings = extract_findings({"messages": messages})
>     # ... 后续逻辑不变
> ```
>
> 关键约束：
> - 保留现有的 `format_evidence_for_agent`、`parse_diagnosis_report`、`extract_findings`、`update_budget`、`handle_agent_failure` 函数
> - `MAX_TOOL_CALLS` 保持 12
> - 工具执行错误不中断循环，返回错误信息给 Agent
> - 外层 LangGraph 图拓扑不变（ingest → unified_agent → reporter）
>   - **ingest** 已实现：auto-prefetch 并行采集 Loki/Tempo + 9 步标准化管线
>   - **unified_agent** 职责：纯 LLM 诊断（格式化证据 → ReAct 循环），不负责数据获取

**验收**：
- BE-020 case 跑通，Agent 调用工具正常
- 工具调用去重生效（相同参数的重复调用被跳过）
- 工具执行错误不中断循环
- `graph/subgraphs/unified_agent.py` 中的 `create_agent` 不再被调用

---

### D4：循环集成测试 + 预算追踪

#### 任务 1.2：预算追踪集成

**AI 提示词**：

> 在任务 1.1 的手动循环中增加预算追踪：
>
> 1. 在循环开始前初始化 `budget` 字典，记录 `tool_result_tokens` 和 `agent_reasoning_tokens`
> 2. 每次工具结果入 messages 后，更新 `budget.tool_result_tokens += len(result) // 4`
> 3. 每次 LLM 响应后，更新 `budget.agent_reasoning_tokens += len(str(response.content)) // 4`
> 4. 在循环中预留以下注入点（注释标记，后续方向填充）：
>    - `# TODO(方向4): maybe_compact_context(messages, budget)`
>    - `# TODO(方向4): truncate_tool_result(tool_name, result)`
>    - `# TODO(方向6): build_dynamic_system_prompt(base_prompt, budget)`
>    - `# TODO(方向12): registry.run_pre / run_post`
>    - `# TODO(方向10): recorder.record_tool_call(...)`
>
> 保留现有的 `update_budget()` 和 `handle_agent_failure()` 函数的调用。

**验收**：
- 预算追踪正确（`budget.total_used` 随迭代增长）
- 注入点注释清晰
- 复杂 case（如 CASCADE-020）不再因 context 爆炸而乱来

---

## D5：code_search ripgrep 升级（方向 3，P0）

### 任务 1.3：实现 ripgrep 精确搜索 + 结构化兜底建议

> ✅ **已实现**（2026-07-01 修订：移除向量兜底，改为结构化引导方案）

**设计决策**：RAG/向量搜索对代码标识符（函数名、类名、变量名）的语义匹配不可靠，
Agent 的正确路径应为 `search_observability → 获取线索 → ripgrep 精确搜`。
无匹配时不假装有救，直接引导 Agent 用 `get_file_content` 或重试其他工具。

**实现架构**：

```python
async def code_search(query: str, k: int = 10) -> str:
    k = min(max(1, k), 20)

    # 第一步：ripgrep 精确匹配
    rg_results = await _ripgrep_search(query, k=k)
    if rg_results:
        return json.dumps(rg_results, ensure_ascii=False)

    # 第二步：结构化兜底建议（非向量搜索）
    # 引导 Agent 走正确闭环：search_observability → 拿到标识符 → retry
    return _build_fallback_suggestion(query)
```

**实现要点**：
1. `_ripgrep_search()`：调用 `asyncio.create_subprocess_exec` 执行 `rg --json -n -C 3 -w "<query>" demo-app/`
   - 先尝试 whole-word 匹配（`-w`），无结果再回退到子串匹配
   - 限制文件类型：`--type py --type ts --type tsx --type js --type jsx`
   - 超时 10s
   - `rg` 不存在时静默返回空结果（不报错）
2. `_parse_ripgrep_output()`：解析 `rg --json` 输出，提取 match 和 context 行
3. `_build_fallback_suggestion()`：无匹配时返回结构化 JSON 建议：
   - 换更短关键词重试 `code_search`
   - 用 `get_file_content` 打开已知文件
   - 先调 `search_observability` 获取更精确线索
4. 结果格式统一：`{file_path, line_number, line_content, match_type, context_before, context_after}`
5. 搜索结果增强：标注 `file_role`（api_route / business_logic / data_model / frontend_page 等）
6. `CODEBASE_ROOT` 从 `settings.base_dir.parent / "demo-app"` 获取
7. **不再依赖 Qdrant / 向量检索** — RAG 保留用于错误模式库（任务 2.6），不用于代码搜索

**验收**：
- `code_search("list_tasks")` 返回 ripgrep 结果（精确匹配函数定义）
- `code_search("TaskResponse")` 返回 ripgrep 结果（类名精确匹配）
- `code_search("N+1 查询问题")` 返回 `match_type=fallback` 的结构化建议 JSON
- 搜索延迟 < 2s
- `rg` 不存在时返回 fallback 建议（不报错、不崩溃）

---

## D6-D7：上下文工程（方向 4，P0）

> **Agent 推理质量的地基。** 依赖方向 0（手动循环）提供注入点。

### D6：上下文预算追踪器 + 工具结果截断

#### 任务 1.4：创建 `context_engine.py` 核心模块

**AI 提示词**：

> 在 `doctor/src/graph/context_engine.py` 创建上下文引擎核心模块，包含：
>
> 1. `ContextBudget` 数据类：追踪 system_prompt / evidence / tool_result / agent_reasoning 的 token 使用
>    - `model_context_window: int = 128_000`
>    - `reserved_for_output: int = 4_000`
>    - `warning_threshold: float = 0.6`（60% 开始降级）
>    - `critical_threshold: float = 0.8`（80% 强制终结）
>    - 自动计算 `usage_ratio` 和 `phase`（INITIAL / INVESTIGATING / CONVERGING / FINALIZING）
>
> 2. `truncate_tool_result(tool_name, content)` 函数：工具结果入 context 前的预算控制
>    - 按工具类型设 token 上限：search_observability=1500, code_search=1000, get_file_content=2000, db_query=800, inspect_frontend_error=1000
>    - 超预算时保留关键行（含 error/exception/trace/span/fail/line 等关键词的行）
>    - 关键行不足时保留头尾（前 15 行 + 后 10 行），中间省略
>    - 追加 `[已压缩]` 标记
>
> 3. 单元测试：`doctor/tests/test_context_engine.py`

**验收**：
- `truncate_tool_result("search_observability", 10000_char_content)` 返回 < 6000 字符
- `truncate_tool_result("db_query", 500_char_content)` 原样返回
- `ContextBudget(usage_ratio=0.85).phase == ContextPhase.FINALIZING`
- 单元测试覆盖率 ≥ 90%

---

### D7：历史消息降级 + 动态 System Prompt + 自动压缩

#### 任务 1.5：历史消息降级

**AI 提示词**：

> 在 `context_engine.py` 中实现 `degrade_old_tool_results(messages, keep_recent=4)`：
>
> - 最近 4 条 ToolMessage：保留原文
> - 第 5-8 条：保留首行 + `[已摘要]` 标记
> - 第 9+ 条：替换为 `[已归档：工具 {name} 的结果已省略]`
>
> 单元测试：构造 12 条 ToolMessage 的 messages 列表，验证降级后 token 数减少 ≥ 40%。

**验收**：
- 12 条工具消息降级后，总 token 减少 ≥ 40%
- 最近 4 条原文不变
- 降级后的消息仍包含 tool_call_id（不破坏 LangChain 消息链）

---

#### 任务 1.6：动态 System Prompt 组装

**AI 提示词**：

> 在 `context_engine.py` 中实现 `build_dynamic_system_prompt(base_prompt, budget, diagnosis_hints)`：
>
> 1. 根据 `budget.phase` 注入不同策略文本：
>    - INITIAL：鼓励系统性探索
>    - INVESTIGATING：聚焦最可疑信号
>    - CONVERGING（60%+）：减少探索，最多再调 2-3 次工具
>    - FINALIZING（80%+）：禁止调工具，立即输出结论，confidence ≤ 0.6
>
> 2. 注入预算状态：已用 tokens / 剩余 tokens / 工具结果占用 / 当前阶段
>
> 3. 注入诊断进展提示（如有）：信号类型、活跃假设、已用工具

**验收**：
- 4 个阶段的策略文本都包含明确的行动指令
- FINALIZING 阶段的 prompt 包含"不要再调用任何工具"
- 动态 prompt 总长度不超过 base_prompt + 500 字符

---

#### 任务 1.7：自动压缩触发器 + 集成到 Agent 循环

**AI 提示词**：

> 1. 在 `context_engine.py` 中实现 `maybe_compact_context(messages, budget)`：
>    - 预算 > 60%：调用 `degrade_old_tool_results(keep_recent=3)`
>    - 预算 > 75%：额外将所有工具结果截断到 500 tokens 以内
>    - 返回 `(messages, compacted: bool)`
>
> 2. 修改 `unified_agent_node`（任务 1.1 的手动循环），将以下注入点填充：
>    - 每次迭代开始时：`messages, compacted = await maybe_compact_context(messages, budget)`
>    - FINALIZING 阶段：`messages.append(HumanMessage("⚠️ 预算即将耗尽，请立即输出诊断 JSON。"))`
>    - 工具结果入 messages 前：`result = truncate_tool_result(tc["name"], str(result))`
>    - System Prompt 动态更新：`messages[0] = SystemMessage(content=build_dynamic_system_prompt(base_prompt, budget))`

**验收**：
- 模拟 10 次工具调用后，`maybe_compact_context` 返回 `compacted=True`
- 压缩后 messages 总 token 减少 ≥ 30%
- 压缩不破坏最近 3 条工具结果
- BE-020 case 跑通，工具结果在日志中可见被截断（`[已压缩]` 标记）
- 平均 token 消耗下降 ≥ 20%

---

## D8-D9：Ingest 层深度化（方向 1，P0）

> **基线已实现**：Ingest 节点已完成 auto-prefetch（并行采集 Loki/Tempo 后端+前端数据）+
> 9 步标准化管线。本阶段在基线之上增加更精细的信号检测能力。

> ⚠️ **设计决策（2026-07-02 修订）**：
> 原计划为信号添加置信度评分（任务 1.8），但经架构评审后决定 **移除置信度评分**：
> - 置信度规则（+0.3 / ×0.8 等）是基于 15 个 gold case 调出来的启发式数值，
>   换一个系统可能完全不适用——本质是 overfitting。
> - **核心原则**：规则引擎做 LLM 不擅长的事（去噪、计数、关联），
>   LLM 做规则引擎不擅长的事（语义理解、推理、判断优先级）。
>   置信度评分恰恰跨过了这条线——它在替 LLM 做判断。
> - 保留：信号分类、Span 级 N+1 检测、去噪、去重、跨层关联。
> - 移除：置信度评分、烟雾弹关键词匹配、middleware 白名单。

### D8：Span 级 N+1 检测（✅ 已实现）

**AI 提示词**：

> 修改 `doctor/src/ingest/signal_extractor.py`，在 `extract_golden_signals()` 中新增 span 级 N+1 检测：
>
> 当前 `deduplicator.py` 只基于 log message 文本折叠 N+1。
> 新增逻辑：遍历 trace spans，检测同一 parent span 下有 ≥ 3 个相同 operation 的 child span。
>
> 判定条件：
> 1. 同一个 db_statement（归一化后）出现 ≥3 次
> 2. 这些 span 的 parent_span_id 相同（同一个父调用）
> 3. 总耗时 = 单次耗时 × 次数（线性增长特征）
>
> 输出 Signal 的 summary 包含 SQL 片段、重复次数、总耗时。

**验收**：
- PERF-020 case 的 N+1 信号被检测到（即使日志被折叠）
- 信号 summary 包含 parent span ID 和重复次数
- 单元测试覆盖

---

### D9：烟雾弹检测 + 证据上下文增强

> ⚠️ **设计决策**：烟雾弹关键词匹配已移除。原因：
> - "越权"/"不应该"/"idor" 等关键词匹配是静态规则，泛化能力差。
> - LLM 天然擅长从 user_report 做语义推断——"无信号却有用户报告"
>   本身就是 LLM 能识别的模式，不需要规则引擎僭越。
> - 证据上下文增强的"信号统计"功能随之简化（不再有高/低置信分类）。

#### 任务 1.11（修订）：证据上下文增强

**AI 提示词**：

> 修改 `format_evidence_for_agent()` 函数，在证据上下文部分增加：
>
> 1. **信号统计**：`共 N 个信号`
> 2. **缺失数据提示**：如果无 trace 数据，追加 `（无 Trace 数据，建议先调 search_observability 获取）`
> 3. **信号排序**：按 severity 降序排列（error → warning → info）

**验收**：
- 证据文本中信号按 severity 排序
- 缺失数据有提示

---

## D10-D11：search_observability 深度化（方向 2，P0）

> **基线已实现**：`search_observability` 已支持 `include_frontend=True` 参数——可主动查询
> 前端 `demo-frontend` 的 Loki 日志 + Tempo `client_error` span。本阶段在基线之上增加自动异常检测。

### D10：异常检测层

#### 任务 1.12：在 search_observability 中增加自动异常检测

**AI 提示词**：

> 修改 `doctor/src/tools/observability_unified.py`，在 auto 模式的分析阶段增加异常检测：
>
> 检测规则：
> 1. **错误突增**：某时间窗口内 ERROR 日志数 > 均值 + 2σ
> 2. **延迟突增**：某 span 的 duration > 同类 span 均值 × 3
> 3. **错误聚类**：相同错误消息在短时间内重复出现（burst）
> 4. **级联失败**：一个 trace 内多个 span 同时报错
> 5. **超时链**：span 链路上最后一个 span 耗时占比 > 80%
>
> 返回结构中新增 `analysis.anomalies` 字段，包含检测到的异常列表。

**验收**：
- BE-020 case 调用 search_observability 后返回 `anomalies` 字段
- 错误聚类将相同错误合并
- 级联失败检测正确

---

### D11：因果链重建 + 洞察摘要

#### 任务 1.13：因果链重建

**AI 提示词**：

> 在 `observability_unified.py` 中新增因果链重建：
>
> 利用已有的 `build_cross_tier_tree()`（`trace_query.py`）构建 span 树，然后提取因果链：
>
> 后序遍历 span 树，对 error 或 slow 节点提取因果链：
> ```
> frontend fetch /api/tasks (error)
>   → backend GET /api/tasks (error)
>     → DB SELECT * FROM tasks (slow, 1200ms)
>     → DB SELECT * FROM comments (×50, N+1)
> ```
>
> 输出为缩进文本列表。

**验收**：
- 因果链重建正确（前端 → 后端 → DB）
- 因果链包含服务层级、操作名、耗时、状态

---

#### 任务 1.14：洞察摘要生成

**AI 提示词**：

> 在 `observability_unified.py` 中新增 `_generate_insights(analysis_result)` 函数：
>
> 将原始分析数据转化为自然语言洞察，追加到返回结果末尾：
>
> ```python
> INSIGHT_TEMPLATE = """
> ## 自动洞察
>
> 1. **错误模式**：检测到 {N} 个相同错误 "{pattern}"，集中在 {time_range}
> 2. **性能瓶颈**：P95 延迟 {p95}ms，最慢端点 {slowest_endpoint}（{duration}ms）
> 3. **因果链**：{causal_chain}
> 4. **建议下一步**：
>    - 用 code_search 搜索 "{key_function}" 的实现
>    - 用 get_file_content 查看 {suspected_file}:{line_range}
>    - 用 db_query 验证 {suspected_table} 的数据状态
> """
> ```
>
> 洞察基于分析结果自动生成，不是调 LLM——是规则引擎。

**验收**：
- search_observability 返回结果末尾包含 `## 自动洞察` 部分
- 洞察包含具体的下一步建议（工具名 + 参数）
- 不依赖 LLM 调用（纯规则引擎）

---

### D12：Phase 1 集成测试 + 验收

#### 任务 1.15：端到端验证

```bash
# 跑 4 个代表性 case
for case in BE-020 FE-020 PERF-020 CASCADE-020; do
    python -m bug_factory.cli full "$case"
done

# 运行 Langfuse Experiment 对比 Phase 0 基线
uv run python scripts/run_experiment.py \
    --dataset diagdoctor-benchmark \
    --run-name "phase1_final" \
    --description "Phase 1 地基完成"

# 在 Langfuse Dashboard 中对比 baseline_phase0 vs phase1_final
```

**Phase 1 验收清单**：
- [ ] 手动 Agent 循环：工具调用去重 + 错误不中断 + 预算追踪
- [ ] 上下文引擎：工具结果截断 + 历史降级 + 动态 Prompt + 自动压缩
- [ ] Ingest 深度：信号分类 + Span 级 N+1（置信度评分已移除——LLM 自行判断优先级）
- [ ] search_observability 深度：异常检测 + 因果链 + 洞察摘要
- [ ] code_search 升级：ripgrep 精确搜索 + 结构化兜底建议（无向量兜底）
- [ ] `overall` ≥ 基线 +15%
- [ ] 平均 token 消耗下降 ≥ 20%
- [ ] 复杂 case（CASCADE-020）不再"后半程乱来"
- [ ] 所有新增功能有单元测试

---

# Phase 2: P1 质量与评测（D13-D25）

> **目标**：建立 Langfuse 驱动的评测体系（替代自研 benchmark）+ Prompt 策略化 + 诊断计划 + Bug 覆盖面扩展。
> **验收目标**：Langfuse 多维度 Scoring 可用 + `overall` ≥ 基线 +25% + 评测 case 数 ≥ 30。

---

## D13-D15：Langfuse 评测体系搭建（方向 5，P1）

> **背景**：Phase 0 已部署 Langfuse + 导入 Dataset + 跑通基线 Experiment。
> 本阶段用 Langfuse 的 Scoring / Experiment / Dataset 能力**替代全部自研 benchmark 代码**。

### D13：Langfuse 多维度 Scoring 配置

#### 任务 2.1：注册 7 维度 Scorer

**评分架构**（在 Langfuse UI 或 SDK 中配置）：

| 维度 | Langfuse Score 名称 | 评分方式 | 权重 |
|------|-------------------|---------|------|
| **root_cause_accuracy** | `root_cause_accuracy` | LLM-as-Judge（Langfuse 内置） | 0.30 |
| **affected_file_accuracy** | `affected_file_accuracy` | Python 自定义 Scorer（精确匹配） | 0.15 |
| **affected_line_accuracy** | `affected_line_accuracy` | Python 自定义 Scorer（范围匹配） | 0.10 |
| **fix_suggestion_quality** | `fix_suggestion_quality` | LLM-as-Judge | 0.20 |
| **category_accuracy** | `category_accuracy` | Python 自定义 Scorer（多标签 F1） | 0.10 |
| **evidence_chain_completeness** | `evidence_chain_completeness` | LLM-as-Judge | 0.10 |
| **confidence_calibration** | `confidence_calibration` | Python 自定义 Scorer | 0.05 |

> ⚠️ **MVP 阶段声明**：以上权重为人工设定（prioritize root_cause + fix_suggestion），
> 未经过统计校准。Phase 2 实现后应通过 Langfuse Experiment 的 A/B 对比
> 反推最优权重，或使用 Langfuse 内置的 score aggregation 自动加权。

**自定义 Scorer 示例**（`scripts/langfuse_scorers.py`）：

```python
from langfuse import Langfuse

langfuse = Langfuse()

def score_category_accuracy(trace_id: str, expected: dict, diagnosis: dict):
    """多标签分类 F1 Scorer。"""
    pred = set(diagnosis.get("categories", []))
    gold = set(expected.get("categories", []))
    tp = len(pred & gold)
    precision = tp / len(pred) if pred else 0.0
    recall = tp / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    langfuse.score(trace_id=trace_id, name="category_accuracy", value=f1)

def score_affected_file_accuracy(trace_id: str, expected: dict, diagnosis: dict):
    """文件定位精确匹配 Scorer。"""
    expected_file = expected.get("affected_file", "")
    actual_file = diagnosis.get("affected_file", "")
    hit = 1.0 if actual_file and actual_file.endswith(expected_file) else 0.0
    langfuse.score(trace_id=trace_id, name="affected_file_accuracy", value=hit)
```

> **LLM-as-Judge Scorer**：`root_cause_accuracy`、`fix_suggestion_quality`、`evidence_chain_completeness`
> 三个维度使用 Langfuse 内置 LLM Judge（配置 prompt template + 选择 judge 模型如 gpt-4o）。

**验收**：
- Langfuse UI → Scores 页面可见 7 个维度的 config
- 每个 Experiment run 完成后自动计算所有维度
- 每个维度可单独追踪退化趋势

---

### D14：过程质量评估 + CI 回归门禁

#### 任务 2.2：基于 Trace 的过程质量评估

> Langfuse 的 Trace 天然记录了完整的 Agent 调用过程（LLM 调用序列、tool 调用、每步耗时）。
> 无需自研 `process.py`，直接在 Langfuse 中配置过程指标：

```python
# scripts/langfuse_scorers.py 追加

def score_process_quality(trace_id: str, langfuse_client):
    """基于 Trace 数据评估过程质量。"""
    trace = langfuse_client.get_trace(trace_id)
    observations = trace.observations

    # 1. 工具选择合理性：是否第一步调用了 search_observability
    first_tool = next((o for o in observations if o.type == "GENERATION"), None)

    # 2. 工具调用效率：去重后调用次数 vs 总调用次数
    tool_names = [o.name for o in observations if o.type == "GENERATION"]
    unique_tools = len(set(tool_names))
    total_calls = len(tool_names)
    dedup_ratio = unique_tools / total_calls if total_calls else 1.0

    # 3. 预算使用率
    max_calls = 12
    budget_ratio = min(total_calls / max_calls, 1.0)

    # 4. 综合过程得分
    score = (dedup_ratio * 0.5 + (1 - budget_ratio) * 0.5)
    langfuse_client.score(trace_id=trace_id, name="process_quality", value=score)
```

**验收**：
- 过程质量评分出现在每个 case 的 Score 列表中
- 可识别"结果对但过程差"和"结果错但过程好"的 case

---

#### 任务 2.3：CI 回归门禁 + 盲集隔离

**CI 回归门禁**（`.github/workflows/eval-gate.yml`）：

```yaml
# PR 时自动跑 Langfuse Experiment，overall 下降 > 5% 则阻断
name: Eval Gate
on: [pull_request]
jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run Langfuse Experiment
        run: |
          cd doctor && uv run python scripts/run_experiment.py \
            --dataset diagdoctor-benchmark \
            --run-name "pr-${GITHUB_SHA::7}" \
            --baseline baseline_phase1
      - name: Check Score Regression
        run: |
          uv run python scripts/check_regression.py \
            --current "pr-${GITHUB_SHA::7}" \
            --baseline baseline_phase1 \
            --threshold 0.05
```

**盲集隔离**（利用 Langfuse Dataset metadata）：

```python
# 导入时用 metadata 区分 train/blind
langfuse.create_dataset_item(
    dataset_name="diagdoctor-benchmark",
    metadata={"split": "train", ...},  # 或 "blind"
)
```

在 Experiment 配置中选择 `metadata.split` 过滤，盲集结果单独统计。

**验收**：
- CI 中评测门禁可触发
- 盲集 Experiment 分数与 train 集分开报告
- overall 下降 > 5% 时 CI 阻断

---

### D15：Experiment 自动化 + 趋势 Dashboard

#### 任务 2.4：Experiment 执行脚本标准化

```bash
# scripts/run_experiment.py — 标准化 Experiment 执行入口
uv run python scripts/run_experiment.py \
    --dataset diagdoctor-benchmark \
    --run-name "phase2_d15" \
    --description "Phase 2 评测体系搭建完成" \
    --doctor-url http://localhost:8001 \
    --concurrency 4
```

**验收**：
- 一键运行全量 Experiment
- Experiment 完成后 Langfuse Dashboard 自动展示分数
- 可与任意历史 Experiment 对比

---

## D16-D18：System Prompt 策略化（方向 6，P1）

### D16：动态策略选择

#### 任务 2.4：信号驱动的策略选择

**AI 提示词**：

> 修改 `graph/nodes/unified_agent.py`，在 Agent 循环开始前根据证据信号类型选择策略：
>
> ```python
> def _select_strategy(evidence: NormalizedEvidence) -> str:
>     signals = evidence.golden_signals
>     has_error_span = any(s.signal_type == "error_span" for s in signals)
>     has_slow_span = any(s.signal_type == "slow_span" for s in signals)
>     has_repeated_query = any(s.signal_type == "repeated_query" for s in signals)
>     has_browser_error = any(s.source == "browser_error" for s in signals)
>     has_smokeless = any(s.signal_type.startswith("smokeless") for s in signals)
>     has_cross_layer = bool(evidence.correlations)
>
>     if has_cross_layer and has_browser_error:
>         return "cross_layer_crash"
>     elif has_repeated_query or has_slow_span:
>         return "performance"
>     elif has_error_span:
>         return "backend_error"
>     elif has_browser_error:
>         return "frontend_crash"
>     elif has_smokeless:
>         return "smokeless"
>     else:
>         return "default"
> ```
>
> 每个策略对应一个 Prompt 片段（在 `prompts/templates/strategies/` 目录下）：
> - `cross_layer_crash.j2`：跨层崩溃诊断策略（前端→后端→DB 逐层追踪）
> - `performance.j2`：性能问题诊断策略（trace 分析 → N+1 检测 → ORM 代码检查）
> - `backend_error.j2`：后端错误诊断策略（错误日志 → 代码定位 → 数据验证）
> - `frontend_crash.j2`：前端崩溃诊断策略（浏览器错误 → source map → 组件代码）
> - `smokeless.j2`：无信号诊断策略（user_report 分析 → 权限/排序/配置检查）
> - `default.j2`：默认策略
>
> 策略片段注入到 System Prompt 的"诊断策略"部分。

**验收**：
- 不同 case 类型使用不同策略
- 策略选择日志可见（`strategy=cross_layer_crash`）
- 跨层 case 使用 `cross_layer_crash` 策略

---

### D17：Few-shot 注入

#### 任务 2.5：Few-shot 示例注入

**AI 提示词**：

> 在 `doctor/src/prompts/templates/few_shot/` 目录下创建 few-shot 示例：
>
> 每个策略对应 1-2 个示例（从 gold case 的成功诊断中提取）：
> - `cross_layer_crash_example.j2`：FE-020 的成功诊断过程
> - `performance_example.j2`：PERF-020 的成功诊断过程
> - `backend_error_example.j2`：BE-020 的成功诊断过程
> - `smokeless_example.j2`：LOGIC-020 的成功诊断过程
>
> 示例格式：
> ```
> ## 诊断示例
>
> **输入证据**：[简要描述]
> **诊断过程**：
> 1. search_observability → [关键发现]
> 2. code_search → [关键发现]
> 3. get_file_content → [关键发现]
> **输出**：[简要诊断结果]
> ```
>
> 在 `build_dynamic_system_prompt()` 中，根据当前策略注入对应示例。

**验收**：
- System Prompt 中包含与策略匹配的 few-shot 示例
- 示例不超过 500 字符（不占太多 context）

---

### D18：错误模式库集成

#### 任务 2.6：错误模式库集成

**AI 提示词**：

> 修改 `doctor/src/prompts/registry.py`，新增 `build_error_pattern_reference()` 函数：
>
> 从 `struct_kb.py` 的 error_patterns 表提取模式，生成 System Prompt 参考：
> ```
> ## 常见错误模式参考
> - **FK 完整性错误**：正则 `foreign key constraint` → 检查关联数据是否存在
> - **scalar_one 500**：正则 `No row was found` → 检查查询条件是否过严
> - **N+1 查询**：正则 `repeated SELECT` → 恢复预加载
> ```
>
> 注入到 System Prompt 末尾（作为参考知识，不是策略指令）。

**验收**：
- System Prompt 包含错误模式参考
- 模式参考不超过 300 字符

---

## D19：诊断计划 TodoWrite（方向 7，P1）

### 任务 2.7：TodoWrite 指令注入

**AI 提示词**：

> 在 `unified_agent.j2` 的诊断策略部分，第 1 步之前新增"诊断计划"指令：
>
> ```jinja2
> ## 诊断计划（必须在第一次工具调用前输出）
>
> 在开始任何工具调用之前，你必须先输出一个诊断计划：
>
> <diagnosis_plan>
> 1. [步骤描述] — 预期工具: [工具名]
> 2. [步骤描述] — 预期工具: [工具名]
> 3. [步骤描述] — 预期工具: [工具名]
> </diagnosis_plan>
>
> 每完成一个步骤后，在后续推理中更新状态：
> - ✅ 已完成
> - 🔄 进行中
> - ⬜ 待执行
>
> 如果执行中发现计划需要调整，显式说明原因并更新计划。
> ```
>
> 同时修改 `parse_diagnosis_report()` 和 `extract_findings()`，从 Agent 输出中提取 `<diagnosis_plan>` 标签内容，存入 `findings` 中用于过程质量评估。

**验收**：
- Agent 在第一次工具调用前输出 `<diagnosis_plan>`
- 计划包含 2-5 个步骤
- 过程质量评估（任务 2.2）能读取计划并对比实际执行

---

## D20-D25：Bug Factory 扩展（方向 8，P1）

### D20-D21：新增 Bug 类型

#### 任务 2.8：新增 10 个 Bug 配方

**AI 提示词**：

> 在 `bug-factory/recipes/gold/` 新增以下 Bug 配方（目标：从 15 → 25 个）：
>
> | ID | 类别 | 描述 | 难度 |
> |----|------|------|------|
> | BE-023 | backend_error | 500 错误：序列化 datetime 失败 | L2 |
> | BE-024 | backend_error | 500 错误：除零异常 | L1 |
> | FE-022 | frontend_crash | 无限渲染循环（useEffect 缺依赖） | L3 |
> | FE-023 | frontend_crash | 状态更新后组件卸载（内存泄漏） | L3 |
> | PERF-022 | performance | 缺少数据库索引导致全表扫描 | L2 |
> | PERF-023 | performance | 同步阻塞操作在 async 函数中 | L2 |
> | LOGIC-023 | logic | 评论权限检查遗漏（可编辑他人评论） | L3 |
> | DATA-022 | data | 时区处理错误导致日期偏移 | L2 |
> | CONFIG-021 | config | CORS 配置错误导致前端请求被拒 | L2 |
> | RACE-021 | race | 并发创建重复项目（缺唯一约束） | L4 |
>
> 每个配方包含完整的：inject、trigger、evidence_collection、expected_diagnosis。

**验收**：
- 10 个新配方都能通过 `python -m bug_factory.cli full <case_id>` 跑通
- 证据收集完整（日志 + Trace + browser_errors）
- expected_diagnosis 字段完整

---

### D22-D23：Bug 变异引擎

#### 任务 2.9：Bug 变异引擎

**AI 提示词**：

> 在 `bug-factory/src/bug_factory/mutator.py` 创建 Bug 变异引擎：
>
> ```python
> class BugMutator:
>     """从一个基础 Bug 配方生成变体。"""
>
>     MUTATION_STRATEGIES = {
>         "rename_function": "修改被注入 bug 的函数名",
>         "shift_line": "将 bug 注入位置上下移动 5-10 行",
>         "change_field": "修改涉及的字段名（如 tags → labels）",
>         "change_error_type": "修改错误类型（如 TypeError → AttributeError）",
>     }
>
>     def mutate(self, recipe: dict, strategy: str, count: int = 3) -> list[dict]:
>         """生成变体配方。"""
>         ...
> ```
>
> 从 BE-020 生成 3 个变体，从 FE-020 生成 3 个变体，共 6 个新 case。

**验收**：
- 变异引擎生成 6 个新 case
- 变体 case 的 expected_diagnosis 与原 case 不同（函数名/行号/字段不同）
- 变体 case 能跑通

---

### D24-D25：对抗性 Bug + Phase 2 验收

#### 任务 2.10：对抗性 Bug 设计

**AI 提示词**：

> 在 `bug-factory/recipes/gold/` 新增 5 个对抗性 Bug：
>
> | 策略 | Bug 描述 | Agent 容易犯的错误 |
> |------|---------|-------------------|
> | 烟雾弹 | 日志有 ERROR 但根因是数据问题 | 停留在 ERROR 日志 |
> | 误导性栈帧 | 前端报错指向组件 A，根因在组件 B | 只看组件 A |
> | 多根因竞争 | 同时有 N+1 和越权，用户只报告慢 | 只诊断 N+1 |
> | 假阳性信号 | health check 偶尔超时 | 浪费预算调查 health check |
> | 时间差陷阱 | Bug 只在缓存过期后出现 | 查询时间窗口不对 |

**验收**：
- 5 个对抗性 case 能跑通
- 评测报告中对抗性 case 的分数单独统计

---

### Phase 2 验收

```bash
# 运行 Langfuse Experiment（现在 ≥ 30 case）
uv run python scripts/run_experiment.py \
    --dataset diagdoctor-benchmark \
    --run-name "phase2_final"

# 在 Langfuse Dashboard 中对比 Phase 1 基线
# Dashboard → Experiments → 选择 phase2_final vs baseline_phase1
```

**Phase 2 验收清单**：
- [ ] Langfuse 7 维度 Scoring 配置完成，每个可单独追踪
- [ ] 过程质量 Scorer 可用
- [ ] CI 回归门禁（Langfuse Experiment）工作
- [ ] 盲集隔离（train 10 + blind 5+）
- [ ] 动态策略选择（5+ 策略）
- [ ] Few-shot 示例注入
- [ ] 错误模式库集成
- [ ] TodoWrite 诊断计划
- [ ] Bug 配方 ≥ 30 个（15 原有 + 10 新增 + 6 变异 + 5 对抗性）
- [ ] `overall` ≥ 基线 +25%
- [ ] Langfuse Dashboard 中多维度分数可视化

---

# Phase 3: P2 鲁棒性（D26-D36）

> **目标**：安全纵深 + Agent 自省 + Langfuse 成本追踪 + Hook 系统 + Subagent 上下文隔离。
> **验收目标**：无幻觉 case + 安全零事故 + LLM 调用链全量可观测 + 模型降级可用 + 复杂 case context 隔离。

---

## D26-D27：安全纵深（方向 9，P2）

### D26：SQL 复杂度限制 + 文件白名单

#### 任务 3.1：SQL 复杂度限制

**AI 提示词**：

> 在 `security/sql_guard.py` 新增 `SQLComplexityChecker`：
> - 最大结果行数 1000（无 LIMIT 时自动添加）
> - 最大 JOIN 数 3
> - 禁止子查询
> - 查询超时 5s

**验收**：
- `SELECT * FROM huge_table`（无 LIMIT）被自动添加 LIMIT 1000
- 超过 3 个 JOIN 的查询被拒绝

---

#### 任务 3.2：文件访问白名单

**AI 提示词**：

> 在 `security/sanitizer.py` 新增文件类型白名单：
> - 允许：`.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.json`, `.yaml`, `.yml`, `.sql`, `.md`
> - 禁止：`.env`, `.git/*`, `*secret*`, `*credential*`, `*.pem`, `*.key`, `id_rsa`

**验收**：
- `get_file_content(".env")` 被拒绝
- `get_file_content("app/services/task_service.py")` 正常返回

---

### D27：工具输出脱敏

#### 任务 3.3：输出脱敏

**AI 提示词**：

> 在 `doctor/src/security/output_sanitizer.py` 创建输出脱敏模块：
>
> ```python
> class OutputSanitizer:
>     PATTERNS = [
>         (r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*", "[REDACTED_JWT]"),
>         (r"sk-[A-Za-z0-9]{20,}", "[REDACTED_API_KEY]"),
>         (r"postgresql://[^:]+:([^@]+)@", r"postgresql://***:\1@"),
>     ]
> ```
>
> 集成到工具执行后、结果入 messages 前。

**验收**：
- 工具返回的 JWT token 被替换为 `[REDACTED_JWT]`
- 数据库连接字符串中的密码被脱敏

---

## D28-D29：Agent 自省与纠错（方向 10，P2）

### D28：假设追踪与证伪

#### 任务 3.4：假设追踪指令注入

**AI 提示词**：

> 在 `unified_agent.j2` 追加假设追踪指令：
>
> ```jinja2
> ## 假设追踪
>
> 在诊断过程中，你必须：
>
> 1. **显式记录假设**：每次基于工具结果形成假设时，用以下格式记录：
>    - 假设 H1: [假设内容] | 证据: [支持证据] | 置信度: [0-1]
>
> 2. **主动证伪**：对每个假设，至少尝试一个"证伪查询"：
>    - 如果假设是"N+1 查询"，用 db_query 查看实际查询日志确认
>    - 如果假设是"字段缺失"，用 search_observability 查看 API 实际响应
>
> 3. **假设淘汰**：如果证伪查询的结果与假设矛盾，必须放弃该假设。
>    不要忽略矛盾证据。
>
> 4. **最终选择**：从存活的假设中选择置信度最高的作为根因。
> ```

**验收**：
- Agent 输出中包含假设追踪记录
- 证伪查询可见

---

### D29：证据覆盖度检查 + 自我审查

#### 任务 3.5：证据覆盖度检查

**AI 提示词**：

> 在 `unified_agent.py` 新增 `_check_evidence_coverage()` 函数：
>
> 检查诊断报告是否基于：
> - 可观测性数据（search_observability）
> - 代码检查（code_search / get_file_content）
> - 数据验证（db_query）
>
> 三者都有 → confidence 可信
> 缺少数据验证 → confidence 降 0.1
> 缺少代码检查 → confidence 降 0.15
>
> 在 Agent 输出报告前，自动检查并调整 confidence。

**验收**：
- 证据覆盖度检查自动调整 confidence
- 缺少代码检查的 case confidence 被降低

---

#### 任务 3.6：自我审查提示

**AI 提示词**：

> 在 `unified_agent.j2` 追加输出前自检指令：
>
> ```jinja2
> ## 输出前自检
>
> 在输出最终诊断报告前，请回答以下问题：
>
> 1. **根因是否经过验证？** 你是否用工具确认了根因，还是仅基于推理？
> 2. **affected_file 是否准确？** 你是否用 get_file_content 查看了该文件？
> 3. **fix_suggestion 是否可执行？** 你是否确认了修改位置和修改内容？
> 4. **是否有遗漏？** 是否有未解释的信号或证据？
> 5. **置信度是否合理？** 0.9+ 需要工具验证 + 证据链完整。
>
> 如果以上任何一项不满足，降低 confidence 或继续调查。
> ```

**验收**：
- Agent 输出中包含自检回答
- confidence 与证据覆盖度一致

---

## D30：成本优化与模型路由（方向 11，P2）

### D30：Langfuse 成本追踪 + 模型降级

> **说明**：Langfuse 已通过 callback 自动追踪每次 LLM 调用的 token 用量和成本。
> 不需要自研 `CostTracker`。D30 聚焦于**利用 Langfuse 数据进行成本分析**和**模型降级链**。

#### 任务 3.7：Langfuse 成本 Dashboard 配置

**操作步骤**：

```python
# Langfuse Dashboard 自动展示：
# 1. Total Cost / per-trace cost / per-model cost
# 2. Token 用量趋势（input / output / total）
# 3. 按 Experiment 聚合的成本报告
# 无需额外代码——callback 已自动上报。
```

**自定义成本分析脚本**（可选，用于 CI 门禁）：

```python
# scripts/cost_report.py — 从 Langfuse API 拉取成本数据
from langfuse import Langfuse

langfuse = Langfuse()
traces = langfuse.fetch_traces(
    tags=["phase3"],
    from_timestamp="2026-07-01T00:00:00Z",
)

total_cost = sum(t.cost for t in traces)
per_model = {}
for t in traces:
    model = t.model or "unknown"
    per_model[model] = per_model.get(model, 0) + (t.cost or 0)

print(f"Total cost: ${total_cost:.4f}")
print(f"Per model: {per_model}")
```

**验收**：
- Langfuse Dashboard 可查看每次诊断的 cost 明细
- 按模型、按 Experiment 聚合的成本可见
- CI 中可检查单次诊断成本是否超预算

---

#### 任务 3.8：模型降级链

**AI 提示词**：

> 在 `doctor/src/llm_factory.py` 新增 `FallbackModelChain`：
>
> ```python
> class FallbackModelChain:
>     CHAIN = ["o3-mini", "gpt-4o", "gpt-4o-mini"]
>
>     async def invoke_with_fallback(self, messages, primary_model):
>         chain = [primary_model] + [m for m in self.CHAIN if m != primary_model]
>         for model in chain:
>             try:
>                 return await self._invoke(model, messages)
>             except (TimeoutError, RateLimitError, ServiceUnavailableError):
>                 continue
>         raise RuntimeError("All models failed")
> ```
>
> 集成到 Agent 循环中，替换直接 `llm.ainvoke()` 调用。
> 降级事件通过 Langfuse trace metadata 自动记录（`model` 字段反映实际使用的模型）。

**验收**：
- 主模型超时时自动切换到备用模型
- Langfuse Trace 中可看到实际使用的模型（验证降级是否发生）
- 降级后诊断继续运行

---

## D31-D32：Hook 系统（方向 12，P2）

### D31：Hook 注册表 + PreToolUse

#### 任务 3.9：Hook 系统核心

**AI 提示词**：

> 在 `doctor/src/graph/hooks.py` 创建 Hook 系统：
>
> 1. `HookRegistry` 类：注册和管理 PreToolUse / PostToolUse 钩子
> 2. `HookFn` 类型：`Callable[[dict], dict | None]`（返回 None 表示拒绝执行）
> 3. 注册示例钩子：
>    - PreToolUse `db_query`：自动注入 LIMIT（如果 SQL 没有 LIMIT）
>    - PreToolUse `code_search`：记录搜索关键词审计
>    - PostToolUse `search_observability`：自动追加异常检测摘要
>    - PostToolUse `get_file_content`：检测返回内容中的敏感信息
>
> 集成到手动 Agent 循环中：
> ```python
> # 工具执行前
> args = await registry.run_pre(tc["name"], tc["args"])
> # 执行工具
> result = await tool_map[tc["name"]].ainvoke(args)
> # 工具执行后
> result = await registry.run_post(tc["name"], result)
> ```

**验收**：
- `db_query` 无 LIMIT 时自动注入
- `search_observability` 结果末尾自动追加异常检测
- `get_file_content` 返回含密钥时自动脱敏
- 所有工具调用有审计日志

---

### D32：审计日志持久化

#### 任务 3.10：审计日志持久化

**AI 提示词**：

> 在 Hook 系统中新增审计日志持久化：
>
> 每次工具调用记录：
> ```json
> {
>   "timestamp": "...",
>   "case_id": "...",
>   "tool_name": "search_observability",
>   "args": {"source": "auto", "query": "..."},
>   "result_tokens": 1200,
>   "duration_ms": 350,
>   "pre_hooks": ["auto_limit"],
>   "post_hooks": ["anomaly_detection"],
>   "success": true
> }
> ```
>
> 审计日志写入 `output/<case_id>/audit_log.jsonl`，用于评测和调试。

**验收**：
- 每个 case 的 `audit_log.jsonl` 存在且格式正确
- 审计日志包含所有工具调用
- 可用于过程质量评估

---

## D33-D35：Subagent 上下文隔离（方向 13，P2）

### D33：Subagent 执行器

#### 任务 3.11：Subagent 核心实现

**AI 提示词**：

> 在 `doctor/src/graph/subagent.py` 创建 Subagent 上下文隔离模块：
>
> 1. `SubagentTask` 数据类：
>    - `task_id: str`
>    - `description: str`（如"调查后端 /api/tasks 的 N+1 查询"）
>    - `tools_allowed: list[str]`（如 `["search_observability", "code_search", "get_file_content"]`）
>    - `max_iterations: int`（默认 5）
>    - `system_prompt_addon: str`（子任务专属策略）
>
> 2. `SubagentResult` 数据类：
>    - `task_id: str`
>    - `findings: str`（结构化结论）
>    - `confidence: float`
>    - `tool_calls_made: int`
>    - `tokens_used: int`
>
> 3. `run_subagent(task, evidence_context, parent_llm)` 函数：
>    - 在隔离的 messages 列表中执行子任务
>    - 与主循环共享 LLM 实例和工具定义
>    - 使用独立的 messages 列表（不污染主循环 context）
>    - 工具结果入 messages 前调用 `truncate_tool_result` 截断
>    - 完成后调用 `_summarize_subagent_result()` 提取摘要
>    - 只返回结构化结论，不返回完整推理过程

**验收**：
- Subagent 在独立 context 中执行
- 结果以摘要形式回传
- 子任务的 messages 不出现在主循环中

---

### D34：delegate_subagent 工具

#### 任务 3.12：delegate_subagent 工具注册

**AI 提示词**：

> 在 `doctor/src/tools/__init__.py` 注册 `delegate_subagent` 工具：
>
> ```python
> @tool
> async def delegate_subagent(
>     description: str,
>     tools: list[str] = ["code_search", "get_file_content"],
>     max_iterations: int = 5,
>     strategy: str = "",
> ) -> str:
>     """将子任务委派给独立 Agent 执行。
>
>     适用场景：
>     - 跨层 Bug：前端和后端调查分开
>     - 多根因：每个根因独立调查
>     - 大文件分析：避免大文件内容占用主循环预算
>     - DB 数据探索：多轮 SQL 查询验证
>
>     Args:
>         description: 子任务描述
>         tools: 允许子任务使用的工具列表
>         max_iterations: 最大迭代次数
>         strategy: 子任务专属策略
>     """
> ```
>
> 在主循环中拦截这个工具调用，启动 Subagent：
> ```python
> if tool_name == "delegate_subagent":
>     task = SubagentTask(...)
>     sub_result = await run_subagent(task, evidence_text, llm)
>     result = f"[Subagent 结果] {sub_result.findings}\n置信度: {sub_result.confidence}"
> ```

**验收**：
- Agent 可自主决定何时委派子任务
- delegate_subagent 工具在 System Prompt 中有说明
- 子任务结果以摘要形式回传主循环

---

### D35：Subagent 集成测试

#### 任务 3.13：端到端验证

```bash
# 跑跨层 case 和多根因 case
for case in CASCADE-020 FE-020 BE-020; do
    python -m bug_factory.cli full "$case"
done
```

**验收**：
- CASCADE-020 case 的前端和后端调查可以分开执行
- 主循环的 context 不被子任务污染
- Subagent 结果正确回传

---

### D36：Phase 3 验收

**Phase 3 验收清单**：
- [ ] 安全纵深：SQL 复杂度限制 + 文件白名单 + 输出脱敏
- [ ] Agent 自省：假设追踪 + 证据覆盖度检查 + 自我审查
- [ ] Langfuse 成本追踪：Dashboard 中每个 case/trace 的成本可见
- [ ] 模型降级：主模型不可用时自动切换（Langfuse trace 可验证）
- [ ] Hook 系统：PreToolUse + PostToolUse + 审计日志
- [ ] Subagent：上下文隔离 + delegate 工具 + 摘要回传
- [ ] 无幻觉 case（Agent 不编造不存在的证据）
- [ ] 安全零事故（无敏感信息泄露）
- [ ] `overall` ≥ 基线 +30%
- [ ] Langfuse Dashboard 中 LLM 调用链全量可观测

---

# 度量体系总览

## 核心指标定义

> 所有指标在 Langfuse Dashboard 中可视化和追踪。以下指标通过 Langfuse Score + Trace 数据计算。

| 指标 | Langfuse Score 名称 | 计算方式 | 目标 |
|------|-------------------|---------|------|
| `overall` | `overall` (composite) | 7 维度加权平均 | ≥ 基线 +30%（Phase 3 末） |
| `root_cause_accuracy` | `root_cause_accuracy` | LLM-as-Judge 语义相似度 | ≥ 0.70 |
| `affected_file_accuracy` | `affected_file_accuracy` | Python Scorer 精确匹配 | ≥ 0.75 |
| `affected_line_accuracy` | `affected_line_accuracy` | Python Scorer 范围匹配 | ≥ 0.60 |
| `fix_suggestion_quality` | `fix_suggestion_quality` | LLM-as-Judge | ≥ 0.65 |
| `category_accuracy` | `category_accuracy` | Python Scorer 多标签 F1 | ≥ 0.80 |
| `evidence_chain_completeness` | `evidence_chain_completeness` | LLM-as-Judge | ≥ 0.60 |
| `confidence_calibration` | `confidence_calibration` | Python Scorer | ≥ 0.70 |
| `avg_tool_calls` | 从 Trace 自动提取 | Langfuse Trace observations 统计 | ≤ 6.0 |
| `avg_tokens` | 从 Trace 自动提取 | Langfuse Trace usage 字段 | ≤ 25000 |
| `avg_latency_s` | 从 Trace 自动提取 | Langfuse Trace latency | ≤ 60s |
| `avg_cost_usd` | 从 Trace 自动提取 | Langfuse Trace cost 字段 | ≤ $0.05 |

## 各 Phase 预期指标

| 指标 | Phase 0 (基线) | Phase 1 | Phase 2 | Phase 3 |
|------|---------------|---------|---------|---------|
| `overall` | ~0.45 | ≥ 0.52 | ≥ 0.56 | ≥ 0.58 |
| `avg_tool_calls` | ~7.2 | ≤ 6.5 | ≤ 6.0 | ≤ 5.5 |
| `avg_tokens` | ~35000 | ≤ 28000 | ≤ 25000 | ≤ 22000 |
| `avg_latency_s` | ~45 | ≤ 50 | ≤ 55 | ≤ 60 |
| case 数 | 15 | 15 | 30+ | 30+ |

> **注意**：延迟可能随功能增加而上升，但准确率和效率应持续改善。token 消耗应因上下文工程而显著下降。

---

## 评测 & 观测命令速查

```bash
# ==================== Langfuse 操作 ====================

# 导入 case 到 Langfuse Dataset
uv run python scripts/import_cases_to_langfuse.py

# 运行 Experiment
uv run python scripts/run_experiment.py \
    --dataset diagdoctor-benchmark \
    --run-name "<run_name>" \
    --description "<description>"

# 对比两个 Experiment（在 Langfuse Dashboard 中操作）
# Dashboard → Experiments → 选择 run_a vs run_b → Compare

# 查看某个 case 的完整 Trace
# Dashboard → Traces → 搜索 case_id

# ==================== Docker 部署 ====================

# 启动 Langfuse（自托管）
docker compose up -d langfuse-server langfuse-postgres

# ==================== 旧 benchmark CLI（已废弃，仅保留导入脚本） ====================

# 如需从旧格式迁移数据
python -m benchmark.cli migrate-to-langfuse
```

---

## 监测架构速查

| 监测目标 | 工具 | 覆盖范围 |
|---------|------|---------|
| **Doctor LLM 调用链** | Langfuse Tracing | LLM 调用 / token / cost / tool 调用 / RAG retrieval |
| **Doctor 服务级** | OTel (保留) | API 延迟 / 错误率 / Python 进程 / DB 查询 |
| **Demo App** | OTel + Loki + Tempo + Grafana (不变) | 系统指标 / 日志 / 分布式调用链 |

```
Doctor                              Demo App
┌─────────────────────┐            ┌──────────────────────────┐
│ Langfuse  │  OTel   │            │  OTel + Loki + Tempo     │
│ (LLM 级)  │ (服务级) │            │  + Grafana               │
│           │         │            │  (系统级，不变)            │
└─────────────────────┘            └──────────────────────────┘
```

---

## 附录 A：方向与任务映射

| 方向 | 优先级 | 对应任务 | Phase |
|------|--------|---------|-------|
| 0. 手动 Agent 循环 | P0 | 1.1-1.2 | Phase 1 |
| 1. Ingest 深度 | P0 | 1.9, 1.11（1.8 置信度已移除，1.10 烟雾弹已移除） | Phase 1 |
| 2. search_observability 深度 | P0 | 1.12-1.14 | Phase 1 |
| 3. code_search ripgrep | P0 | 1.3 | Phase 1 |
| 4. 上下文工程 | P0 | 1.4-1.7 | Phase 1 |
| 5. Langfuse 评测体系 | P1 | 0.0 (部署) → 0.3-0.5 (基线) → 2.1-2.4 (深化) | Phase 0-2 |
| 6. System Prompt 策略 | P1 | 2.4-2.6 | Phase 2 |
| 7. TodoWrite | P1 | 2.7 | Phase 2 |
| 8. Bug Factory 扩展 | P1 | 2.8-2.10 | Phase 2 |
| 9. 安全沙箱 | P2 | 3.1-3.3 | Phase 3 |
| 10. Agent 自省 | P2 | 3.4-3.6 | Phase 3 |
| 11. Langfuse 成本追踪 | P2 | 3.7 (利用 Langfuse 内置) | Phase 3 |
| 12. Hook 系统 | P2 | 3.9-3.10 | Phase 3 |
| 13. Subagent | P2 | 3.11-3.13 | Phase 3 |

---

## 附录 B：与 learn-claude-code Harness 的对应

| learn-claude-code 机制 | DiagDoctor 对应 | 实现状态 |
|----------------------|----------------|---------|
| s01 Agent Loop | unified_agent 手动循环 | ✅ Phase 1 |
| s02 Tool Use | 5 工具 dispatch map | ✅ 已实现 |
| s03 Permission | sql_guard + sanitizer + secrets + output_sanitizer | ✅ Phase 3 深化 |
| s04 Hooks | HookRegistry | Phase 3 |
| s05 TodoWrite | 诊断计划注入 | Phase 2 |
| s06 Subagent | SubagentTask + run_subagent | Phase 3 |
| s07 Skill Loading | 动态策略 + few-shot | Phase 2 |
| s08 Context Compact | context_engine 四层压缩 | ✅ Phase 1 |
| s09 Memory | 暂不实施 | — |
| s10 System Prompt | 动态组装 + 策略 | ✅ Phase 1-2 |
| s11 Error Recovery | 模型降级 + 假设证伪 | Phase 3 |
| s12 Observability | **Langfuse** (LLM 级) + OTel (服务级) | ✅ Phase 0-3 |
| s13 Evaluation | **Langfuse** Scoring + Experiments | ✅ Phase 0-2 |
| s20 Comprehensive | 多机制围绕一个循环 | ✅ 全部 |

---

> **核心原则**：每个 Phase 完成后，系统必须仍可端到端运行。评测用 Langfuse Experiment 验证，分数可度量提升。不追求一步到位，追求每步可验证。

---

## 附录 C：自研 vs Langfuse 对照表

| 旧自研组件 | Langfuse 替代 | 删/留 | Phase |
|-----------|--------------|-------|-------|
| `benchmark/runner.py` | Langfuse Experiment SDK | **删** | 0 |
| `benchmark/evaluators/` (4 文件) | Langfuse Scoring (Python + LLM-as-Judge) | **删** | 0-2 |
| `benchmark/reporters/` (HTML/MD) | Langfuse Dashboard | **删** | 0 |
| `benchmark/schema.py` | Langfuse Trace/Observation 模型 | **删** | 0 |
| `benchmark/cli.py` | `scripts/run_experiment.py` + Langfuse UI | **精简** | 0 |
| `benchmark/loader.py` | `scripts/import_cases_to_langfuse.py` | **精简** | 0 |
| `doctor/src/observability/cost_tracker.py` | Langfuse 内置 cost 追踪 | **不建** | 3 |
| `scripts/eval_dashboard.py` | Langfuse Dashboard | **删** | 0 |
| `doctor/src/observability/` (OTel) | 保留不变 | **留** | — |
| `infra/` (Loki/Tempo/Grafana) | 保留不变 (Demo App 用) | **留** | — |

---

## 附录 D：可配置参数速查

> 所有参数通过 Pydantic Settings 加载，支持环境变量覆盖（`DOCTOR_BACKEND_SERVICE_NAME=my-svc`）。

| 环境变量 | 默认值 | 用途 |
|---------|--------|------|
| `DOCTOR_BACKEND_SERVICE_NAME` | `demo-backend` | 被诊断后端服务的 OTel service.name |
| `DOCTOR_FRONTEND_SERVICE_NAME` | `demo-frontend` | 被诊断前端服务的 OTel service.name |
| `DOCTOR_INGEST_SLOW_SPAN_THRESHOLD_MS` | `200.0` | Slow span 判定阈值（毫秒） |
| `DOCTOR_INGEST_N1_MIN_COUNT` | `3` | N+1 检测：最少重复次数 |
| `DOCTOR_INGEST_N1_LINEAR_TOLERANCE` | `0.3` | N+1 检测：线性增长容差 |
| `DOCTOR_INGEST_TIME_WINDOW_MINUTES` | `5` | Loki/Tempo 查询时间窗口 |
| `DOCTOR_AGENT_MAX_TOOL_CALLS` | `12` | Agent 最大工具调用次数 |
| `DOCTOR_AGENT_MODEL_CONTEXT_WINDOW` | `128000` | 模型 context window 大小 |
| `DOCTOR_AGENT_CONTEXT_WARNING_RATIO` | `0.6` | 上下文预算警告阈值 |
| `DOCTOR_AGENT_CONTEXT_CRITICAL_RATIO` | `0.8` | 上下文强制终止阈值 |
