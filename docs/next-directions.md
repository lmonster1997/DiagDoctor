# DiagDoctor 后续方向规划

> 基于 2026-07-12 全系统评估，按投入产出比 + 可量化性排序。
> 每个方向标注验证层级：**已验证** / **可实现可验证** / **可实现难验证** / **仅设计**

---

## P0：RAG 错误模式检索（可实现可验证）

**目标**：在 diagnosis 前检索相似错误模式，注入 system prompt，对比有/无 RAG 的诊断质量差异。

### 动机

- 这是四个待做方向中**最容易出量化数据**的一个
- 能产出一张有说服力的对比表，成为简历上最硬的增量
- 对 performance/logic/data/config 类"需要经验模式匹配"的 case 帮助预期最大

### 实验设计

```
同一批 case，跑两组：
  A 组（baseline）：当前 doctor，无 RAG
  B 组（RAG）   ：diagnosis 前先检索相似错误模式 → 注入 system prompt

对比指标：
  - overall 加权分（7 维度）
  - root_cause_accuracy（最重要）
  - 诊断轮次（RAG 能否减少不必要的工具调用？）
  - token 消耗（RAG 额外 token vs 节省的调查 token）
```

### 错误模式库设计

不用等 30 个 case，从**现有 15 个配方**中提取模式特征：

```yaml
pattern_id: "N+1_QUERY"
symptom_signature: "high latency + repeated identical SQL queries in trace"
observability_clues:
  - "Tempo span shows >500ms in database operations"
  - "same parameterized SQL appearing >3 times in a single request trace"
root_cause_template: "ORM lazy loading causing N+1 queries"
fix_template: "Use joinedload() / selectinload() to eager-load relations"
category: "performance"
related_recipes: ["PERF-020"]
```

```yaml
pattern_id: "NULL_REFERENCE"
symptom_signature: "TypeError / Cannot read property of null in frontend"
observability_clues:
  - "console.error with 'Cannot read properties of null'"
  - "client_error span in Tempo with component stack"
  - "race between data fetch and render"
root_cause_template: "Component rendering before async data resolved"
fix_template: "Add null-guard / optional chaining / loading state before render"
category: "frontend_crash"
related_recipes: ["FE-020"]
```

### 技术方案

```
错误模式 → embedding (Qdrant 已有) → 检索 top-k 相似模式
                                      ↓
                         注入到 diagnosis system prompt
```

- Embedding 模型：复用现有 Qdrant 部署
- 检索策略：按 symptom_signature 相似度 + category 加权
- 注入格式：`## 已知错误模式参考\n{matched_patterns}`
- 对比基线：`run_baseline_experiment.py` 加 `--rag` flag

### 预期产出

一张对比表 + 分析洞察：

| Case | Baseline overall | RAG overall | Δ | 分析 |
|------|-----------------|-------------|---|------|
| PERF-020 | ? | ? | ? | 性能类，预期 RAG 显著帮助 |
| DATA-020 | ? | ? | ? | 数据类，预期中等帮助 |
| FE-020 | ? | ? | ? | 前端 crash，预期帮助不大（证据已足够） |
| ... | | | | |

**面试金句**："RAG 错误模式检索在 15 个 case 上 overall 提升了 X%，尤其在需要经验模式匹配的 performance/logic 类 case 上最显著，而对证据已足够充分的 crash 类 case 帮助不大——这验证了 RAG 的价值边界。"

### 预计投入

3-5 天。

---

## P1：Evidence Ranking（上下文工程深化，可实现可验证）

**目标**：在注入 context 前对 evidence 按信息密度打分排序，提升关键信号的可见性。

### 动机

- 当前 evidence 是无差别 append 的，没有排序
- 某些 evidence（ERROR 日志、error span）信息密度远高于健康检查日志
- 这是上下文工程中最务实的改进方向

### 技术方案

```python
def rank_evidence(evidence_items: list[Evidence]) -> list[Evidence]:
    """按信息密度排序，高密度在前."""
    def score(item: Evidence) -> float:
        s = 0.0
        # 1. 日志级别加权
        if item.level == "ERROR":   s += 3.0
        elif item.level == "WARN":  s += 1.5
        # 2. Trace span 错误标记
        if item.span_status == "ERROR": s += 2.0
        # 3. 时间距离（越接近故障时刻权重越高）
        time_dist = abs(item.timestamp - trigger_time)
        s += max(0, 2.0 - time_dist / 60)  # 5 分钟内满分衰减
        # 4. 包含已知关键词（exception/traceback/timeout/crash）
        if any(kw in item.content.lower() for kw in KEYWORDS):
            s += 1.0
        return s
    return sorted(evidence_items, key=score, reverse=True)
```

### 实验设计

```
同一批 case，跑两组：
  A 组：当前无序 evidence 注入
  B 组：ranked evidence 注入（同样 token 预算下）

对比：
  - overall 加权分
  - token 效率（相同准确率下消耗更少 token）
  - 诊断轮次（Agent 是否更快定位关键信号？）
```

### 预期产出

- Token 节省率 + 准确率对比表
- 分析哪些 case 受益最大（日志噪音多的 case 预期受益更大）

### 预计投入

2-3 天。

---

## P2：安全守卫全量接线

**目标**：将 `sanitize_path`、`safe_subprocess_args`、`sanitize_for_llm` 从死代码变为接线状态。

### 当前状态

| 函数 | 实现 | 接线 |
|------|------|------|
| `assert_readonly(sql)` | ✅ 4 层防御 | ✅ `db_query.py:225` |
| `sanitize_path()` | ✅ | ❌ `code_search` / `get_file_content` 未调用 |
| `safe_subprocess_args()` | ✅ | ❌ `code_search` 的 ripgrep 子进程未调用 |
| `sanitize_for_llm()` | ✅ | ❌ 注入 LLM 前的 evidence 未调用 |
| `ensure_no_leaked_secrets()` | ✅ | ❌ 无调用方 |

### 接线清单

1. **`code_search` 工具**：调用前 `sanitize_path(pattern_dir)` + `safe_subprocess_args(ripgrep_args)`
2. **`get_file_content` 工具**：调用前 `sanitize_path(file_path, allowed_roots=[workspace_root])`
3. **`AgentLifecycleMiddleware`**：在注入 evidence 到 system prompt 前 `sanitize_for_llm(evidence_text)`

### 预期产出

- 补完安全体系的完整性
- 面试时可讲："5 重安全守卫，100% 接线"

### 预计投入

1-2 天。

---

## P3：历史诊断 RAG 架构设计（可实现难验证）

**目标**：设计并实现历史诊断检索机制，但在当前 benchmark 上不要求量化验证。

### 动机

- 真实场景中最可能产生 ROI 的方向——工程师修 bug 的过程本身就在标注高质量诊断
- 但在 15 个独立 case 的 benchmark 上**无法验证增量价值**（每个 case 都是全新的 bug 类型，没有"同类 bug 反复出现"的场景）
- 这是**场景不匹配**，不是方案无效

### 技术方案

```
每次诊断完成后：
  DiagnosisReport → embedding → Qdrant collection "diagnosis_history"

新诊断时：
  evidence → embedding → Qdrant 检索 top-3 相似历史诊断
  → 注入 system prompt："以下是历史上类似的 Bug 诊断记录，可供参考：..."
```

### 面试讲法

> "我设计了历史诊断检索架构并完成了代码实现，但在独立 case 的 benchmark 上无法体现增量价值。因为历史诊断的价值体现在'同类 bug 反复出现'的场景，而不是每个 bug 都是全新的。在有生产数据的真实场景中，这是最有可能产生 ROI 的方向，因为工程师修 bug 的过程本身就在标注高质量诊断。当前不做量化验证，这是边界判断。"

### 预计投入

1 天（架构设计文档 + 代码骨架），不强求跑通实验。

---

## P4：Bug Case 扩展至 30+

**目标**：增加 case 数量，使 benchmark 具备统计显著性。

### 当前瓶颈

- 15 个 case 无法区分 0.85 和 0.88 的 overall 差异（可能在噪声范围内）
- CI 门禁需要足够的 case 才能可靠检测回归

### 扩展方向

| 类别 | 当前数量 | 建议增加 | 新增类型 |
|------|---------|---------|---------|
| Backend Error | 3 | +3 | 中间件异常、第三方 API 超时、DB 连接池耗尽 |
| Frontend Crash | 2 | +2 | 状态管理 bug、路由守卫失效 |
| Performance | 2 | +2 | 前端 bundle 过大、后端无索引查询 |
| Logic | 3 | +2 | 权限校验遗漏、状态机跳转错误 |
| Data | 2 | +2 | 级联删除、事务隔离级别 |
| Config | 1 | +2 | 环境变量缺失、Feature flag 误配 |
| Race Condition | 1 | +1 | WebSocket 竞态 |
| Cascade | 1 | +1 | 缓存雪崩触发 DB 过载 |
| **新增：Intermittent** | 0 | +2 | 时间窗口触发的 Heisenbug |

### 预计投入

5-7 天（每个新配方 ~2h 设计 + 实现 + 验证）。

---

## P5：CI 评测门禁

**目标**：在 GitHub Actions 中接入 Langfuse Experiment，实现自动化回归检测。

### 技术方案

```yaml
# .github/workflows/eval-gate.yml
evaluate:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - name: Run smoke cases (fast, ~5 min)
      run: uv run scripts/run_baseline_experiment.py --split smoke --run-name "ci-${{ github.sha }}"
    - name: Check overall threshold
      run: |
        SCORE=$(uv run scripts/get_latest_overall.py --run-name "ci-${{ github.sha }}")
        if (( $(echo "$SCORE < 0.80" | bc -l) )); then
          echo "Overall score $SCORE below threshold 0.80"
          exit 1
        fi
```

三级门禁：
- **smoke**（4 case，~5 min）：PR 触发，检测灾难性回归
- **train**（8 case，~40 min）：合并到 main 前触发
- **full**（全部 case，~75 min）：发布前触发

### 预计投入

2-3 天。

---

## 方向总览

| 优先级 | 方向 | 验证层级 | 投入 | 简历价值 |
|--------|------|---------|------|---------|
| **P0** | RAG 错误模式检索 | 可实现可验证 | 3-5d | ⭐⭐⭐⭐⭐ 最硬的增量 |
| **P1** | Evidence Ranking | 可实现可验证 | 2-3d | ⭐⭐⭐⭐ 上下文工程加分 |
| **P2** | 安全守卫接线 | 可实现可验证 | 1-2d | ⭐⭐⭐ 补完完整性 |
| **P3** | 历史诊断 RAG | 可实现难验证 | 1d | ⭐⭐⭐ 展示边界判断 |
| **P4** | Bug Case 扩展 | 可实现可验证 | 5-7d | ⭐⭐⭐⭐ 统计可信度 |
| **P5** | CI 评测门禁 | 可实现可验证 | 2-3d | ⭐⭐⭐⭐ 工程化闭环 |

---

## 补充：哪些方向明确不做

| 方向 | 原因 |
|------|------|
| 四阶段动态策略注入 | 实际测试无收益，已放弃（正确决策） |
| 前端交互式补充信息 | 用户研究成本过高，不适合 side project |
| 多租户/生产部署 | 超出项目定位，面试时可讲设计思路即可 |
