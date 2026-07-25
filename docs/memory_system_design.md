# DiagDoctor 记忆系统设计(现状 + 考量)

> 本文档是记忆系统的**总设计**,反映当前已实现状态(code-verified)+ 设计考量与演进项。
> 取代 `long_term_memory_design.md`(旧版把已实现的 P1-a 双向量写成"未来",阈值未标定,embedder 未切 v4;旧版保留在 git 作演进记录)。
> 定位:**简历项目**。取舍以"面试讲得圆 + 有非 trivial 判断 + 无硬伤"为标准,不做生产级过度工程(见 §10)。
> 检索侧验证落地规格见 `docs/retrieval_test_design.md`。

---

## 1. 目标与定位

给 DiagDoctor(诊断 bug 的 agent)一套长期记忆:把 👍 认可的历史诊断沉淀为可检索知识,新诊断时复用,形成"越用越准"闭环。记忆是**增益,非依赖**--检索失败/空库都不阻塞诊断。

**核心判断**:
- 用 **👍 当标注器**:真实诊断无金标,`llm_judge` 评的是"报告质量"不是"诊断正确性",auto 通道不可靠。只走 👍,库涨得慢但干净。
- **语义空间对齐**:查询端(症状)与文档端(历史诊断)都是自然语言,embedding 检索可靠。

---

## 2. 业界参照与设计原则

| 标杆 | 核心做法 | 本系统借鉴 |
|---|---|---|
| **Generative Agents**(Park 2023) | memory stream + reflection;检索 = **recency × importance × relevance** | 三因子检索;semantic pattern 层 |
| **MemGPT / Letta** | 分层记忆;agent 自主管理 | 检索工具化,agent 带假设查询 |
| **Claude Code 文件记忆** | 类型化;verify before recommend | 记忆可治理(陈旧校验、衰减) |
| **LangSmith / Braintrust example store** | good case few-shot;有效性回流 | 反馈闭环(effectiveness 回填) |
| **MMR**(Carbonell & Goldstein 1998) | 相关性 - 冗余度,显式多样性 | 症状层 top-k 多样性(已实现,见 §7.2) |

**五条设计原则**:
1. **embedding 是召回对齐手段,不是信息存储**--索引向量只承载查询时可对齐的语义。
2. **召回 / 过滤 / 利用三分离**--embedding 管召回,payload 管过滤与展示,prompt 注入管利用。
3. **记忆可治理**--会过期、会冲突,需校验与衰减。
4. **效果回流**--检索注入的 case 是否真帮到诊断,回填到该 case 质量分。
5. **范围克制**--简历项目,生产级能力只 acknowledged 不实现。

---

## 3. 记忆类型:episodic + semantic

| 类型 | 内容 | 存储 | 状态 |
|---|---|---|---|
| **episodic** | 一次诊断完整记录(症状+根因+修复) | `historical_cases` | ✅ 已实现 |
| **semantic** | 跨同类 case 反思提炼的 bug pattern | `bug_patterns` | ⏳ P1-b 未实现 |

episodic:一 Report 一 Point,不切块(全文 200-500 字)。point id = UUID,upsert 幂等。

semantic(P1-b):累积 N 个同类 case 后 LLM 反思提炼 pattern,检索时 case+pattern 双路注入。**简历价值点**:记忆能跨案例学习出模式。当前未实现。

---

## 4. 编码:召回/利用三分离

### 4.1 核心原则

| 角色 | 承载内容 | 作用 |
|---|---|---|
| **Embedding 向量** | 只含查询时可对齐的 NL 语义(症状 / 根因文本) | 召回对齐 |
| **Payload** | 结构化锚 + 诊断输出全文 | 过滤 / rerank / 注入展示 |
| **Prompt 注入** | 从 payload 取 root_cause + fix | 利用 |

诊断输出(`root_cause`/`fix`)不进向量--查询端症状与文档端诊断输出语义空间不对齐,拼进向量得到混合相似度,两头不靠。

### 4.2 Embedding passage(索引端 = 查询端,对称)

- **症状向量**:`build_symptom_passage(evidence)` = `user_report`。索引/查询同源同模板,在同一症状子空间可比。
- **根因向量**:`report.root_cause`(索引)/ agent 根因 hypothesis(查询)。两端都是根因描述文本,对称。
- 结构化信号(`signal_types`/`tier`)+ 代码标识符(`SELECT * FROM tasks` 等)留 payload,不进向量--对齐"语义向量对代码标识符不可靠"。
- **embedder**:DashScope `qwen3.7-text-embedding`(1024 维,见 §5.1)。

### 4.3 tier 推算(索引端 payload 标签,查询侧不再用)

`derive_tier(evidence)`:`correlations` 非空 -> `cross_layer`;否则 `golden_signals.service_tier` 多数投票;无信号默认 `backend`。仅索引端标 payload `symptom_tier`(注入展示用),查询侧**不调用、不做 filter**(reversal,见附录 B)。

---

## 5. 存储

### 5.1 Collection + Embedder

| Collection | 向量 | 用途 | 状态 |
|---|---|---|---|
| `historical_cases` | `symptom` + `root_cause` 双命名向量(各 1024/cosine/int8) | episodic 记忆 | ✅ |
| `historical_cases_test` | 同上 | 检索测试隔离库 | ✅ |
| `bug_patterns` | pattern 文本向量 | semantic 记忆 | ⏳ P1-b |

- **双命名向量已实现**(P1-a):每 point 同时存 `symptom` + `root_cause` 向量,查询侧 `query_points(using=...)` 选一个。
- **INT8 标量量化** per-vector(~50% 内存,2-5% 精度损失);HNSW m=16, ef_construct=200。
- **payload 索引**:`category` / `symptom_tier` / `source` / `trace_id` / `created_at`。
- **Embedder**(`embedding.py`):DashScope `qwen3.7-text-embedding`,OpenAI 兼容 API,1024 维。配了 `EMBEDDING_BASE_URL`+`DASHSCOPE_API_KEY` 走 API **不回退**(防混模型);`EMBEDDING_BASE_URL` 空时才走 legacy TEI/本地 bge-m3(离线 dev,与 API 不可同库混用)。

### 5.2 Payload 字段(含使用审计)

| 字段 | 用途 |
|---|---|
| `case_id` | point id / 身份 |
| `trace_id` | bug 身份;索引去重 + 检索自排除(见 §5.3) |
| `root_cause` | 注入 LLM(全文) |
| `fix_suggestion` | 注入 LLM(全文) |
| `user_report_snippet` | 注入展示症状 |
| `affected_files` | 注入展示(历史 bug 在哪个文件,有参考价值) |
| `confidence` | importance 因子 |
| `effectiveness` | importance 因子(反馈闭环写入,认可关联分;见 §8.2) |
| `created_at` | recency 因子 |

> **精简原则**:payload 只留 scoring 输入(`confidence`/`effectiveness`/`created_at`)+ 自排除(`trace_id`)+ 注入内容(`root_cause`/`fix_suggestion`/`user_report_snippet`/`affected_files`)+ 身份(`case_id`)。display 化妆字段(category/tier 标签,root_cause 文本已含)+ 死字段全删。
> **已删 7 字段**:`category`、`source`(P1 category filter + 专家通道不做,无 filter/分流作用,display 冗余或恒定)、`is_cross_layer`、`symptom_tier`、`root_cause_tier`(tier filter 撤销后 tier 死)、`signal_types`(原计划 filter 闲置)、`hit_count`(与 effectiveness 冗余,§6.1)。`derive_tier` 函数保留(eval/单测用),payload 不存 tier。
> **payload 索引**相应精简:留 `trace_id`(去重/自排除)、`created_at`(recency 排序);删 `category`/`symptom_tier`/`source` 索引(无 filter 用不上)。

### 5.3 ID 语义 + trace_id 自排除(设计考量)

三个 ID 各管一层:

| ID | 含义 | 作用 |
|---|---|---|
| `case_id`(UUID) | 一次索引事件 | point id,upsert 幂等 |
| `trace_id` | 一个 bug 的 W3C trace(跨 session 复现同 bug) | 索引去重警告 + 检索自排除 |
| `run_id`/thread_id | 诊断会话 | (历史概念,现统一到 case_id/trace_id) |

**trace_id 自排除的作用**(设计考量,非纯技术必要):
- **测试**:**必需**。leave-one-out 不排除自己 = 召回自己,recall 失效。
- **生产**:当前诊断 case 还没 👍 入库(👍 在诊断后),排除当前 trace 是 **no-op**。只有同 bug 之前 👍 过(复现 bug)时才生效,且**两层不对称**:
  - 症状层(静态注入)**排除 self**:不直接注入"这 bug 上次怎么解的",避免 agent 抄答案,强制走证据 + 给多样上下文。
  - 根因层(按需工具)**不排除 self**(`exclude_trace_ids` 默认空):agent 形成假设后查,召回"我们解过这个 exact bug"= 理想的"越用越准"确认路径。

> trace_id 是 **bug 粒度身份**(同 bug 复现多次同 trace_id,不同 case_id)。自排除主要为测试 leave-one-out;生产里两层层级化处理 self-recall(症状避、根因留)是有意设计。

---

## 6. 检索

### 6.1 三因子排序(两层共享)

```
score = relevance × recency × importance
```

- **relevance** = Qdrant cosine(查询向量, 索引向量)。
- **recency** = `exp(-Δt / τ)`,Δt = 距 `created_at` 天数,τ = 90 天。读 payload `created_at`。
- **importance** = `0.5·confidence + 0.5·effectiveness`。读 payload。`effectiveness` 初始 0(冷启动),反馈闭环写入后生效。**砍掉 hit_count**(见下)。

**两层共享同一套三因子 + 同一管线**(见 §6.2),不拆"症状层三因子/根因层三因子"。

#### 设计考量:recency(`exp(-Δt/τ)`, τ=90)

- **exp 衰减标准**(推荐/搜索 recency 常用):近期权重高、远期温和淡出不归零,比 linear 均匀惩罚合理。
- **τ=90 是默认值,未从数据标定**--取决于 bug 复现周期,可从真实复现数据标定。诚实标注。
- **乘法非加法**:低 recency 强力压低总分(近期+相关才高分),是较强的 recency 偏好;加法允许老但相关竞争。设计选择。
- **冷启动未测**:测试 recency 常数(无时间差),生产才完整生效。
- **面试答法**:"exp 是 recency 标准做法;τ 默认 90 可标定;乘法表强 recency 偏好;冷启动未 exercise,诚实标注。"

#### 设计考量:importance(砍到两个信号)

- **砍掉 hit_count**:`hit_count`(召回/👍 计数)对诊断排序没正向帮助--召回多 ≠ 有用,可能是个万能但没用的近似 case。且当前实现里 `hit_count` 和 `effectiveness` 都只在 👍 时增加,`norm(hit_count) == effectiveness`(都 = `min(👍数/10, 1.0)`,HIT_COUNT_CAP=10、delta=0.1),数学上完全冗余。**删 hit_count,importance 只留 confidence + effectiveness**。
- **importance = `0.5·confidence + 0.5·effectiveness`**:`confidence`(诊断 LLM 置信度,先验)+ `effectiveness`(反馈累积,后验)。权重 0.5/0.5 把原 feedback 总权重(0.3+0.2)整体给 effectiveness,行为变化最小。可调。
- **理论支撑**:confidence + effectiveness 两信号概念标准(先验质量 + 后验反馈),但**具体公式无论文**,pragmatic heuristic。真实系统用学习权重。诚实标注。
- **effectiveness 是相关代理,非因果**:量化"被 agent 引用 + 被用户认可"的累积(见 §8),不等于"导致好诊断"(因果靠离线 ablation)。文档不称"有效性分",称**"认可关联分"**更诚实。
- **effectiveness 只升不降**:"没帮助"/👎 不降权(归因不清,见 §8.2)。

### 6.2 共享管线 `_search_named_vector`

两层同一套(代码 `case_retriever.py`):

```
overfetch (OVERFETCH=10, Qdrant query_points, using=<vector_name>)
  -> 排除自身 (exclude_trace_ids)
  -> 三因子打分 (_score_hit: relevance × recency × importance)
  -> trace_id 去重 (留最高分)
  -> 阈值过滤 (relevance >= <层阈值>)
  -> top-k (K_FINAL=3):症状层 diversify=True 走 MMR(§7.2:relevance vs root_cause 向量冗余,λ 权衡);根因层纯 score top-k
```

### 6.3 双向量(两层各司其职,非"补救对子")

| | 症状层 `search_historical_cases` | 根因层 `search_by_root_cause` |
|---|---|---|
| 向量 | `symptom` | `root_cause` |
| query | `build_symptom_passage(evidence)` = user_report | agent 根因 hypothesis |
| 阈值 | `SYMPTOM_RELEVANCE_THRESHOLD` = **0.60** | `ROOT_CAUSE_RELEVANCE_THRESHOLD` = **0.61** |
| 职责 | **广度**:同症状多根因方向 | **深度**:同根因跨症状复用 |
| 集成 | §6.5 静态注入(pre-agent,`rag_injection_enabled`) | §6.4 agent 工具(on-demand,`rag_root_cause_tool_enabled`) |
| 自排除 | 排除 self(trace_id) | 不排除 self(默认) |
| top-k | MMR(§7.2:`_select_mmr_topk`,relevance vs root_cause 向量冗余,λ 权衡) | 纯 score top-k(深度要同根因) |

**三因子/管线共享,末步 top-k 分层**:症状层 `diversify=True` 走 MMR(§7.2,relevance vs root_cause 向量冗余,λ 权衡保多根因方向);根因层纯 score top-k(深度要同根因,MMR 会惩罚它要的东西)。两层差异 = 向量 + 阈值 + query + 自排除策略 + top-k 选择策略。职责差异(广度/深度)既是设计意图,也落到 top-k 选择上。

**阈值已标定**(从合成检索测试全 cosine 分布,见 §9):症状 0.60、根因 0.61。两层衡量不同语义,分离点不同,各标各的。

### 6.4 根因层:agent 工具化(P1-a 已实现)

`search_historical_root_cause` 注册为 agent tool。agent 形成**根因假设**后按需调用,用假设查 `root_cause` 向量,拿**根因相似**--突破症状层"查询时不知根因"的天花板。`rag_root_cause_tool_enabled` 开关;关了也注册(schema 稳定),返回"未启用"。

### 6.5 症状层:静态注入 + 注入格式

诊断前 node 侧自动注入 top-k 同症状 case(`rag_injection_enabled`)。注入格式(`format_similar_cases`):

```markdown
## 历史相似诊断参考(来自知识库)
[⚠️ 冲突提示(若多根因)]
### Case 1 [id: hist-1](综合分: 0.82)
- 用户报告: "..."
- 根因: ...(已含类别信息,不单列 category)
- 修复: ...
- 涉及文件: app/schemas/task.py
⚠️ 以上仅为历史参考,请基于当前实际证据独立判断。
```

> `affected_files` 现展示在注入格式里(§5.2 精简后的核心字段)。每个 case 标 `[id: ...]` 暴露 `case_id`(§8.1 path 2):agent 在最终 JSON 的 `referenced_case_ids` 里声明本次实际参考了哪些 case,用户只能对这些被引用的 case 标"有帮助"。

### 6.6 空召回

三因子过滤后 0 条命中:注入 0 条(等于无 RAG),记 `rag_empty_recall` 日志,不降级塞噪声。

---

## 7. 治理

### 7.1 陈旧性(code fingerprint)

payload 存 `code_fingerprint`(affected_files 关键符号 hash)。检索时/周期性校验符号是否还在:在 -> 有效;消失 -> 降权(importance × 0.3)或剔除。与 recency 软衰减合流:recency 软(时间)+ fingerprint 硬(代码变更)。**状态:设计已定,周期校验未实现。**

### 7.2 冲突检测(P1-c 已实现)+ 多样性(MMR 已实现)

**冲突检测(P1-c,检测 only)**:`detect_conflict` 在注入时检查召回集是否含 **≥2 个不同 `root_cause` 文本**(normalized)。多根因时提醒 agent "历史有 N 种方向,请勿锚定 top-1,独立核查"。冲突键选 root_cause 文本 distinctness(`category` 太粗、`root_cause` 向量聚类受同领域异根因同簇限制)。**只检测+提醒,不改选择。**

**多样性(MMR,已实现)**:召回集原本可能 3 个同根因 dupe(无多样性),与症状层"给 LLM 多根因方向"职责相悖。症状层 top-k 改用 `_select_mmr_topk`(§6.2 末步 `diversify=True`)--Maximal Marginal Relevance(Carbonell & Goldstein 1998),贪心选:

```
MMR(c) = rel_norm(c) - λ · max_{s∈已选} cosine(root_cause_vec[c], root_cause_vec[s])
```

- **rel_norm** = 三因子分(`relevance × recency × importance`)按候选池 max 归一化到 [0,1],与 cosine 冗余项量纲可比 -> λ∈[0,1] 是干净的相关-多样权衡(0=纯 score,1=最大多样)。用三因子分(非裸 cosine)保留 recency/importance。
- **sim** = 候选间 `root_cause` 命名向量 cosine--diversify 的是根因方向(同症状异根因是去锚定场景)。向量查询时 `with_vector` 取(payload 不带向量)。
- **软惩罚非硬去重**:MMR 不硬并同根因 case,只降其 MMR 分。高相关同根因 case 仍可能被选(分低些);候选池 distinct roots 不足 k 时**仍填满 k**(冗余但相关,非噪声--§6.6"不塞噪声"指不相关 case,阈值已滤)。
- **λ=0.5 未标定**(无 gold 多样性数据),与 τ=90 / importance 0.5/0.5 同属手调 knob,诚实标注可调。
- **为什么不是文本去重**:曾考虑按 `root_cause` 文本 normalized 聚类(每类留 top-1),被否--`_normalize_root_cause` 只折叠空白,`"N+1 查询"`/`"N+1 query"`/`"ORM N+1"` 文本不同即不同簇,只在**逐字相同**时合并;合成 96 case 全唯一文本 -> 完全 no-op,生产 LLM 自由文本不同 bug 几乎不逐字相同,且 `_dedup_by_trace` 已去重同 bug,文本去重基本没活干。向量 sim 才抓语义同根因。
- **仅症状层**(广度);根因层(深度)保持纯 score top-k(同根因正是它要的,MMR 会惩罚它要的东西)。
- **OVERFETCH 保持 10**:同症状 distinct roots 是高 cosine 者(§9.1 同标签 mean 0.71),已在 top-10 内;MMR 的 relevance 项压住 diff-symptom 噪声(低 rel_norm),不必为 diversity 调大。若实测 distinct roots 频繁不足,再调大。
- **P1-c 冲突检测保留(双保险)**:MMR 主动保多样(改选择),冲突检测提醒防 top-1 锚定(改提示),互补不冗余。MMR 后多根因成常态,提醒更常触发但仍有价值(agent 仍需被告知"历史多方向、勿抄 top-1")。

> **比 rerank 更优先**:直接对齐症状层广度职责,有论文。已落地 MMR(向量 sim + λ)。

### 7.3 衰减

recency 因子承担时间衰减;持续低 effectiveness 的 case 由 §8 闭环不强化(只升不降,等于相对降权)。不另设硬遗忘。

---

## 8. 反馈闭环(让"越用越准"成立 + 归因干净)

### 8.1 双反馈路径(已实现)

**两条独立反馈路径,解耦诊断质量与召回质量**:

```
路径 1(诊断级,已实现):用户 👍/👎 诊断 -> 评诊断质量(👍 索引新 case;不再 backfill 召回 case)
路径 2(case 级,已实现):
  agent 出结果时声明"本次参考了 case [X,Y,Z]"(referenced_case_ids)
  -> 用户对【被引用的 case】逐个标"有帮助/没帮助"(POST /api/feedback/{run_id}/case)
  -> 标"有帮助"的 case effectiveness += (认可关联分)
```

- **agent 引用**:诊断报告附 `referenced_case_ids`(`ForcedDiagnosisReport` schema + system prompt 引导 agent 声明用了哪些召回 case,像论文引文)。`parse_diagnosis_report` 解析后,`diagnosis_agent` node 用 `clamp_referenced_case_ids` 收敛到 `⊆ retrieved_case_ids`(防幻觉:agent 只能引用被注入的 case)。透明性 + 归因基础。
- **case 级反馈**:端点 `POST /api/feedback/{run_id}/case` body `{case_id, helpful: bool}`,独立于诊断 👍/👎。绑 `run_id` 是为了从 checkpoint 读 `report.referenced_case_ids` 校验 `case_id ∈ referenced`(只接受被引用的 case,防乱标)。UI:"本次参考了 [X,Y,Z],有帮助吗?"。只问被引用的 2-3 个(不是所有召回),UX 可控。
- **backfill 触发**:case 被标"有帮助" -> `effectiveness += delta`。"没帮助"/不标 -> 不 +(见 §8.2)。

**为什么双路径**:之前"👍 诊断 -> 所有召回 case +"是粗归因--👍 诊断 ≠ 召回信息有帮助(agent 可能忽略召回 case 自己推出诊断,或只用了 3 个里的 1 个)。双路径把"诊断好不好"和"召回有没有用"分开评,且 case 级反馈只针对**被 agent 引用的** case,归因到"被用且被认可"。

### 8.2 effectiveness 的诚实定位

- **effectiveness = "认可关联分"**(被 agent 引用 + 被用户标"有帮助"的累积),**不是"有效性/因果帮助"**。
- **仍是代理,非因果**:"帮助"是反事实(没这 case 会怎样),生产观测不到。用户标"有帮助"是主观感知,不是因果证明。但这是**生产能拿到的最强代理**(agent 引用过滤 + 用户人验),比"👍 诊断给所有召回 +"干净得多。
- **因果靠离线 ablation**:同一 bug 跑两遍(有 case X vs 无 case X)比诊断质量,是唯一能量化"case 真帮没帮上"的方法。属端到端评测,不在在线闭环。
- **只升不降**:case 标"没帮助" -> 不 +(不减)。归因仍不清("没帮助"可能是"不适用此 bug"而非"坏 case"),保持单调,避开歧义。诊断 👎 也不降权(同因)。
- **不标不 +**(严格):用户不标 case = effectiveness 不涨。effectiveness 真正是"显式认可",不掺未验证的。代价:冷启动涨得慢,可接受。

### 8.3 当前实现(已落地)

双反馈路径已落地(2026-07-25,任务 3):
- **路径 1(诊断级)**:👍 索引新 case(`maybe_index_diagnosis`);👎 只 log。👍 **不再 backfill 召回 case**(任务 3c 移除--"👍 诊断给所有召回 +"是粗归因)。
- **路径 2(case 级)**:agent 最终 JSON 输出 `referenced_case_ids`(`ForcedDiagnosisReport` schema + system prompt `## 输出格式` 引导);`parse_diagnosis_report` 纯解析;`diagnosis_agent` node 用 `clamp_referenced_case_ids` 收敛到 `⊆ retrieved_case_ids`(防幻觉)。新端点 `POST /api/feedback/{run_id}/case` body `{case_id, helpful}`:校验 `case_id ∈ report.referenced_case_ids`(无 report -> 404;不在引用集 -> 422),`helpful=True` -> `backfill_effectiveness([case_id], delta=+0.1)`,`helpful=False` -> 只 log(只升不降)。
- **注入块** `format_similar_cases` 给每个 case 标 `[id: ...]`,agent 才有 id 可引用(前置 gap)。
- `effectiveness` 现为真正的"认可关联分"(被引用 + 被认可才累积),非"参与分"。常量 `EFFECTIVENESS_HELPFUL_DELTA=0.1`(原 `EFFECTIVENESS_UPVOTE_DELTA` 重命名,👍 不再用)。**非幂等**:每次 `helpful=True` +0.1(与任务 3 前 👍 backfill 一致;前端防重复点击)。
- **前端闭环**:ReportPanel 展示 `referenced_case_ids` + 每个 case 的"有帮助/没帮助"按钮 -> `POST /api/feedback/{run_id}/case`(`DiagnosePage` 传 `runId=state.case_id`,与 👍/👎 同源)。CopilotKit 走外层图(`DiagDoctorAgent.execute` -> `get_copilotkit_graph`),RAG 注入 + clamp + `report` 写 state 都生效;`state.report.referenced_case_ids`(clamped)同步到前端,端点再按 checkpoint 的 clamped 集校验(双保险)。

### 8.4 failed case 处理(搁置)

不做 👎 负样本注入(归因不清 + 单位价值密度低 + 增上下文压力)。👎 只写结构化日志,为 P1-b 失败 pattern 提炼留数据源。

### 8.5 写入门控

👍 为唯一自动触发。acknowledged 👍 selection bias(库偏简单/表层 case)。**不补专家通道**(P1 不做),接受该 bias,在文档与面试讲法中明确说明;冷启动期 RAG 无效已知。

---

## 9. 验证策略(已执行)

> **指标数字统一在 `docs/memory_system_metrics.md`(单一真相源,post-MMR)**。落地规格见 `docs/retrieval_test_design.md`。本节为策略概述。

### 9.1 阈值标定(已做)

合成 96 case(`templates×entities` 全唯一文本,无 cosine≈1.0 簇),leave-one-out,标定模式(`threshold=0`+`OVERFETCH=100` 收集全 95 候选 cosine 分布)。结果(2026-07-25,v4):

| 层 | 同标签 cosine(min/mean/max) | 异标签 cosine(min/mean/max) | 分离点 | 回填阈值 |
|---|---|---|---|---|
| 症状 | 0.43 / 0.71 / 0.95 | 0.24 / 0.44 / 0.76 | 0.597 | **0.60** |
| 根因 | 0.43 / 0.69 / 0.94 | 0.20 / 0.42 / 0.81 | 0.616 | **0.61** |

同/异标签均值清晰分离。阈值 0.60/0.61 已回填 `case_retriever.py` + 更新 `test_relevance_thresholds_calibrated`。

### 9.2 检索质量指标(已测)

| 层 | Precision@3 | HitRate@3 | MRR | 同标签 Recall@3 | 噪声 |
|---|---|---|---|---|---|
| 症状 | 1.000 | 1.000 | 1.000 | 0.200 | 0.000 |
| 根因 | 0.990 | 1.000 | 1.000 | 0.270 | 0.010 |

**Precision@3≈1.0 + MRR=1.0**:top-3 几乎全同标签且首个就同标签,零噪声--检索质量极好。同标签 Recall@3=0.20/0.27 是 **k-bound**(3/15、3/11),非缺陷;增 k 或 rerank 可提。

### 9.3 测了 / 没测

- ✅ **测了**:relevance(向量召回质量 + 阈值标定)。
- ❌ **没测**:recency/importance(冷启动常数,需时间差+👍 闭环数据);§3.5 假设扰动鲁棒性(未实现);token/accuracy(端到端 A/B,另立项)。
- **诚实上界**:合成根因是类 gold 干净文本,真实 agent 残缺假设召回更低,由端到端测。

---

## 10. 已知限制 + 生产 scaling 路径

### 已知限制(acknowledged,不实现)

| 限制 | 处理 |
|---|---|
| 👍 selection bias | 库偏简单 case | §8.5,接受 bias(专家通道不做) |
| 冷启动 | 接受初期 RAG 无效 |
| 隐私/访问控制 | Qdrant 限内网 |
| 检索可观测性 | 仅结构化日志,无仪表盘 |
| 多租户/生产部署 | 超出项目定位 |
| effectiveness 降权 | 👎 归因不清,只 👍 单调累积 |

### 生产 scaling 路径(库大了的演进,未实现,面试点)

当历史库从 96 涨到千+,同标签池变大,cosine 区分度下降(一堆 0.7-0.8 挤一起):

1. **overfetch↑**:retrieve 宽池(10->20-50),bi-encoder 便宜。
2. **三因子**:relevance × recency × importance 从宽池初选(已实现)。
3. **MMR diversity**(§7.2,已落地):症状层 root_cause 向量 sim + λ 贪心选,保多根因方向;库大时调 overfetch / λ 标定。
4. **rerank**(measured fallback):三因子分不开时,加 cross-encoder(`gte-rerank`)/ LLM-as-rerank(DeepSeek 给 top-N 打"对当前 bug 多有参考价值"分)。**约束**:层内重排,不跨层混分(保三分离)。**加之前先测**,带 before/after delta,勿 cargo cult。
5. **context-budget k**:`K_FINAL` 受 `agent_context_warning_ratio` 约束,diversity 比 count 重要。

> 当前 96 case Precision 1.0,无实测需求不加 rerank(过度工程)。MMR(向量 sim + λ)已落地对齐症状层广度职责;rerank 库大、测到需要时再加(带 delta)。

---

## 11. 面试讲法

> "我设计了一个 agent 长期记忆系统,参考 Generative Agents(Park 2023)的三因子检索 + example store 的有效性回流:
> ① **编码三分离**:embedding 只承载查询可对齐的 NL 语义(症状/根因文本),诊断输出走 payload,避免混合相似度;
> ② **双向量**:症状向量(广度,同症状多根因方向)+ 根因向量(深度,同根因跨症状复用),两层共享三因子管线,各标各的阈值(0.60/0.61,合成测试标定);
> ③ **三因子** `relevance × recency × importance`:recency exp 衰减(τ=90,可标定),importance = 0.5·confidence + 0.5·effectiveness(砍掉冗余 hit_count);权重手调,诚实标注未学习;
> ④ **反馈闭环双路径**:诊断 👍/👎 评诊断质量;case 级"有帮助"评召回质量(agent 引用 + 用户认可,effectiveness 才 +)。归因到"被用且被认可"的 case,非"被召回";诚实讲仍是相关代理,因果靠离线 ablation;只升不降(归因不清);
> ⑤ **冲突检测 + 多样性**:同症状多根因时提醒防 top-1 锚定(P1-c detect-only);症状层 top-k 走 MMR(`_select_mmr_topk`,relevance vs root_cause 向量冗余 + λ 贪心选,保多根因方向,有论文;λ=0.5 未标定诚实标注);
> ⑥ **验证**:合成 96 case leave-one-out,Precision@3≈1.0、MRR=1.0,阈值 0.60/0.61 标定;诚实标注冷启动未 exercise recency/importance、合成根因是上界。
> 核心取舍:砍 llm_judge auto 通道(真实诊断无金标,judge 评报告质量非诊断正确性),只走 👍 gold standard;rerank 不预先加(测到需要才加,带 delta)。"

---

## 附录 A:设计考量与演进项汇总

| 项 | 现状 | 演进 |
|---|---|---|
| trace_id 自排除 | 测试必需,生产两层不对称(症状避/根因留) | 已是设计,无需改 |
| payload 精简 | 7 字段冗余/死(category/source/is_cross_layer/symptom_tier/root_cause_tier/signal_types/hit_count) | 删 7 字段 + payload 索引;`affected_files` 加进注入(§5.2/§6.5) |
| hit_count 冗余 | 与 effectiveness 数学等价(都=👍计数) | 删,importance 砍到 `0.5·conf + 0.5·eff`(§6.1) |
| recency τ=90 | 默认值未标定 | 从 bug 复现数据标定 |
| importance 权重 0.5/0.5 | 手调 heuristic | 数据驱动标定/学习 |
| 反馈归因 | 已落地双路径(agent 引用 + case 级"有帮助",§8.1) | 非幂等 helpful 标记;因果靠 ablation |
| effectiveness 命名 | "有效性"暗示因果 | 重命名"认可关联分",因果靠 ablation |
| 冲突检测 + 多样性 | 冲突检测 detect-only(P1-c)+ 症状层 MMR(`_select_mmr_topk`,root_cause 向量 sim + λ,已实现) | λ 标定 / overfetch 调优库大时演进 |
| rerank | 未加 | 库大了 measured fallback(三因子分不开时) |
| semantic pattern | P1-b 未实现 | 同类 case LLM 反思提炼 |
| code fingerprint 周期校验 | 设计已定未实现 | 周期任务校验符号存在性 |

---

## 附录 B:tier hard filter reversal(查询侧不再按 tier 过滤)

**原设计**:`tier` 走 payload hard filter,意图精确匹配替代语义猜测 + 剪枝跨 tier 噪声降 token。

**撤销理由(correctness 优先于 token)**:
1. **静默 0 召回**:symptom 召回在 agent 诊断前、整链最前。`derive_tier` 误判(correlations 没抓到 -> 真 cross_layer 判单端;多数投票打平;无信号默认 backend)任一发生,hard filter 静默排除异 tier 同根因 case,无日志无降级。把"召回差"放大成"0 召回且不告警"。
2. **双向量收益无法归因**:hard filter 让跨 tier 同根因对的 symptom 路变"被 filter 拦"而非"被向量分不清",收益归因到 filter 还是根因向量说不清。去掉后干净归因到根因向量。
3. **token 杠杆不在此**:真杠杆是 root_cause 工具按需调(不调 0 token)+ 召回更准致 agent 少 flail,而非 tier filter 剪枝。

**撤销后**:tier 留 payload(索引端 `derive_tier` 标 `symptom_tier`,注入展示用),查询侧不 filter。跨 tier 异根因噪声由 **relevance 阈值标定(§9.1,0.60/0.61)+ 三因子(§6.1)+ 冲突检测(§7.2)** 接住。`derive_tier` 保留(索引侧 + eval + 单测用)。

**遗留**:阈值标定变关键(filter 原替阈值干一部分 tier 分离的活,现全压阈值)--已由 §9.1 标定完成。
