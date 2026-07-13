# 分支重建计划：基于 `12e4f14` 恢复 Agent 质量 + 保留 FE 功能

> 创建日期：2026-07-11
> 目标：从 `12e4f14`（BE-020 overall=0.97）重建新分支，仅应用 FE 相关改进，剔除 CK-1 退化

---

## 一、当前状态评估

### 1.1 两个路径的当前表现

| 路径 | Trace ID | 工具调用 | overall | 核心问题 |
|------|----------|---------|--------|---------|
| REST API | b8323e15 | 22 | 0.76 | CK-1 prompt 退化，文件定位错误 |
| REST API (回退后) | eff1e1d2 | 18 | 0.72 | 仍差于基准，0 次 db_query |
| REST API **基准** | 4d7ac448 | 11 | **0.97** | ✅ 自然停，文件正确，db_query 命中 |
| CopilotKit | e5cbca2b | 14 | 失败 | "Agent 未输出有效 JSON" |

### 1.2 退化根因

```
CK-1 改动（3项）:
├── ① prompt 效率纪律 (diagnosis_agent.j2)     → 影响两路径，REST API 0.97→0.76
├── ② 证据富化 (bug_info.py, copilotkit_graph) → 仅 CopilotKit，但有 _attach_error_log_excerpts
└── ③ BudgetState 修复 (copilotkit_graph)       → 仅 CopilotKit
```

**核心发现**：CK-1 prompt 改动是共享的（`_build_system_prompt()` 两路径共用），副作用扩散到 REST API。

---

## 二、Git 提交图谱

```
12e4f14  feat: 使用langchain.create_agent框架+hooks        ← ✅ BE-020 0.97
    │
9033045  ci: 添加 dev-* 分支 push 触发                     ← 🔵 仅 CI
e0f9307  fix: CI                                           ← 🔵 CI + 大量脚本/测试文件
d2a51e5  fix(doctor): resolve mypy errors for CI pass       ← 🟢 安全（仅重命名+类型标注）
84452f9  feat: fe                                          ← 🟡 含整个 doctor/backend 重写
68a3bda  feat: fe实现流式展示agent调用链                     ← 🟡 前后端混合
787f414  feat: 修正文档乱码                                  ← 🔵 仅文档
ae667d9  feat: 实现证据链和Report展示                        ← 🟡 前后端混合
323421c  fix: 清理git残留                                   ← 🔵 清理
aae23c5  feat: 整合两条agent 分析路径 (HEAD, origin)         ← 🔴 含 CK-1 改动！
```

**工作区未提交改动**：
- `llm_factory.py` — `triage`→`buginfo` 重命名（不影响 REST API）
- `tools/__init__.py` — 移除 `INGEST_TOOL`
- `observability_unified.py` — 截断优化（error 日志优先）
- `tools_reference.md` — 移除 `run_ingest` 文档
- `diagnosis_agent.j2` — CK-1 prompt 改动（已回退又撤销）
- `main.py` — CopilotKit 集成
- 各种脚本/文档

---

## 三、重建策略

### 策略 A：Cherry-pick（风险高 ❌）

逐个 cherry-pick `9033045..323421c`（7 个 commit），然后选择性应用 `aae23c5` 中的安全部分。

**问题**：`84452f9` 和 `68a3bda` 是全量重写（数百文件），很难分离前后端改动。

### 策略 B：File-level 复制 + 前端目录整体迁移 ✅ 推荐

```
1. 新分支起点：12e4f14
2. 复制 doctor/frontend/ → 从 HEAD（保留完整前端功能）
3. 复制必要的后端支撑文件 → 从 HEAD 选择性复制
4. 应用安全补丁（mypy fix, CI）
5. 验证：跑 BE-020 确保 overall 回到 0.97
```

### 策略 C：从 HEAD reset 12e4f14 的 agent 文件 ✅ 备选

```
1. 在 HEAD 上 reset agent 核心文件到 12e4f14 版本
2. 保留前端文件不变
3. 清理 CK-1 残留
```

---

## 四、文件分类矩阵

### 🔴 必须从 12e4f14 恢复（Agent 核心，不能引入退化）

| 文件 | 原因 |
|------|------|
| `doctor/backend/src/prompts/templates/diagnosis_agent.j2` | CK-1 效率纪律破坏了 agent 行为 |

### 🟢 保持 HEAD 版本（安全，不改 agent 行为）

| 文件 | 原因 |
|------|------|
| `doctor/frontend/**` | 纯前端，不影响 agent |
| `doctor/backend/src/main.py` | CopilotKit 集成，仅添加路由 |
| `doctor/backend/src/graph/copilotkit_graph.py` | CopilotKit 专属图，仅影响 CopilotKit 路径 |
| `doctor/backend/src/graph/nodes/bug_info.py` | BugInfo 节点，仅 CopilotKit 路径 |
| `doctor/backend/src/graph/nodes/ingest.py` | 仅变量重命名（mypy fix），逻辑不变 |
| `doctor/backend/src/ingest/signal_extractor.py` | 仅类型标注（mypy fix），逻辑不变 |
| `doctor/backend/src/graph/nodes/diagnosis_agent/node.py` | 仅类型标注（mypy fix），逻辑不变 |
| `.github/workflows/ci.yml` | CI 配置 |
| `doctor/backend/src/tools/__init__.py` | ✅ 移除 INGEST_TOOL（决定见下方） |
| `doctor/backend/src/prompts/templates/tools_reference.md` | ✅ 对应移除 run_ingest 文档（决定见下方） |

### 🔧 INGEST_TOOL 移除决定

**决策**：移除 `INGEST_TOOL`（`run_ingest`）。

**理由**：
1. 4d7ac448（0.97 好版本）全程未调用 `run_ingest`——agent 从不需要它
2. 证据采集已在 graph 层完成（`ingest_node` / `bug_info_node`），agent 拿到的是标准化后的 `NormalizedEvidence`
3. 保留它不会提升诊断质量，但会增加 agent 的工具选择空间（可能误调用）
4. 减少工具数 → 减少 agent 决策负担 → 减少不必要的工具探索

**配套**：`tools_reference.md` 同步更新（移除 run_ingest 条目，工具数 6→5），但**保留搜索优先的引导语气**（不引入"你无需手动采集"的消极暗示）。

### 🟡 需要审查（可能含 agent 影响）

| 文件 | 审查要点 |
|------|---------|
| `doctor/backend/src/tools/observability_unified.py` | 截断阈值 8000→24000 + error 日志优先，需确认不引入退化 |
| `doctor/backend/src/llm_factory.py` | `triage`→`buginfo` 重命名是否影响其他调用 |
| `doctor/backend/src/api/diagnose.py` | Phase 2 evidence payload 添加 |

### ⚪ 不关心（脚本、文档、测试）

| 文件 | 说明 |
|------|------|
| `doctor/backend/scripts/**` | 辅助脚本，随 HEAD |
| `doctor/backend/tests/**` | 测试，随 HEAD |
| `docs/**` | 文档 |
| `doctor/frontend/**` | 前端 |

---

## 五、执行步骤（策略 B）

### Step 1：创建新分支
```bash
git checkout 12e4f14 -b dev-rebuild-v2
```

### Step 2：复制前端
```bash
# 从 dev-create-agent 复制 doctor/frontend/
git checkout dev-create-agent -- doctor/frontend/
```

### Step 3：复制必要的后端支撑
```bash
# CopilotKit 集成（main.py）
git checkout dev-create-agent -- doctor/backend/src/main.py

# CopilotKit 专属图 + BugInfo 节点
git checkout dev-create-agent -- doctor/backend/src/graph/copilotkit_graph.py
git checkout dev-create-agent -- doctor/backend/src/graph/nodes/bug_info.py

# 后端 API（Phase 2 evidence payload）
git checkout dev-create-agent -- doctor/backend/src/api/diagnose.py
git checkout dev-create-agent -- doctor/backend/src/graph/state.py
```

### Step 4：应用安全补丁 + INGEST_TOOL 移除
```bash
# mypy fix（安全：仅重命名 + 类型标注）
git checkout dev-create-agent -- doctor/backend/src/graph/nodes/ingest.py
git checkout dev-create-agent -- doctor/backend/src/ingest/signal_extractor.py
git checkout dev-create-agent -- doctor/backend/src/graph/nodes/diagnosis_agent/node.py

# INGEST_TOOL 移除（决定：✅ 移除）
git checkout dev-create-agent -- doctor/backend/src/tools/__init__.py

# tools_reference.md 同步更新（移除 run_ingest 文档，但不引入"无需手动采集"）
git checkout dev-create-agent -- doctor/backend/src/prompts/templates/tools_reference.md

# CI
git checkout dev-create-agent -- .github/workflows/ci.yml
```

### Step 5：保持 12e4f14 版本（不做改动）
```bash
# 这些文件保持 12e4f14 原样，不引入 HEAD 的改动：
# - observability_unified.py: 不做截断阈值改动（8000→24000 risk）
# - llm_factory.py: 保持 triage 角色名（不影响 REST API）
# - diagnosis_agent.j2: 保持旧 prompt（核心，已从 🔴 恢复）
```

### Step 6：前端适配（可能需要）
```bash
# 如果前端依赖新的 backend API 字段，检查 state.py / diagnose.py 兼容性
```

### Step 7：验证
```bash
# 重启 doctor-api
# 跑 BE-020 验证
uv run python scripts/eval_agent.py --cases BE-020
# 预期：overall ≥ 0.90，tool_calls ≤ 12，自然停止
```

---

## 六、风险点

| 风险 | 缓解 |
|------|------|
| 前端依赖新 backend API 字段 | Step 6 检查 Phase 2 payload 兼容性 |
| CopilotKit 功能断裂 | main.py 集成已完整，bug_info 节点已复制 |
| `tools_reference.md` 的"无需手动采集"语气可能削弱 agent 主动性 | HEAD 版本默认带有此措辞，需审查并在必要时手动修正措辞 |
| 新分支需独立测试 | 完整跑 BE-020 + CopilotKit smoke |

---

## 七、验证 checklist

- [ ] BE-020 REST API overall ≥ 0.90
- [ ] BE-020 tool_calls ≤ 12
- [ ] BE-020 early_stopped = false (自然停)
- [ ] BE-020 affected_file = `api/comments.py`
- [ ] CopilotKit 前端能正常发消息
- [ ] CopilotKit 诊断能产出 JSON（不再 "Agent 未输出有效 JSON"）
- [ ] `uv run pytest doctor/backend/tests/graph/ -q` 全绿
