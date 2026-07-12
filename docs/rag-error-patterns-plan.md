# RAG 错误模式库 — 后续实施计划

## 背景

N+1 检测等硬编码诊断规则已从 Ingest 归一化层移除。Ingest 现在只做纯数据整理（打标签、去噪、去重、合并时间线、信号提取、跨层关联）。错误模式识别应通过 RAG 方式在 DiagnosisAgent 诊断时按需检索。

## 架构

```
Ingest（纯数据整理，无诊断逻辑）
  mark_tiers → denoise → dedup → merge_timeline
      → extract_golden_signals → correlate_by_trace_id
                                         ↓
                                   NormalizedEvidence
                                         ↓
DiagnosisAgent
  ├─ search_observability(trace_id=...)     # 查 Loki/Tempo
  ├─ code_search(pattern=...)               # 搜代码
  └─ search_error_patterns("N+1", ...)      # 新工具：RAG 检索错误模式
       → Qdrant 向量检索
       → 返回匹配的模式卡片 + 诊断建议
```

## 错误模式卡片格式

```json
{
  "pattern_id": "n-plus-one-query",
  "title": "N+1 查询反模式",
  "category": "performance",
  "severity": "warning",
  "description": "一个父操作触发了 N 次相同的数据库查询，导致响应时间线性增长",
  "typical_signals": [
    "同一 parent_span 下 ≥3 个子 span 有相同 db_statement",
    "子 span 耗时呈现线性增长（total ≈ avg × count）"
  ],
  "code_smells": [
    "ORM relationship lazy loading（未设置 selectin/joined）",
    "循环体内调用 repository.findById()",
    "GraphQL resolver 中逐个查询关联字段"
  ],
  "fix_strategy": "使用 JOIN 或 eager loading 批量加载关联数据",
  "example_cases": ["BE-020", "PERF-020"],
  "related_patterns": ["slow-query", "missing-index"]
}
```

## 实施步骤

### Phase 1: 基础设施
- [ ] `doctor/backend/src/knowledge/` 目录结构
- [ ] 错误模式 schema（Pydantic model）
- [ ] Qdrant collection 创建 + 索引配置
- [ ] 模式导入脚本（从 YAML/JSON 批量建库）

### Phase 2: 工具实现
- [ ] 新增 `search_error_patterns` LangChain 工具
  - 输入：自然语言查询 + 可选信号类型过滤
  - 输出：Top-K 匹配模式卡片 + 相关性分数
- [ ] 集成到 `ALL_TOOLS` 工具集
- [ ] 更新 `diagnosis_agent.j2` 系统提示（让 Agent 知道有这个工具）

### Phase 3: 模式库建设
- [ ] 首批 10+ 错误模式（基于现有 15 个 bug 配方提炼）
  - N+1 查询、慢查询、缺失索引、外键违例
  - 空指针/AttributeError、JWT 过期、竞态条件
  - CORS 错误、前端 JS 异常、React 渲染错误
- [ ] 每个模式关联 1-3 个已知 case

### Phase 4: Agent 集成
- [ ] 在 ReAct 循环中，Agent 根据 golden_signals + correlations 自动检索
- [ ] 检索结果注入诊断证据（类似当前的 evidence 格式化）
- [ ] E2E 测试 + Langfuse 评测对比（RAG vs 无 RAG）
