# DiagDoctor 后续方向规划

> 基于 2026-07-12 全系统评估，按投入产出比 + 可量化性排序。
> 每个方向标注验证层级：**已验证** / **可实现可验证** / **可实现难验证** / **仅设计**

---

## P0：历史诊断 RAG（可实现；基础设施已就绪）

**目标**：将每次高质量诊断自动存入 Qdrant，新诊断时检索相似历史案例并注入 system prompt，形成"越用越准"的正反馈循环。

### 动机

- 真实场景中最可能产生 ROI 的方向——工程师修 bug 的过程本身就在标注高质量诊断
- **P0 错误模式检索被废弃的原因**：原始证据（logs/traces）与人类描述的错误模式之间存在语义鸿沟，embedding 直接检索不可行
- 而历史诊断 RAG 的查询端与文档端**都是自然语言**（user_report + evidence 摘要 ↔ 历史 DiagnosisReport），语义空间一致，检索可靠
- 基础设施已就绪：`index_diagnosis()`、`search_historical_cases()`、Qdrant collection `historical_cases` 均已实现

### 为什么这条线可行（与废弃 P0 的对比）

| 维度 | 废弃 P0（错误模式检索） | 新 P0（历史诊断 RAG） |
|------|------------------------|---------------------|
| 查询端 | 原始 logs/traces（结构化） | user_report + signal 摘要（自然语言） |
| 文档端 | 人工编写的模式描述 | 历史 DiagnosisReport（自然语言） |
| 语义空间 | 不一致 ❌ | 一致 ✅ |
| 基础设施 | 需从零搭建 | `index_diagnosis` + `search_historical_cases` 已有 |

### 技术方案

```
诊断完成 → 用户点赞（👍）
    │
    ▼
DiagnosisReport ──→ embed(user_report + root_cause + fix_summary)
    │                   │
    │                   ▼
    │             Qdrant "historical_cases"
    │
    ▼
新诊断时：
    user_report + signal 摘要 ──→ embed ──→ Qdrant 检索 top-3
                                                │
                                                ▼
                                    注入 system prompt：
                                    "## 历史相似诊断参考
                                     {case_1}...{case_2}...{case_3}"
```

### 实现步骤

1. **检索接入 diagnosis 流程**（核心改动，~0.5d）
   - 在 `subgraph.py:_build_system_prompt()` 之前调用 `search_historical_cases()`
   - 查询文本：`evidence.user_report` + `format_evidence_for_agent(evidence)` 的前 500 字符
   - 将 top-3 结果格式化注入 system prompt 末尾

2. **检索质量保障**（~0.5d）
   - 相似度阈值：cosine ≥ 0.75，低于阈值不注入（避免不相关历史干扰）
   - 去重：排除当前 case 自身（按 trace_id / case_id）
   - category 加权：同类别 case 提升 rank

3. **写入质量保障**（用户反馈驱动，~0.5d）
   - 诊断完成后，展示"👍 有帮助 / 👎 无帮助"反馈按钮
   - 仅用户点赞（👍）的诊断才触发 `index_diagnosis()` 写入 Qdrant
   - 增加去重：同一 trace_id 不重复写入
   - 优势：新 case 无 ground truth，自动评分无法判断诊断质量；只有遇到 bug 的用户自己能确认"这个诊断帮到我了"，以人类反馈为最终标准，"越用越准"由真实用户驱动

4. **Prompt 注入格式设计**（~0.5d）
   ```
   ## 历史相似诊断参考（来自知识库）

   以下是与当前问题相似的已解决 Bug，仅供参考其诊断思路：

   ### Case 1（相似度: 0.89）
   - 用户报告: "创建任务后页面卡死..."
   - 根因: ORM lazy loading 导致 N+1 查询
   - 修复: 使用 selectinload() 预加载关联数据

   ### Case 2（相似度: 0.82）
   ...

   ⚠️ 以上仅供参考，请基于当前实际证据独立判断，不要机械套用。
   ```

### 验证策略

**在 15 个独立 case 的 benchmark 上不做量化验证**。原因是场景不匹配：
- 历史诊断的价值体现在"同类 bug 反复出现"的场景
- 当前 benchmark 的 15 个 case 各不相干，无法体现增量
- 强行跑 A/B 对比会得到噪声级别的差异，没有统计意义

**替代验证方式**：
- 手动构造 2-3 个"同类变体"case（如 PERF-020 的 N+1 变体），验证检索命中率
- 人工检查检索结果的相关性（top-3 是否确实相似）
- 这是**边界判断**——架构正确但当前场景无法量化，面试时可讲清楚

### 面试讲法

> "我实现了历史诊断 RAG 检索，每次高质量诊断自动存入 Qdrant，新诊断时检索相似历史案例注入 system prompt。这形成'越用越准'的正反馈——工程师修 bug 的过程本身就在标注训练数据。在 benchmark 上不做量化验证，因为 15 个独立 case 无法体现历史检索的价值——它的真正价值在'同类 bug 反复出现'的生产场景中。这是边界判断能力。"

### 预计投入

2 天（基础设施已有，主要是接线 + prompt 工程）。

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

## P3：Bug Case 扩展至 30+

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

## P4：CI 评测门禁

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
| **P0** | 历史诊断 RAG | 可实现（基础设施已就绪） | 2d | ⭐⭐⭐⭐⭐ 越用越准的正反馈 |
| **P1** | Evidence Ranking | 可实现可验证 | 2-3d | ⭐⭐⭐⭐ 上下文工程加分 |
| **P2** | 安全守卫接线 | 可实现可验证 | 1-2d | ⭐⭐⭐ 补完完整性 |
| **P3** | Bug Case 扩展 | 可实现可验证 | 5-7d | ⭐⭐⭐⭐ 统计可信度 |
| **P4** | CI 评测门禁 | 可实现可验证 | 2-3d | ⭐⭐⭐⭐ 工程化闭环 |

---

## 补充：哪些方向明确不做

| 方向 | 原因 |
|------|------|
| 四阶段动态策略注入 | 实际测试无收益，已放弃（正确决策） |
| 前端交互式补充信息 | 用户研究成本过高，不适合 side project |
| 多租户/生产部署 | 超出项目定位，面试时可讲设计思路即可 |
