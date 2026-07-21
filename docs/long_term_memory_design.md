# DiagDoctor 长期记忆系统设计

> 本文档是长期记忆系统的**总设计**，后续实现以此为准。
> 收编并取代 `history_gold_bugs.md`（后者作为 P0 episodic 子集的早期版本保留为演进记录，见附录 A 的差异表）。
> 定位：**简历项目**。设计取舍以"面试讲得圆 + 有非 trivial 判断 + 无硬伤"为标准，不做生产级过度工程（见 §10）。

---

## 1. 目标与定位

给 DiagDoctor（诊断 bug 的 agent）一套长期记忆：把历史诊断沉淀为可检索知识，新诊断时复用，形成"越用越准"的闭环。

**与初版（`history_gold_bugs.md`）的关键差异**：初版只做了"episodic 记忆的存与取"。本文档把它升级为完整的 agent 记忆系统，补齐四个维度：

1. **记忆类型**：从只有 episodic（单 case）→ 加 semantic（反思提炼的 pattern）。
2. **编码**：从"全字段 embedding"→ **召回/利用三分离**（embedding 只承载症状，诊断输出走 payload）。
3. **检索**：从纯 cosine → **recency × importance × relevance 三因子**；P1 工具化双向量。
4. **治理与闭环**：从只写不读不反馈 → 陈旧性校验 + **效果回流闭环**。

**核心判断（保留自初版）**：
- 用 **👍 当标注器**：真实诊断无金标，`llm_judge` 评的是"报告质量"不是"诊断正确性"，auto 通道不可靠。P0 只走 👍，库涨得慢但干净。
- **语义空间对齐**：查询端（症状）与文档端（历史诊断）都是自然语言，embedding 检索可靠（对比废弃的"错误模式检索"：结构化 logs ↔ 人工模式描述，语义空间不一致）。

---

## 2. 业界参照与设计原则

| 标杆 | 核心做法 | 本系统借鉴点 |
|---|---|---|
| **Generative Agents**（Park 2023） | memory stream + **reflection**（episodic → semantic）；检索 = **recency × importance × relevance** | semantic pattern 层；三因子检索 |
| **MemGPT / Letta** | 分层记忆；**agent 自主管理**（archival_search / edit_memory） | P1 检索工具化，agent 主动带假设查询 |
| **Claude Code 文件记忆** | 类型化；**verify before recommend**；可更新/删除 | 记忆可治理（陈旧校验、衰减） |
| **LangSmith / Braintrust example store** | 标注 good case 做 few-shot；**有效性回流** | 反馈闭环（effectiveness 回填） |

**五条设计原则**：
1. **embedding 是召回对齐手段，不是信息存储**——索引向量只承载查询时可对齐的语义。
2. **召回 / 过滤 / 利用三分离**——embedding 管召回，payload 管过滤与展示，prompt 注入管利用。
3. **记忆可治理**——会过期、会冲突，需校验与衰减，不是只写不删的堆。
4. **效果回流**——检索注入的 case 是否真帮到诊断，要回填到该 case 的质量分。
5. **范围克制**——简历项目，生产级能力（仪表盘、多租户、冷启动 seed）只 acknowledged 不实现。

---

## 3. 记忆类型：episodic + semantic

| 类型 | 内容 | 存储 | 作用 |
|---|---|---|---|
| **episodic** | 一次诊断的完整记录（症状 + 根因 + 修复） | `historical_cases` | 给具体参考 |
| **semantic** | 跨同类 case 反思提炼的 bug pattern | `bug_patterns`（P1） | 给抽象规律 |

### 3.1 episodic（P0）

一 Report 一 Point，不切块（全文 200–500 字，无需 chunk）。point id = `run_id`（thread_id），upsert 天然幂等。

### 3.2 semantic pattern（P1）

累积 N 个同类 case（同 `category` / `signal_type`）后，用 LLM 反思提炼一条 pattern：

```python
BugPattern(
    pattern="本代码库的 N+1 多发于 ORM 关系未 selectinload；"
            "前端 undefined 报错多发于后端 schema 漏返字段",
    source_cases=[case_id, ...],   # 可溯源到 episodic
    category="performance",
    confidence=0.0,                # 由样本数 + 一致性决定
    created_at=...,
)
```

检索时 **case + pattern 双路注入**：case 给具体参考，pattern 给抽象规律。pattern 向量用 pattern 文本本身 embed。

> **简历价值**：这是整个系统最能拉开档次的设计——"记忆不只是存历史，还能跨案例学习出模式"。

---

## 4. 编码：召回/利用三分离

### 4.1 核心原则

初版把 `root_cause + fix_suggestion`（诊断输出）也拼进 embedding，但查询端只有症状——**两端不对齐，得到的是混合相似度**：症状被代码符号稀释、根因又因查询时无对齐而拿不到，两头不靠。

**改正**：

| 角色 | 承载内容 | 作用 |
|---|---|---|
| **Embedding 向量** | 只有查询时可对齐的"症状语义" | 召回对齐 |
| **Payload** | 结构化锚 + 诊断输出全文 | 过滤 / rerank / 注入展示 |
| **Prompt 注入** | 从 payload 取 root_cause + fix | 利用 |

### 4.2 Embedding passage（索引端 = 查询端，真正对称）

```
{user_report}
```

- 两端都从 `evidence.user_report` 取，**完全同模板**，向量在同一症状子空间可比。
- **C（hybrid 重构）**：向量只装 `user_report`（自然语言，bge-m3 可靠）。结构化信号（`signal_types`/`tier`）走 Qdrant payload **filter**（精确匹配，不进向量）；`golden_signals.summary`（含 `SELECT * FROM tasks` 等代码标识符）留 payload 不进向量--对齐项目 `code_search` 原则（"语义向量对代码标识符不可靠"）。
- **移出 embedding**：`root_cause`、`fix_suggestion`、`category`、`affected_files`（诊断输出或查询时不可得字段）；`signal_types`、`tier`、`golden_signals.summary`（结构化/代码标识符，走 filter 或 payload）。
- tier filter 源：`derive_tier(evidence)`，索引端 payload 与查询端 filter 同源（§4.3 对称）。
### 4.3 tier 推算（索引/查询统一逻辑，消除对称性坑）

`symptom_tier` 原始类型只有 `frontend`/`backend`，`cross_layer` 是派生量。两端统一用：

```python
def _derive_tier(evidence) -> str:
    if evidence.correlations:
        return "cross_layer"
    # 单端：从 golden_signals 的 service_tier 多数投票
    tiers = [s.service_tier for s in evidence.golden_signals if s.service_tier]
    return max(set(tiers), key=tiers.count) if tiers else "backend"
```

索引端和查询端调用同一函数，真正对称（初版查询端硬编码 `"backend"` 破坏对称，已改正）。

---

## 5. 存储

### 5.1 Collection 规划

| Collection | 向量 | 用途 | 阶段 |
|---|---|---|---|
| `historical_cases` | 症状向量（1024/cosine/int8） | episodic 记忆 | P0 |
| `bug_patterns` | pattern 文本向量 | semantic 记忆 | P1 |

> **为 P1 双向量预留**：`root_cause` 不进症状向量，P1 可在 `historical_cases` 上加 named vector `root_cause_vector`，症状向量 + 根因向量共存，按场景选用。若 P0 就想省 P1 重建，建 collection 时直接用 named vectors（`symptom` + 预留 `root_cause`）。

### 5.2 Payload 字段（episodic，完整定义）

```python
PointStruct(
    id=run_id,                      # = thread_id，upsert 幂等
    vector=symptom_embedding,       # 只含症状（见 §4.2）
    payload={
        # ── 去重 / 溯源 ──
        "run_id": run_id,
        "trace_id": trace_id,       # bug 的 W3C trace（跨 session 重复检测）
        # ── 结构化锚（filter / rerank 用）──
        "category": report.primary_category,
        "symptom_tier": report.symptom_tier,   # 原始单端：frontend/backend
        "is_cross_layer": bool(evidence.correlations),
        "signal_types": [s.signal_type for s in evidence.golden_signals],
        "affected_files": _to_list(report.affected_file),  # 单数→列表
        "root_cause_tier": report.root_cause_tier,
        # ── 诊断输出（注入 LLM 用，全文不截断）──
        "root_cause": report.root_cause,
        "fix_suggestion": report.fix_suggestion,
        "confidence": report.confidence,
        "user_report_snippet": evidence.user_report[:200],
        # ── 治理字段（见 §7）──
        "code_fingerprint": {...},   # affected_files 关键符号 hash
        "hit_count": 0,              # 被检索命中次数
        "effectiveness": 0.0,        # 回流的有效性分（见 §8）
        # ── 元数据 ──
        "source": "user_upvote",     # P0 唯一；P1 可有 "expert_curated"
        "created_at": isoformat,
    },
)
```

payload index 必建：`category`、`symptom_tier`、`source`、`trace_id`、`created_at`（`trace_id` 用于去重查询，初版漏建）。

### 5.3 去重语义厘清（修复初版矛盾）

三个 ID 各管一层，不再混淆：

| ID | 含义 | 去重作用 |
|---|---|---|
| `run_id` / thread_id | 一次诊断会话 | **point id**，upsert 幂等（同诊断重复 👍 覆盖不新增） |
| `trace_id` | 一个 bug 的 W3C trace | 检测"同 bug 多次诊断"；同 trace_id 多条 case **各自存为不同 point**，但检索时按 trace_id 去重结果（同 trace_id 只取最优一条），避免占满 top-k |
| `case_id` | 不再独立引入 | 统一等于 `run_id`（初版 `evidence.case_id` 不存在，已移除） |

---

## 6. 检索

### 6.1 三因子排序（替掉纯 cosine + 拍脑袋阈值）

```
score = relevance × recency × importance
```

- **relevance**：cosine(查询症状向量, 索引症状向量)。
- **recency**：`exp(-Δt / τ)`，Δt = 距 `created_at` 天数，τ 按代码库变更频率调（默认 90 天）。代码库在演进，旧 case 过期风险，新 case 优先。
- **importance**：`0.5·confidence + 0.3·normalize(hit_count) + 0.2·effectiveness`。`effectiveness` 是**被 👍 验证的有用复用累积分（只升，§8.1）**--case 被召回且该会话最终 👍 时 +0.1 累积；不设降权（👎 不回填，归因不清，见 §8.1）。被召回且帮到诊断的 case 更重要。

> 三因子的权重和 relevance 阈值**用 gold case 对标定**（见 §9.1），不再是拍脑袋的 0.75。

### 6.2 P0 检索流程（静态注入）

```python
async def search_historical_cases(evidence, k_final=3):
    query_vec = await embed_single(_build_query_text(evidence))   # §4.2
    hits = await client.search(collection="historical_cases",
                               query_vector=query_vec, limit=10,
                               with_payload=True)
    # 1. 排除自身（全部 trigger_trace_ids，不只第一个）
    self_ids = set(evidence.trigger_trace_ids)
    hits = [h for h in hits if h.payload["trace_id"] not in self_ids]
    # 2. 按 trace_id 去重（同 trace 只留最优）
    hits = _dedup_by_trace(hits)
    # 3. 三因子重排
    scored = [_three_factor_score(h) for h in hits]
    # 4. relevance 阈值过滤（标定值）
    scored = [s for s in scored if s.relevance >= RELEVANCE_THRESHOLD]
    # 5. top-k
    return scored[:k_final]
```

### 6.3 空召回处理（初版缺失）

三因子过滤后若 **0 条命中**：注入 0 条（等于无 RAG），记结构化日志 `rag_empty_recall`（可能阈值过高或库空），不降级塞噪声。

### 6.4 P1 工具化双向量（§11 困境的正解）

把 `search_historical_cases` 做成 agent tool。P0 静态注入只能拿**症状相似**（查询时不知根因）；P1 agent 形成**根因假设**后，用假设查独立的 `root_cause_vector`，拿到**根因相似**。这解决"症状相似/根因相似不可兼得"——P0 接受症状相似天花板，P1 用工具化突破。

> 同时解决 `category` 矛盾：P1 工具化时 agent 能给出 category，做同类别 filter；P0 静态阶段查询端无 category，就接受无 filter（不再像初版那样既说做又说做不了）。

### 6.5 注入格式

```markdown
## 历史相似诊断参考（来自知识库）

以下是与当前问题相似的已解决 Bug，仅供参考其诊断思路，请勿机械套用：

### Case 1（综合分: 0.82，来源: 用户点赞）
- 用户报告: "创建任务后页面卡死，console 报 Cannot read properties of undefined"
- 类别: frontend_crash / cross_layer
- 根因: 后端 TaskResponse schema 未返回 tags 字段，前端 task.tags.length 抛 TypeError
- 修复: schemas/task.py 中 TaskResponse 增加 tags: list[TagResponse] = []

⚠️ 以上仅为历史参考，请基于当前实际证据独立判断。
```

注入位置：`diagnosis_agent._diagnosis_agent_node` 里 `base_prompt` 之后、`initial_messages` 之前。**不改 `_build_system_prompt()` 签名**（它无参、同步、模块级缓存，改了会污染全局 prompt）。

---

## 7. 治理

### 7.1 陈旧性（code fingerprint）

payload 存 `code_fingerprint`：`affected_files` 的关键符号 hash（函数名 / 类名 / 字段名）。检索时或周期性校验这些符号是否还存在于代码库：

- 符号仍在 → 有效。
- 符号消失 → 降权（importance × 0.3）或剔除，并记日志。

与 §6.1 的 recency 衰减合流：recency 软衰减（时间）+ fingerprint 硬校验（代码变更）。

### 7.2 冲突检测（P1）

同症状但不同 `root_cause` 的 case 标记冲突。注入时若命中冲突 case，提示 agent："历史上有 N 种不同诊断方向，请核查"。避免单一历史 case 误导。

### 7.3 衰减

recency 因子已承担时间衰减；持续低 `effectiveness` 的 case 由 §8 闭环降权。不另设硬遗忘策略。

---

## 8. 反馈闭环（让"越用越准"真正成立）

初版标语写"越用越准"，但实际只写不读、读了不反馈——闭环是断的。补齐：

```
检索注入 case → 记录命中的 case_id 到 run state
    │
    ▼
诊断结束 → 该 run 的 👍/👎/诊断成败
    │
    ▼
回填到这些 case 的 effectiveness 字段
    │
    ▼
下次检索 importance 因子纳入 effectiveness → 低效 case 降权
```

### 8.1 effectiveness 回填（只升不降）

- 检索时：把命中的 `case_id` 列表写入 `DoctorState`（`retrieved_case_ids` 字段）。
- 诊断后 👍：命中的 case `effectiveness` 上调（+0.1，上限 1.0），`hit_count` +1。归因：👍 时 case 至少参与了被认可的诊断，加权方向正确。
- 诊断后 👎：**不回填**（见下）。`effectiveness` 只靠 👍 单调累积，无降权机制。

> **为什么不降权**：👎 的失败归因不清--可能是召回的 case 症状似根因异（对当前不适用，但 case 本身没错）、agent 推理错、或根因不在库（覆盖盲区），一刀切降权会冤枉好 case。👎 只记结构化日志（为未来失败 pattern 提炼留数据源），不影响 case 质量分。"越用越准"因此弱化为"越用越丰富 + 有效 case 被强化"，不声称"低效 case 被淘汰"。

### 8.2 failed case 处理（搁置，不做负样本注入）

**不做**：👎 case 不存为负样本直接注入。原因：① 归因不清（见 §8.1）；② 失败负样本单位价值密度低（给"别这样"的警告，需 agent 二次推理，ROI 低于正样本和 pattern）；③ 持续积累增加上下文压力。

**保留数据源**：👎 只写结构化日志（`agent_root_cause` / `category` / `confidence`），为未来"P1-b 失败 pattern 提炼"留数据（从多个同类失败聚类出"易错点 pattern"，以 pattern 形态注入，而非存原始失败）。这是 P1-b 的镜像，属 P1-b 范畴，不单列。

### 8.3 写入门控（平衡 selection bias）

P0 仍以 👍 为唯一自动触发，但 **acknowledged 👍 的 selection bias**：用户只点赞看得懂、能验证的诊断，库会偏向简单/表层 case，缺复杂深根因 case。缓解：

- P0：接受该 bias（auto 通道更不可靠），在文档与面试讲法中明确说明。
- P1：补 **专家手动入库**通道（`source="expert_curated"`），由开发者手动将复杂 case 入库，平衡分布。这是区别于不可靠 `llm_judge` auto 的正路。

---

## 9. 验证策略

### 9.1 阈值标定（初版缺失，必须做）

用 gold case 对（标注好的同类 / 异类）跑一遍三因子各分量的相似度分布，画分布找分离点：

- 定 `RELEVANCE_THRESHOLD`（同类分布与异类分布的分离点）。
- 定三因子权重（α/β/γ），使同类召回@3 最大化、异类召回@3 最小化。

### 9.2 变体召回（同类变体）

拿 3 个 gold recipe 各造 2–3 变体（同 bug 不同注入点/端点），用变体 evidence 检索，看 top-3 能否召回同源原版。预期：同源召回@3 ≥ 0.8。

### 9.3 症状似根因异区分（初版缺失的关键测试）

构造"**不同 bug 但症状相似**"的 case 对（如"页面卡死"分别是 N+1 / 死锁 / 前端大列表），验证检索能否区分、不互相污染。这是 RAG 最易翻车场景，比同源变体测试更有区分度。

### 9.4 闭环效果（P1 验证）

观察 `effectiveness` 回填后，低效 case 是否被稳定降权；同类 bug 反复出现时，受益 case 的诊断质量是否上升。

### 9.5 不做量化 A/B

15 个独立 case 无法体现历史检索增量（各不相关），强行 A/B 是噪声级。用 9.2/9.3 的可控验证替代——这是边界判断，面试讲清即可。

---

## 10. 已知限制（acknowledged，不实现）

| 限制 | 说明 | 处理 |
|---|---|---|
| 👍 selection bias | 库偏向简单 case | §8.3，P1 补专家通道 |
| 冷启动 | 库靠 👍 涨，初期空召回 | 接受冷启动期 RAG 无效，已知 |
| 隐私 / 访问控制 | passage 含私有代码细节 | Qdrant 限定内网访问，已知 |
| 检索可观测性 | 无仪表盘 | 仅结构化日志（`rag_empty_recall` 等），不做仪表盘 |
| 多租户 / 生产部署 | 超出项目定位 | 不做 |
| effectiveness 降权 | 👎 归因不清,降权冤枉好 case | §8.1,只 👍 单调累积 |

> 这些是**生产级能力**，对简历项目做了反而是过度工程（显得不会范围控制）。面试时作为"已知限制 + 演进方向"讲即可。

---

## 11. 分阶段实施

### P0 — episodic 记忆闭环（~4d）

| 内容 | 改动 |
|---|---|
| 编码三分离：embedding 只症状，诊断输出进 payload | `case_store._build_passage_text` / `_build_point` |
| 写入：👍 触发 + 完整性硬拦截 + trace_id 去重 | `case_store.maybe_index_diagnosis` / `api/feedback.py` |
| 三因子检索 + 阈值标定 + 空召回处理 | `case_retriever.py`（新建） |
| 静态注入诊断节点 | `engine/nodes/diagnosis_agent.py` |
| 反馈闭环：`retrieved_case_ids` + effectiveness 回填 | `state.py` + `case_store` + `feedback.py` |
| 陈旧性 fingerprint 校验 | `case_store` + 周期任务 |

### P1 — semantic + 工具化（~4d）

| 内容 | 改动 |
|---|---|
| semantic pattern 反思提炼 + `bug_patterns` collection | 新建 `pattern_store.py` |
| 工具化双向量检索（root_cause_vector） | `case_retriever` 做 tool + named vector |
| 冲突检测 + 专家手动入库通道 | `case_store` |

> P1-b 只做**正向** pattern(从 👍 case 提炼);失败 pattern(👎 镜像)随 👎 通道搁置(§8.2)。

### 不做

见 §10。

---

## 12. 面试讲法

> "我设计了一个 agent 长期记忆系统，参考 Generative Agents 和 example store：
> ① **编码上做召回/利用三分离**——embedding 只承载查询可对齐的症状语义，诊断输出走 payload，避免诊断输出污染召回向量得到混合相似度；
> ② **检索用 recency × importance × relevance 三因子**，不是纯 cosine，阈值用 gold case 对标定；
> ③ **记忆分 episodic case 和 semantic pattern 两层**，pattern 由同类 case 反思提炼，让记忆能跨案例学习；
> ④ **👍 回填命中的 case 有效性**（被复用且帮到诊断的 case 加权）；👎 **不降权**--失败归因不清（召回不相关/agent 推理错/覆盖盲区），降权会冤枉好 case，主动砍掉，失败信号只留作 pattern 提炼输入；
> ⑤ P1 把检索工具化，agent 带根因假设查独立的根因向量，拿根因相似而非症状相似。
> 核心取舍是砍掉 llm_judge auto 通道——真实诊断无金标，llm_judge 评的是报告质量不是诊断正确性，筛出来'报告漂亮但根因浅'会污染库，所以 P0 只走 👍 一条 gold standard。"

---

## 附录 A：与 `history_gold_bugs.md` 的差异

| 维度 | 初版（history_gold_bugs） | 本文档 |
|---|---|---|
| 记忆类型 | 仅 episodic | episodic + semantic |
| 编码 | 全字段 embedding（root_cause/fix 进向量） | 三分离（诊断输出不进向量） |
| 检索 | 纯 cosine + 0.75 | 三因子 + 标定阈值 |
| 治理 | 仅去重告警 | 陈旧 fingerprint + 衰减 + 冲突(P1) |
| 反馈闭环 | 无 | effectiveness 回填 + failed case |
| point id | §3.1 说 run_id / §7 说 case_id(UUID)，矛盾 | 统一 = run_id |
| 去重语义 | trace_id / case_id / run_id 混淆 | 三 ID 各管一层（§5.3） |
| 字段名 | affected_file(单) / affected_files(复) 混用 | report 用单数，payload 用复数，明确转换 |
| symptom_tier | 注释含 cross_layer（类型无此值） | cross_layer 拆为 is_cross_layer 布尔 |
| 查询端 tier | 硬编码 "backend"（破坏对称） | 统一 `_derive_tier` 推算 |
| bge-m3 前缀 | "前缀位置权重更高"（论据错） | "结构化锚点稳定分类信号" |
| 阈值 | 0.75 拍脑袋 | gold case 标定 |
| 空召回 | 未定义 | 注入 0 条 + 记日志 |
| 自身排除 | 只排首个 trace_id | 排全部 |
| load_run lag | 后端重试 3×500ms | 前端时序约束（诊断完成才展示 👍） |

> `history_gold_bugs.md` 建议保留为演进记录（或归档至 `docs/archive/`），不再作为实现依据。
