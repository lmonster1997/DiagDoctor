明白，P0 就钉死"点赞即入库"这一条路，auto 静默 / failed case / 复杂门控全推后序。出一版能直接贴 repo 的 md。

---

# P0 详细设计：历史诊断 RAG（Historical Diagnosis RAG）

> 基于 2026-07-14 复盘修订：砍掉 `llm_judge` 驱动的 auto 静默通道（真实诊断无金标，llm_judge 评的是"报告质量"不是"诊断正确性"，门控不可靠）。P0 只做 **👍 显式点赞 → 入库 → 新诊断检索注入** 这一条闭环，其余（auto 通道、failed case、门控深化）留 P1+。

---

## 1. 动机与定位

工程师"确认这个诊断有用"的动作本身就在标注高质量训练数据。通用模型没见过你司私有代码库，历史诊断 RAG 正好补足这个缺口：

- **查询端**：`user_report` + signal 摘要（自然语言）
- **文档端**：历史 `DiagnosisReport`（自然语言）
- **语义空间一致** → embedding 检索可靠（对比废弃的"错误模式检索"：查询端是 logs/traces 结构化、文档端是人工模式描述，语义空间不一致，检索不可靠）

业界同向参照：

- **Claude Code** 对话记忆：存"结论 + 关键片段"、新 session inject，不存原始交互
- **Cursor "similar fix"**：按"报错 + 文件路径"检索团队历史修法，inject 到 LLM
- **Braintrust/LangSmith Example Store**：把人工标注 good case 存 example，新 case 检索做 few-shot

DiagDoctor 这一版的差异化：**用"用户点赞"当标注器，标注对象是私有代码库的 bug 诊断报告**——通用模型没这块数据。

---

## 2. 整体流程

```
诊断完成 → Reporter 输出 → 前端弹"👍 有用 / 👎 无用"
    │
    👍 点击
    ▼
maybe_index_diagnosis(report, evidence)
    │
    ├── 硬拦截（报告不完整 / trace_id 已存在）→ 丢弃
    │
    └── 通过 → 组装 passage → bge-m3 embed → Qdrant upsert
                                            │
                                            ▼
                                     collection: historical_cases
                                             │
                                             ▼
                                    新诊断时 diagnosis_agent.py
                                     构造 system prompt 后
                                            │
                                            ▼
                                    search_historical_cases(evidence)
                                            │
                                            ▼
                                    top-3 格式化 → inject 到 system prompt 末尾
```

写入侧**异步**（`asyncio.create_task`），不阻塞诊断返回。用户点 👍 是前端交互，诊断已经完成，写入延迟无感。

---

## 3. 入库：什么能进、怎么存

### 3.1 触发条件（P0 极简版）

| 条件 | 说明 |
|---|---|
| 用户显式 👍 | 唯一触发，P0 不设 auto 静默通道 |
| 报告完整 | `root_cause` + `affected_file` + `fix_suggestion` 非空（防脏数据，非质量门控） |
| 去重 | `run_id`（thread_id）作 Qdrant point id，`upsert` 天然幂等（同一诊断重复 👍 覆盖不新增）；同 `trace_id` 跨 session 重复写入仅记日志告警，不前置拦截（P0 先用 trace_id 级，P1 可升级三元组） |

> 📌 为什么 P0 不搞 auto 静默通道：`llm_judge` 在 gold case 里是靠 `expected.root_cause` 当锚评的，真实场景无金标 → `llm_judge` 退化成评"报告写得像不像好诊断"，筛出来的是"报告漂亮"不是"诊断对"。P0 先只走 👍 这一条 gold standard，库涨得慢但干净；auto 通道的"自证信号"门控（证据覆盖 + 置信校准 + 工具链 + 跨层探查）留 P1 设计。

### 3.2 文档建模：一 Report 一 Point，不切块

DiagnosisReport 全文 200-500 字，无需再 chunk。Qdrant 一个 point 对应一次诊断。

**索引端 passage 构造**（重点：加结构化锚，避免 root_cause 太短导致 embedding 飘）：

```
[诊断元数据] 信号类型: {逗号分隔 evidence.golden_signals[].signal_type} | 类别: {primary_category} | 层级: {symptom_tier} | 涉及文件: {affected_files}

{user_report}

{root_cause}

{fix_suggestion 全文}
```

元数据用自然语言 `[诊断元数据]` 前缀放在 passage **开头**——bge-m3 对前缀位置权重更高，能更有效影响 embedding 方向，确保同类 case 的信号类型/类别能参与相似度匹配。`fix_suggestion` 保留全文不截断（含代码片段如字段名/函数签名，截断可能丢失关键语义；bge-m3 支持 8192 token，全文 500 字完全够用）。结构化字段**同时**进 Qdrant payload（见下节），双路保险。

### 3.3 Payload 字段

```python
PointStruct(
    id=case_id,  # UUID
    vector=embedding,
    payload={
        "trace_id": trace_id,                 # 去重用
        "case_id": evidence.case_id,
        "category": report.primary_category,   # "performance"/"frontend_crash"/...
        "symptom_tier": report.symptom_tier,    # "frontend"/"backend"/"cross_layer"
        "signal_types": [s.signal_type for s in evidence.golden_signals],
        "affected_files": report.affected_files,   # 可能多个文件，全部列出增强跨文件检索
        "root_cause": report.root_cause,
        "confidence": report.confidence,
        "source": "user_upvote",                # P0 只有这一种
        "created_at": isoformat,
        "user_report_snippet": evidence.user_report[:200],
        "fix_snippet": report.fix_suggestion[:300],
    }
)
```

---

## 4. Embedding 选型：bge-m3

| 维度 | text-embedding-3-small（现有猜测） | **bge-m3**（推荐） |
|---|---|---|
| 中文短查询 recall | ~0.78 | ~0.91 |
| 代码术语混合 | 中 | 佳（专训） |
| 维度 | 1536 | **1024** |
| 成本 | $0.02/1M | 免费（本地） |
| sparse 三模 | ❌ | ✅（P1 可开 hybrid） |

**P0 部署**：TEI 起 bge-m3 容器，Qdrant collection 重建为 1024 / COSINE。

```bash
# GPU（推荐）
docker run -p 8080:80 --gpus all \
  ghcr.io/huggingface/text-embeddings-inference:1.2 \
  --model-id BAAI/bge-m3

# CPU fallback（无 GPU 时使用，推理较慢但对 side project 够用）
docker run -p 8080:80 \
  ghcr.io/huggingface/text-embeddings-inference:1.2 \
  --model-id BAAI/bge-m3
```

调用侧：

```python
async def embed_texts(texts: list[str]) -> list[list[float]]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "http://localhost:8080/embed",
            json={"inputs": texts, "truncate_dim": 1024},
        )
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json()]
```

> ⚠️ 切模型意味着 `historical_cases` 要重建（1536 → 1024 维度不兼容）。P0 阶段库里数据量小（👍 驱动增长慢），重建成本可接受。若将来不想重建，可另建 `historical_cases_v2` 双写过渡。

---

## 5. Qdrant Collection 配置

```python
client.create_collection(
    collection_name="historical_cases",
    vectors_config=models.VectorParams(
        size=1024,
        distance=models.Distance.COSINE,
    ),
    hnsw_config=models.HnswConfigDiff(m=16, ef_construct=200),
)

# 标量量化：内存减半，精度损失 2-5%
client.update_collection(
    collection_name="historical_cases",
    quantization_config=models.ScalarQuantizationConfig(
        scalar=models.ScalarType.INT8,
    ),
)

# payload 索引（过滤必建）
for field, kind in [("category", "keyword"), ("symptom_tier", "keyword"), ("source", "keyword"), ("created_at", "datetime")]:
    client.create_payload_index("historical_cases", field, kind)
```

> 📌 **迁移步骤**（从旧 1536 维 → 新 1024 维）：新建 `historical_cases_v2` → 重新索引（当前库空，秒完）→ 删除旧 collection → 将 v2 重命名为 `historical_cases`。未来数据量大了可用 Qdrant alias 做零停机切换。

数据点规模：side project 级 100-1000 条，HNSW 暴力搜索延迟可忽略。

---

## 6. 检索与注入

### 6.1 查询文本构造（与索引端完全对称）

```
[诊断元数据] 信号类型: {逗号分隔 signal_types} | 层级: {evidence.correlations ? "cross_layer" : 单端 tier}

{evidence.user_report}
```

查询端与索引端（§3.2）**同一份 passage 模板**：都只含 evidence 可得字段（signal_types + tier + user_report），不含任何诊断输出（root_cause/fix/category/affected_files）。匹配语义干净--纯症状相似度，查询向量与索引向量在同一子空间内可比。

### 6.2 检索执行

```python
async def search_historical_cases(
    evidence: NormalizedEvidence,
    k: int = 5,           # 多拿，后置过滤
) -> list[RetrievedCase]:
    query_text = _build_query_text(evidence)
    vector = (await embed_texts([query_text]))[0]

    hits = client.search(
        collection_name="historical_cases",
        query_vector=vector,
        limit=k,
        with_payload=True,
    )

    # 排除自身 trace_id
    _self_trace = evidence.trigger_trace_ids[0] if evidence.trigger_trace_ids else ""
    hits = [h for h in hits if h.payload["trace_id"] != _self_trace]

    # 相似度阈值过滤（避免不相关历史干扰 Agent）
    hits = [h for h in hits if h.score >= 0.75]

    return [_hit_to_case(h) for h in hits[:3]]   # 最终 top-3（可能不足 3 条）
```

### 6.3 Prompt 注入格式

```markdown
## 历史相似诊断参考（来自知识库）

以下是与当前问题相似的已解决 Bug，仅供参考其诊断思路，请勿机械套用：

### Case 1（相似度: 0.89，来源: 用户点赞）
- 用户报告: "创建任务后页面卡死，console 报 Cannot read properties of undefined"
- 类别: frontend_crash / cross_layer
- 根因: 后端 TaskResponse schema 未返回 tags 字段，前端 task.tags.length 抛 TypeError
- 修复: schemas/task.py 中 TaskResponse 增加 tags: list[TagResponse] = []

### Case 2（相似度: 0.82，来源: 用户点赞）
...

⚠️ 以上仅为历史参考，请基于当前实际证据独立判断。
```

- 标注 `来源`（P0 全是"用户点赞"，但字段留着 P1 auto 通道扩展）
- 标注 `类别`（跨层 / 单端诊断路径不同，Agent 能区分）
- 末行免责防机械套用

### 6.4 接入点

在诊断节点 `src/engine/nodes/diagnosis_agent.py` 的 `_diagnosis_agent_node` 里、构造好 base_prompt 之后、组装 initial_messages 之前，调用 `search_historical_cases(evidence)` 并 append。**不要改 `src/engine/agent.py:_build_system_prompt()` 的签名**--它无参、同步，且在 agent 构建时（模块级缓存 `build_diagnosis_agent`）也被调用；检索注入必须放在 per-session 的诊断节点里，否则会污染缓存的全局 prompt。

```python
# src/engine/nodes/diagnosis_agent.py :: _diagnosis_agent_node
base_prompt = _build_system_prompt()
try:
    cases = await search_historical_cases(evidence, k=5)
    if cases:
        base_prompt += "\n\n" + _format_retrieved_cases(cases)
except Exception:
    logger.warning("historical_cases_search_failed", exc_info=True)
initial_messages = [SystemMessage(content=base_prompt), HumanMessage(content=evidence_text)]
```

---

## 7. 写入接入

```python
# 在 reporter node 之后 / 前端 👍 回调均可触发，P0 走前端回调
async def maybe_index_diagnosis(
    report: DiagnosisReport,
    evidence: NormalizedEvidence,
    *,
    source: Literal["user_upvote"] = "user_upvote",
) -> None:
    # 硬拦截：报告完整性
    if not (report.root_cause and report.affected_file and report.fix_suggestion):
        return

    passage = _build_passage_text(report, evidence)   # 见 §3.2
    vector = (await embed_texts([passage]))[0]
    point = _build_point(report, evidence, vector, source)
    # case_id（UUID）作 point id → upsert 天然幂等，无需前置去重锁
    point.id = evidence.case_id

    # 同 trace_id 重复写入仅告警，不拦截
    trace_id = evidence.raw_refs.get("trace_id")
    if trace_id and await _dedup_exists(trace_id):
        logger.info("duplicate_trace_id_upvote", trace_id=trace_id)

    await client.upsert(collection_name="historical_cases", points=[point])


# 前端 👍 回调示例（FastAPI）
# ⚠️ load_run 依赖 Langfuse/checkpoint 的落盘时序：用户秒点 👍 时 report 可能尚未落完。
# P0 妥协：load_run 内部加重试（最多 3 次，间隔 500ms），或从内存 checkpoint 取。
# 更优解（P1）：诊断完成时即把 report+evidence 落到自有 store，👍 时从自有 store 取--
#   彻底消除落盘 lag、解耦 graph 运行时，并顺带支撑 👎 failed-case 回填。
@app.post("/feedback/{run_id}/upvote")
async def upvote(run_id: str):
    report, evidence, trace_id = await load_run(run_id)   # trace_id 取自 evidence.trigger_trace_ids
    asyncio.create_task(maybe_index_diagnosis(
        report, evidence, source="user_upvote", trace_id=trace_id, case_id=run_id,
    ))
    return {"ok": True}


# 前端 👎 回调示例（FastAPI）—— 不打分、不入库，仅记结构化日志供 P1 failed case collection 回填
@app.post("/feedback/{run_id}/downvote")
async def downvote(run_id: str):
    report, evidence, trace_id = await load_run(run_id)
    logger.info("user_downvote",
                trace_id=trace_id,
                root_cause=report.root_cause[:200])
    return {"ok": True}
```

---

## 8. 验证策略

P0 不硬跑 15 独立 case 的 A/B（各 case 不相关，历史检索无增量可体现，差异在噪声级）。做**主动可控验证**：

**Step 1：同类变体检索召回**
- 拿 3 个 gold recipe 各造 2-3 变体（同 bug 不同注入点 / 不同端点，如 PERF-020 N+1：tasks→comments、projects→members）
- 用变体的 evidence 检索，看 top-3 能否召回"同源 recipe 的原版诊断"
- 预期：同源变体召回@3 ≥ 0.8，跨类别 ≤ 0.3

**Step 2：异类污染检查**
- 3 个异类 case 检索 top-3 注入，人工看 Agent 是否被带偏（是否出现"参考 Case N"的机械套用）

**Step 3：端到端 smoke（可选）**
- 同 15 case，A 无 RAG / B 有 RAG，不追求 p<0.05（样本小），看 per-case 差异中"同类 bug 反复出现"的那几个是否受益

---

## 9. 实施排期（P0 · 2d）

| 半天 | 内容 | 改动文件 |
|---|---|---|
| AM1 | bge-m3 + TEI 起容器；重建 `historical_cases`（1024/cosine/int8，走 v2→重命名迁移步骤）；payload 索引 | infra（docker / Qdrant） |
| PM1 | `maybe_index_diagnosis` 完整实现；👍 前端回调 API；去重（trace_id） | `src/memory/long_term/case_store.py` + `src/api/feedback.py` |
| AM2 | `search_historical_cases` 实现；接入 `diagnosis_agent.py` 诊断节点；查询文本构造 | `src/memory/long_term/case_retriever.py`（新建）+ `src/engine/nodes/diagnosis_agent.py` |
| PM2 | Prompt 注入格式 + 验证（3×变体召回 + 异类污染） | `src/prompts/` 或 inline |

---

## 10. 面试讲法

> "我做了历史诊断 RAG：每次用户点 👍，诊断报告自动 embed 进 Qdrant，新诊断时检索 top-3 相似历史 inject 到 system prompt。这里有个关键取舍——最初设计过 `llm_judge >= 0.85` 的 auto 静默通道，但诊断场景真实无金标，llm_judge 评的是'报告质量'不是'诊断正确性'，筛出来可能'报告漂亮但根因浅'（比如 FE-020 类'加可选链'症状层修补），污染库。所以 P0 只走 👍 一条 gold standard，库涨得慢但干净；auto 通道留 P1 用'自证信号'（证据覆盖 + 置信校准 + 工具链 + 跨层探查）替代 llm_judge。Embedding 选型从 text-embedding-3-small 切到 bge-m3，中文 bug 报告 + 代码术语混合场景 recall 从 ~0.78 提到 ~0.91，TEI 起容器成本可接受。"

---

## 11. 后续延伸（P1+ 候选项，P0 不做）

| 方向 | 说明 |
|---|---|
| **检索工具化（关键路径）** | 把 `search_similar_cases` 做成 agent 工具，让 agent 形成根因假设后**带假设查询**。静态注入下"症状相似/根因相似"不可兼得（查询时不知根因），工具化是拿到**根因相似性**的正解，而非普通增强 |
| auto 静默通道 | 用"自证信号"替代 llm_judge：`coverage≥0.6 + calibrated_conf≥0.75 + tool_chain≥0.7 + not _missed_deeper_layer` |
| failed case collection | 存 👎 case 的 `agent_root_cause + why_wrong(可选)`，检索到高相似时 inject "曾走过此方向但未解决，请核查" |
| KB 陈旧性 | payload 已留 `affected_files` 锚点；加 `code_fingerprint`（文件 hash / 关键符号），按 `created_at` 新鲜度衰减，或静态剔除"引用符号已不存在"的过期案例（代码重构后避免误导） |
| sparse hybrid | bge-m3 sparse 端补 "N+1" / "selectinload" 术语检索；P0 若起步即建双向量可省去 P1 重新 embed |
| rerank 提精度 | top-3 前先取 top-10，过轻量 rerank（cross-encoder 或 llm 判分） |
| 去重升级 | `(trace_id, rc_hash, affected_files)` 三元组 |
| 同类别提权 | 检索 filter 或 rerank 时同类别加权（注意：P0 查询端无 category，需先从 evidence 推断或走 sparse 关键词带出） |

---

要我把 `maybe_index_diagnosis` / `search_historical_cases` / `_build_passage_text` 这三段直接落到你现有 `index_diagnosis` 接口上出代码，还是先这样 md 合入 repo 再开工？