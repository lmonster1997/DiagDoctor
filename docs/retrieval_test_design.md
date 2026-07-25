# 检索侧测试集设计(两层解耦:症状层 + 根因层)

> 定位:`docs/long_term_memory_design.md` §9(验证策略)的检索侧细化实现。
> 范围:**仅检索端**(确定性、可复现、主证据)。两层解耦:症状层只测症状召回/误召回,根因层只测根因召回/误召回,互不背锅。端到端 token/accuracy A/B 是后续单独立项,本文不覆盖。
> 状态:设计已定 + 生成器/验证脚本已实现(`gen_retrieval_test_cases.py` / `verify_retrieval_dual_vector.py`)。§3.5 假设鲁棒性循环待实现。

---

## 1. 要验证什么:两层各司其职,各自独立评估

双向量(symptom + root_cause 两个 named vector)**不是"一条路补救另一条路的天花板"**,而是**两层目标不同、互不替代**:

- **症状层**(symptom 向量):查症状相似。召回同症状的 case,给 LLM **多种根因、多种诊断思路**(广度)。症状像但根因不同 -> 仍召回,这是功能(发散思路),不是噪声。
- **根因层**(root_cause 向量):查根因相似。召回同根因的 case,**跨症状复用根因经验**(深度)。症状不同但根因同 -> 仍召回,这是功能(经验复用)。

**核心原则:两层彻底解耦,各自只对自己的职责负责,互不背锅。** 症状层只看症状像不像(不看根因);根因层只看根因像不像(不看症状)。每层用自己的标签标定自己的阈值、评估自己的召回/误召回。

### 1.1 四命题:两层各司其职的正常行为(非补救对子)

| 命题 | 症状层视角 | 根因层视角 |
|---|---|---|
| P1 同根因同症状 | 症状像->该召回 ✓ | 根因像->该召回 ✓ |
| P2 同根因异症状 | 症状不像->不召回(症状层正确,非缺陷) | 根因像->该召回 ✓(根因层价值:跨症状复用) |
| P3 异根因同症状 | 症状像->该召回 ✓(给 LLM 多根因思路,功能) | 根因不像->不召回(根因层正确) |
| P4 异根因异症状 | 症状不像->不召回 ✓ | 根因不像->不召回 ✓ |

**关键翻转(对早期设计的纠正)**:P2/P3 不再是"双向量补救 symptom 天花板"的对子,而是**两层各司其职的正常行为**。双向量价值不再是"symptom 路缺陷 root_cause 路补",而是**广度(症状层)+ 深度(根因层)正交,各有不可替代的价值**。这比"补救"讲法更诚实--不把症状路说成"有天花板要被补",而是"两路目标不同、互不替代"。

### 1.2 两层独立标定,两个独立阈值(§9.1 的实证落点)

两层衡量不同语义(症状文本 vs 根因文本),good/bad 分离点不同,**各自独立标定**:

- **症状层阈值** `SYMPTOM_RELEVANCE_THRESHOLD`:取"同症状 / 异症状"的 symptom-cosine 分布分离点。回答症状层召回率/误召回率。**与根因无关**(症状层不该用根因标签评估)。
- **根因层阈值** `ROOT_CAUSE_RELEVANCE_THRESHOLD`:取"同根因 / 异根因"的 root_cause-cosine 分布分离点。回答根因层召回率/误召回率。**与症状无关**。

代码已拆两个常量(`case_retriever.py` `SYMPTOM_RELEVANCE_THRESHOLD` / `ROOT_CAUSE_RELEVANCE_THRESHOLD`,**已标定 0.60 / 0.61**--DashScope qwen3.7-text-embedding 模型,本测试集首跑产物;gold-case 精化待 §9.1)。本测试集输出两个分布,标定两个分离点是其副产物。

> 症状层调参(阈值/overfetch/三因子)只能优化"症状召回 + 症状层面去噪",**碰不到根因**(三因子的 relevance 仍是 symptom cosine,无根因信号)。要"按根因排序"只有根因层能做到--这正是双向量存在的理由,而非在症状层 rerank 里硬塞根因(会回到混合相似度,违反 §4.1 三分离)。

> 前提:tier hard filter 已去掉(附录 B reversal)。否则 P2 跨 tier 对的 symptom 路是被 filter 拦的,归因不干净。去 filter 后,两层召回行为完全由各自向量 + 各自阈值决定,可归因。

---

## 2. 测试集:96 合成 case + leave-one-out + 独立测试库

### 2.1 为什么合成,不用 15 bug 金标

检索召回率是**统计量**,需要足量同根因/同症状/跨 tier 对才能画 cosine 分布、标定阈值。15 bug 金标太少(跨 tier 同根因仅 BE-022↔FE-021 一对,n=1 撑不起统计),且金标造一个成本高(要代码注入+证据链+可诊断性)。

检索测试只需**症状文本 + 根因文本 + 标签**,不需代码/证据链/agent 可诊断性--可批量造。故用合成数据,落盘 fixture,规模 ~100。

### 2.2 合成数据生成(`scripts/gen_retrieval_test_cases.py`)

**网格**:8 根因类型(n-plus-1/null-check/idor/missing-field/fk-violation/race-condition/config-error/silent-data-loss)× 6 症状类型(http-500/frontend-crash/slow-query/access-anomaly/silent-loss/intermittent)× 2 措辞 = **96 case**。

每个 case 精简 schema(不背 bug 金标包袱):`case_id` / `user_report`(症状文本) / `root_cause_summary`(根因文本) / `root_cause_type` / `symptom_type` / `tier` / `cross_tier`。

**防高估机制(关键)**:每个根因/症状类型配 3-4 套措辞模板,同格内两份随机选**不同措辞**。例 P1 对(null-check × http-500):症状"操作后页面直接报错,接口返回 500" vs "提交请求后服务器报内部错误,什么都没成功";根因"返回前直接读取可能为 null 的外键字段" vs "对象属性访问未防御空值"。同根同症但字面差很多 -> cosine 反映真实语义相似,非模板匹配。否则同类型对全 ~1.0,测的是模板识别,高估召回、阈值标定偏乐观。

**覆盖**(实测):P1=48 对 / P2=480 对 / P3=672 对 / P4=3360 对,**跨 tier case=32**(解决原 n=1 痛点,P2 变统计命题)。固定 seed=20260723 可复现,落盘 `tests/fixtures/retrieval_cases/cases.yaml`。

**标签即金标**:`root_cause_type` / `symptom_type` 是测试标签,评估时作 ground truth(检索测试本就测"按标签的召回")。

### 2.3 独立测试库(不污染开发库)

合成 case 灌入**独立 collection** `historical_cases_test`,与开发库 `historical_cases` 物理隔离。脚本 monkeypatch `case_store`/`case_retriever` 的 `COLLECTION_NAME` 指向 test 库,跑测重建即净,可复现。开发库零影响。

### 2.4 结构:leave-one-out

96 case 轮流当查询,其余 95 入库(模拟 👍 历史库)。每次查询跑两层:

- **症状层**:`search_historical_cases(query_evidence)` -- 用查询 case 的 `user_report`(原始症状文本,真实运行与 gold 同源,**无失真**)。
- **根因层**:`search_by_root_cause(root_cause_summary)` -- 用查询 case 的 `root_cause_summary` 当 hypothesis。

总查询数:96 × 2 层 = 192 次检索。自动覆盖全量对,无人为 seed 偏差。

> 与 `eval_recall_ablation.py` 的关系:后者是纯 in-memory cosine 矩阵,**不经过** overfetch/三因子/阈值/去重,回答向量层天花板。本测试集走真实 `search_*` 函数,捕捉真实管线行为(阈值挡/放的真实效果)。两者互补。

> **root_cause 层的合成根因仍是上界**。合成 case 的根因文本是干净、完整的"类 gold"描述,不是真实 agent 读完证据+搜完代码后 LLM 生成的残缺猜测。故根因层召回率是上界,真实 agent 假设措辞不同/不完整/部分错,召回率更低。合成数据的措辞多样性(§2.2)**部分**逼近了"措辞不同"这一失真,但不等于真实 agent 的残缺假设。最终真实值由端到端 agent 测(范围外)。

---

## 3. 断言:四命题的判定

### 3.1 P1 同根因同症状(基线正例)

合成网格里每个格子(同 root_cause_type + 同 symptom_type)的 2 份 case 互为 P1 对(共 48 对)。leave-one-out 自动覆盖。

- 症状层 top-3 含其同格对?**预期:是**(同症状,症状层该召回)。
- 根因层 top-3 含其同格对?**预期:是**(同根因,根因层该召回)。

**指标**:P1 两层各自召回率@3,期望 ≥ 0.8。基线:确认两层在"该召回"时行为正确。

### 3.2 P2 同根因异症状(根因层价值:跨症状复用,统计命题)

合成数据有 480 对同根因异症状(含 32 跨 tier case)。P2 从原 n=1 机制深挖升级为**统计命题**:leave-one-out 跑完,统计根因层对 P2 对的召回率。

- **症状层**:同根因异症状对召回率?**预期:低**(症状不像,symptom cosine < 症状阈值)。症状层正确不召--症状不像就不召回,非缺陷。
- **根因层**:同根因异症状对召回率?**预期:高**(根因文本相似,root_cause cosine ≥ 根因阈值)。根因层价值:症状不同也能跨症状复用同根因经验。

**判定**:症状层 P2 召回低(症状层正确) AND 根因层 P2 召回高(跨症状复用) = 双向量职责分工成立。跨 tier 子集(32 case)单独统计,验证去 tier filter 后跨 tier 同根因不再被拦(归因到向量)。

> **上界提醒**:根因层用合成根因(类 gold 干净文本),召回率是上界。合成数据的措辞多样性(§2.2)部分逼近"措辞不同"失真,但不等于真实 agent 残缺假设。完整失真逼近见 §3.5。

### 3.3 P3 异根因同症状(症状层价值:多根因思路)

合成数据有 672 对异根因同症状(同 symptom_type 跨 root_cause_type)。轮流当查询:

- **症状层**:异根因同症状对召回率?**预期:高**(症状文本相似,症状层该召回)。**这是症状层功能**--召回同症状多根因 case,给 LLM 多种诊断思路(发散),不是"过召污染"。
- **根因层**:异根因同症状对召回率?**预期:低**(根因文本不同,root_cause cosine < 根因阈值)。根因层正确不召异根因。

**指标**:P3 两层各自召回率@3 -- 症状层(期望高,多根因思路)vs 根因层(期望低,只同根因)。两层差值体现"症状层广度 vs 根因层深度"职责分工。

### 3.4 P4 异根因异症状(基线负例)

合成数据有 3360 对异根因异症状(不同 root_cause_type + 不同 symptom_type)。leave-one-out 自然产生。

- 两层都该不召回(症状和根因都不像)。
- **指标**:P4 两层各自误召率@3,期望低。sanity check,非主证据。

### 3.5 假设鲁棒性(逼近真实 agent 假设的失真,补 gold 上界与真实值之间的空档)

§3.1-3.4 的根因层用合成根因(类 gold 干净文本),召回率是**上界**。真实 agent 生成的 hypothesis 会失真:措辞不同、不完整、部分错。合成数据的措辞多样性(§2.2)已**部分**逼近"措辞不同"这一失真,但不覆盖"不完整/部分错"。本节用**受控文本扰动**补足:对合成根因做扰动当 hypothesis,测根因层对假设失真的容忍度。仍不跑 agent/不调 LLM/不搜代码,确定性可复现。

**只测根因层**(症状层查询输入是 `user_report`,不经 agent 假设,无此失真层)。

**扰动类型**(对每个同根因对的查询 case,造一组扰动 hypothesis):

| 扰动 | 构造 | 测什么 | 期望 |
|---|---|---|---|
| **措辞改写** | 同义改写合成根因(如"N+1 查询:list_tasks 逐条查 comments"->"循环里对每个任务单独查数据库评论") | 语义鲁棒性:同义不同字,向量还认不认 | 召回仍在(cosine ≥ 阈值) |
| **不完整假设** | 只给根因一半,省细节(如"assignee_id 没判空",省掉 .hex/.slice 机制) | agent 假设粗略时还行不行 | 召回大概率降(cosine 降),记录降幅 |
| **部分错假设** | 方向对、机制错一点(如把"FK 约束违反"说成"唯一约束违反") | 噪声容忍 | 召回降或不召,记录 |

**指标**:每种扰动下,根因层对同根因对的召回率@3(对比 §3.1/3.2 的上界)。**降幅 = 假设失真对检索的侵蚀量**。

**判定**:
- 措辞改写仍召回 -> 根因向量语义鲁棒,agent 措辞自由度有保障(强结论)。
- 不完整/部分错假设召回掉得多 -> 根因层对假设质量敏感,真实 agent 假设不够好时收益打折(诚实结论,提示 agent 假设质量是端到端关键变量)。

> 扰动 hypothesis **人工构造**,不调 LLM 生成--保持确定性。合成数据有 8 根因类型,每类抽 1-2 对同根因对造 3 种扰动,共 ~8-16 对 × 3 ≈ 24-48 条扰动 hypothesis。扰动 case 可内嵌脚本或独立 fixture(待实现)。

---

## 4. 副产物:两层 relevance 阈值标定(§9.1)

leave-one-out 跑完,**两层分别记录 cosine + 各自的标签**,输出**四个分布、两个分离点**:

**症状层**(用症状标签,与根因无关):
- 同症状对的 symptom-cosine 分布(P1/P3 的正样本--症状像)。
- 异症状对的 symptom-cosine 分布(P2/P4 的负样本--症状不像)。
- 分离点 = `SYMPTOM_RELEVANCE_THRESHOLD` 标定值。

**根因层**(用根因标签,与症状无关):
- 同根因对的 root_cause-cosine 分布(P1/P2 的正样本--根因像)。
- 异根因对的 root_cause-cosine 分布(P3/P4 的负样本--根因不像)。
- 分离点 = `ROOT_CAUSE_RELEVANCE_THRESHOLD` 标定值。

两层分离点**几乎必然不同**(症状文本 vs 根因文本语义不同),这正是拆两个常量的理由。本测试集不强求一次标定到位,但**输出四个分布 + 两个候选分离点**,作为 §9.1 的实证数据。标定后回填 `case_retriever.SYMPTOM_RELEVANCE_THRESHOLD` / `ROOT_CAUSE_RELEVANCE_THRESHOLD` + 更新 `test_relevance_thresholds_are_calibrated_placeholders` 断言。

> 这是去 tier filter 后的关键收尾(附录 B 遗留):filter 原来替阈值干了一部分 tier 分离的活,现在全压到阈值上,标定变必须。两层各自标定,互不背锅。

---

## 5. 实现脚本

**数据生成**(已实现):`doctor/backend/scripts/gen_retrieval_test_cases.py`
- 8×6×2 网格生成 96 合成 case,落盘 `tests/fixtures/retrieval_cases/cases.yaml`。
- 措辞多样性(3-4 套模板随机选),固定 seed=20260723 可复现。

**检索验证**(已实现):`doctor/backend/scripts/verify_retrieval_dual_vector.py`,仿 `verify_feedback_loop.py` 模式(真实 Qdrant + 本地 bge-m3,非 mock):
- 环境前置:`BGE_M3_LOCAL_PATH` + `HF_HUB_OFFLINE=1`(TEI 未起,走本地)。
- **隔离**:monkeypatch `case_store`/`case_retriever` 的 `COLLECTION_NAME` -> `historical_cases_test`,开发库零影响。每轮重建 test collection。
- **走 doctor 自己的检索代码**:`maybe_index_diagnosis` / `search_historical_cases` / `search_by_root_cause` -- 脚本不重写检索逻辑,只造数据+喂查询+按标签评估。验的是真实管线(overfetch/trace 去重/三因子/阈值/top-k)。
- leave-one-out 主循环:重建 test collection -> 入库 95 -> 查询 1 跑两层 -> 记录。
- 四命题判定 + 打印(P1-P4 两层 recall@k 对比)。
- **§3.5 假设鲁棒性循环**(待实现):对同根因对跑合成根因 + 3 种扰动 hypothesis 的根因层,对比召回降幅。
- cosine 分布输出(四分布 + 两候选分离点,标定两阈值)。
- force utf-8 stdout(Windows GBK)。

**确定性选择**(用户决策):
- `created_at` 不造时间差 -> recency 因子常数 1.0(recency 确定性,不在本次验证范围)。
- `confidence` 全部统一(0.8) -> importance = 0.5·conf 常数,三因子退化为纯 relevance 排序。本次只验**阈值 + 双向量 + relevance 管线**,不验 recency/importance 排序(那需时间/反馈闭环数据,冷启动库没有)。
- `hit_count`/`effectiveness` = 0(没跑 👍 闭环)-> importance 里这两个分量本就死,如实反映冷启动库。

### 5.1 运行

**前置**:Qdrant 在跑(`http://127.0.0.1:6333`,健康检查 `curl http://127.0.0.1:6333/healthz`);`.env` 配好 DashScope(`EMBEDDING_BASE_URL` + `DASHSCOPE_API_KEY` + `EMBEDDING_MODEL=qwen3.7-text-embedding`)。Loki/Tempo/Doctor API **不需要起**(纯检索层,不跑 agent)。

**1. 生成合成 case fixture**(已生成,一般不用重跑;改网格/措辞时重跑):

```bash
cd doctor/backend
uv run python scripts/gen_retrieval_test_cases.py            # 写 tests/fixtures/retrieval_cases/cases.yaml
uv run python scripts/gen_retrieval_test_cases.py --seed 42  # 换 seed
uv run python scripts/gen_retrieval_test_cases.py --stdout   # 只打印不写盘
```

**2. 跑检索验证**(leave-one-out,索引 96 一次 + 96×2 查询,DashScope API embed 脚本内缓存去重 ~192 次唯一调用,< 1 分钟):

```bash
cd doctor/backend
PYTHONIOENCODING=utf-8 uv run python scripts/verify_retrieval_dual_vector.py
```

- **embed 路径**:`.env` 配了 `EMBEDDING_BASE_URL` + `DASHSCOPE_API_KEY` -> `embedding.py` 走 DashScope API(不回退,防混模型)。脚本加文本 keyed 缓存(同文本同向量,9120 -> ~192 次唯一 API 调用,省钱省时)。
- **标定模式**:脚本 monkeypatch `threshold=0` + `OVERFETCH=100`,收集全 95 候选的 cosine 分布(占位阈值 0.75 会把 v4 候选全滤掉--v4 cosine 分布偏低)。recall@3 = 全排序列表 top-3 切片(原始排序质量,与阈值解耦)。
- `PYTHONIOENCODING=utf-8`:Windows GBK 控制台输出 ✓/✗/中文会乱码,强制 utf-8(脚本内也 `reconfigure` 了,双保险)。

**产物**(stdout 打印):
- 四命题评估表:P1-P4 两层各自 recall@3 + 期望对比。
- 层聚合指标:Precision@k / HitRate@k / MRR / 同标签 Recall@k / 噪声占比(误召)--回答"层召回准不准、首个同标签排多前、top-k 有没有噪声"。同标签 Recall@k 是 k-bound(上限 k/同标签总数),不是质量问题;增 k 或 rerank 可提。
- 阈值标定:四 cosine 分布(同/异症状 × 同/异根因)+ 两候选分离点(标定:症状 0.60 / 根因 0.61,已回填 `SYMPTOM_/ROOT_CAUSE_RELEVANCE_THRESHOLD`)。
- 跑完按候选分离点回填 `case_retriever.py` 两常量 + 更新 `test_relevance_thresholds_calibrated` 断言。

**隔离保证**:脚本全程操作 `historical_cases_test`,开发库 `historical_cases` 零影响(monkeypatch `COLLECTION_NAME`,跑完 test 库可留可删,不影响开发)。

## 6. 边界与诚实声明

- **P2 已非 n=1**:合成数据 32 跨 tier case + 480 同根因异症状对,P2 升级为统计命题。原 15 金标 n=1 痛点已解决。
- **根因层合成根因是上界**:合成根因是类 gold 干净文本,召回率偏乐观,非真实 agent 值。措辞多样性(§2.2)部分逼近"措辞不同",§3.5 扰动补"不完整/部分错",最终真实值由端到端 agent 测(范围外)。检索层只回答"向量+管线行不行",不背 agent 假设质量的锅(正交,混测污染归因)。
- **不测 token/accuracy**:纯检索层,不跑 agent。token/accuracy 是端到端 A/B,后续立项。
- **不验 recency/importance 排序**:confidence 统一+无时间差+无 👍 闭环,三因子退化为纯 relevance。recency/importance 的区分作用需另造数据(时间差/反馈),不在本次。诚实标注。
- **合成数据措辞多样性**:生成器用 `templates × entities` 池(每根因类型 3 模板×4 实体=12,每症状类型 4×4=16),跨同类型所有 case **不放回抽样**,保证 96 case 的 root_cause_summary 和 user_report **全唯一**--无 cosine≈1.0 人工簇,cosine 反映真实语义相似(同类型均值 0.69-0.71,非 1.0)。
- **recall@3 的 k-bound(重要解读)**:同标签 Recall@3 = 0.20(症状)/ 0.27(根因),看似低,实为 k 上限(3 槽 / 同标签 15 或 11 个,3/15=0.2)。**不是质量问题**--Precision@3≈1.0、MRR=1.0、HitRate=1.0 证明 top-3 几乎全同标签且首个就同标签,检索质量极好。要召回更多同标签 case,增 k 或加 rerank(两阶段检索,面试点)。早期"3-4 套措辞覆盖 12 case 致重复(cosine=1.0)挤掉 twin、P1 仅 0.02-0.08"的失真问题已由全唯一数据修复,P1 升至 0.19/0.33。
- **§3.5 扰动人工构造**:不调 LLM 生成,保确定性。代价是扰动覆盖度受人工设计限制,不穷尽真实 agent 假设分布。
- **标签即金标**:`root_cause_type`/`symptom_type` 是人工分类的测试标签,评估以此为 ground truth(检索测试本就测"按标签召回")。

---

## 7. 实测结果(2026-07-25,v4 首跑 + 唯一数据)

> **当前指标(post-MMR,含 operational 模式 + 多样性)统一在 `docs/memory_system_metrics.md`**。本节为 v4 首跑(pre-MMR)记录,留作对比;数字以 metrics 文档为准。

> 环境:DashScope `qwen3.7-text-embedding`(1024 维),96 合成 case 全唯一文本(§2.2 `templates×entities`),leave-one-out + 脚本内 embed 缓存。标定模式(`threshold=0` + `OVERFETCH=100`)收集全 95 候选;recall@3 = 全排序列表 top-3 切片。

### 7.1 四命题 recall@3

| 层 | P1 同根同症 | P2 同根异症 | P3 异根同症 | P4 异根异症 |
|---|---|---|---|---|
| 症状层 | 0.19 | 0.00 | 0.20 | 0.00 |
| 根因层 | 0.33 | 0.26 | 0.00 | 0.00 |

异标签(P2 症状 / P3 根因 / P4)该低的都 0 或近 0 ✓。P1(twin 进 top-3)0.19/0.33--数据全唯一后 twin 不再被精确重复挤掉(首跑重复数据时仅 0.02/0.08)。

### 7.2 层聚合指标(k=3)

| 层 | Precision@3 | HitRate@3 | MRR | 同标签 Recall@3 | 噪声占比 |
|---|---|---|---|---|---|
| 症状层 | 1.000 | 1.000 | 1.000 | 0.200 | 0.000 |
| 根因层 | 0.990 | 1.000 | 1.000 | 0.270 | 0.010 |

**主证据**:Precision@3≈1.0 + MRR=1.0 + HitRate=1.0 -- top-3 几乎全同标签,且**首个结果就总是同标签**,零噪声。双向量检索质量极好。

同标签 Recall@3 = 0.20/0.27 是 **k-bound**(3 槽 / 同标签 15 或 11 个,3/15=0.2),非质量问题。要召回更多同标签 case -> 增 k 或加 rerank(measured fallback,见 §6)。

### 7.3 阈值标定(四 cosine 分布)

| 层 | 同标签 n / min / mean / median / max | 异标签 n / min / mean / median / max | 候选分离点 | 回填阈值 |
|---|---|---|---|---|
| 症状层 | 1440 / 0.432 / 0.712 / 0.703 / 0.950 | 7680 / 0.238 / 0.437 / 0.431 / 0.761 | 0.597 | 0.60 |
| 根因层 | 1056 / 0.426 / 0.690 / 0.680 / 0.939 | 8064 / 0.204 / 0.419 / 0.411 / 0.805 | 0.616 | 0.61 |

同/异标签均值清晰分离(症状 0.71 vs 0.44,根因 0.69 vs 0.42)。分离点 0.597/0.616 与回填值 0.60/0.61 一致。分布有重叠(同 min < 异 max),但 Precision@3≈1.0 证明在 top-k 操作点重叠不造成噪声(同标签 cosine 仍压过异标签)。

### 7.4 结论

- 双向量两层职责成立:症状层召回同症状(P1+P3,广度),根因层召回同根因(P1+P2,跨症状复用深度),互不背锅。
- 检索质量极好(Precision/MRR/HitRate 均≈1.0),阈值 0.60/0.61 是有效操作点。
- 同标签 Recall@3 受 k 限制,非缺陷;rerank 作为后续 measured fallback(不现在加,违背"勿过度工程")。
- 诚实上界:合成根因是类 gold 干净文本,真实 agent 残缺假设下召回更低,由端到端测(范围外)。
