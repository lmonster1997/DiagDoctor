# 记忆系统测试记录(检索召回 + 阈值标定 + 多样性)

> 记忆系统**所有测试结果的单一真相源**。`docs/memory_system_design.md` §9(验证策略)与 `docs/retrieval_test_design.md` §7(v4 首跑记录)指向此处,避免散落重复。
> 脚本:`doctor/backend/scripts/verify_retrieval_dual_vector.py`(跑法见 `retrieval_test_design.md` §5,需 Qdrant + DashScope `.env`)。
> 含 **pre-MMR 基线**(v4 首跑)与 **post-MMR**(完整 MMR 向量+λ)两轮结果,可对比 MMR 对召回的影响。

---

## 0. 环境与测试集

- **Embedder**:DashScope `qwen3.7-text-embedding`(1024 维)。
- **测试集**:96 合成 case(`tests/fixtures/retrieval_cases/cases.yaml`),8 根因类型 × 6 症状类型 × 2 措辞,全唯一文本(无 cosine≈1.0 人工簇),leave-one-out。标签即金标(`root_cause_type` / `symptom_type`)。
- **隔离**:独立 test 库 `historical_cases_test`,与开发库 `historical_cases` 物理隔离(monkeypatch `COLLECTION_NAME`)。
- **确定性**:`confidence` 统一 0.8 -> importance 常数,三因子退化为纯 relevance。**recency/importance 不在本次验证范围**(见 §5)。
- **两轮**:
  - **pre-MMR 基线**(2026-07-25 v4 首跑):症状层纯 score top-k。
  - **post-MMR**(2026-07-25):症状层完整 MMR(`_select_mmr_topk`,λ=0.5)。
- **两种测量模式**(post-MMR 轮新增 operational):
  - **标定模式**(`threshold=0` + `OVERFETCH=100`):收集全 95 候选 cosine 分布标定阈值;`recall@3` = 全排序列表 top-3 切片(原始排序质量,与阈值解耦)。
  - **operational 模式**(真实阈值 0.60/0.61 + `OVERFETCH=10` + `k=3`):**生产操作点**,post-MMR 质量 + 多样性。标定模式下 MMR 跑全 95 池(含被生产阈值滤掉的 diff-symptom 候选)会 under-state precision / over-state diversity,operational 才准。

---

## 1. 检索质量(operational, post-MMR,生产操作点)

| config | Precision@3 | HitRate@3 | MRR | distinct-roots@3 |
|---|---|---|---|---|
| **症状层 MMR (λ=0.5)** | 0.997 | 1.000 | 1.000 | **2.948** |
| 症状层 pure (λ=0,pre-MMR baseline) | 1.000 | 1.000 | 1.000 | 2.750 |
| 根因层(无 MMR) | 0.990 | 1.000 | 1.000 | 1.031 |

- 同标签:症状层 = 同 `symptom_type`(广度职责);根因层 = 同 `root_cause_type`(深度职责)。
- **Precision@3≈1.0 / MRR=1.0 / HitRate=1.0**:top-3 几乎全同标签,首个结果就同标签,零噪声--检索质量极好。
- 根因层 distinct-roots@3=1.031:深度层召回同根因(≈1 种根因),符合职责(广度层才要多根因)。

---

## 2. 多样性(MMR 效果,症状层)

症状层 MMR(λ=0.5)vs pure(λ=0,MMR 退化为纯 score = pre-MMR 行为)对比:

| 指标 | pure (λ=0) | MMR (λ=0.5) | Δ |
|---|---|---|---|
| distinct-roots@3 | 2.750 | **2.948** | **+0.198**(多样性↑) |
| Precision@3 | 1.000 | 0.997 | −0.003(≈持平) |

- `distinct-roots@3` = top-3 覆盖几种不同 `root_cause_type`(max=3)。MMR 把它从 2.75 推到 **2.95(近最大值)**。
- **代价可忽略**:Precision@3 仅降 0.003(96 查询 × 3 槽 = 288 槽里 1 个槽被换成 diff-symptom)。MRR/HitRate 不变(MMR 第一个选最高分,与 pure 一致;只后续位用 root_cause 向量冗余换多样)。
- **诚实解读**:pure 已达 2.75/3,因为合成数据的同症状组本身跨多根因(P3 异根因同症状 672 对)。MMR 的边际收益是把"本来就有多样"推到"近最大多样",不是把"无多样"变"有多样"。在更同质的生产库里(pure top-3 易扎堆同根因),MMR 的 Δ 会更大。
- 设计/实现见 `memory_system_design.md` §7.2(`_select_mmr_topk`,root_cause 向量 sim + λ 贪心);λ=0.5 未标定(无 gold 多样性数据,与 τ=90 / importance 0.5/0.5 同属手调 knob)。

---

## 3. 阈值标定(标定模式,MMR 无关)

四 cosine 分布(leave-one-out 全 95 候选,n=9120=96×95)+ 候选分离点。**MMR 不影响此项**(分布采全集,不碰 top-3 选择),pre/post-MMR 两轮一致。

| 层 | 同标签 n / min / mean / median / max | 异标签 n / min / mean / median / max | 候选分离点 | 回填阈值 |
|---|---|---|---|---|
| 症状层 | 1440 / 0.432 / 0.712 / 0.703 / 0.950 | 7680 / 0.238 / 0.437 / 0.431 / 0.761 | 0.597 | **0.60** |
| 根因层 | 1056 / 0.426 / 0.690 / 0.679 / 0.939 | 8064 / 0.204 / 0.419 / 0.411 / 0.805 | 0.616 | **0.61** |

同/异标签均值清晰分离(症状 0.71 vs 0.44,根因 0.69 vs 0.42)。分布有重叠(同 min < 异 max),但 §1 Precision@3≈1.0 证明在 top-k 操作点重叠不造成噪声(同标签 cosine 仍压过异标签)。

---

## 4. 四命题 recall@3 + 层聚合(标定模式,pre vs post-MMR 对比)

### 4.1 四命题 recall@3(leave-one-out,标定模式 top-3 切片)

| 层 | 轮次 | P1 同根同症 | P2 同根异症 | P3 异根同症 | P4 异根异症 |
|---|---|---|---|---|---|
| 症状层 | pre-MMR | 0.19 | 0.00 | 0.20 | 0.00 |
| 症状层 | post-MMR | 0.22 | 0.00 | 0.20 | 0.00 |
| 根因层 | pre-MMR | 0.33 | 0.26 | 0.00 | 0.00 |
| 根因层 | post-MMR | 0.33 | 0.26 | 0.00 | 0.00 |

- 症状层:P1(同症该召回)/ P3(异根同症,多根因思路)高;P2/P4(异症不召)0.00 ✓。
- 根因层:P1(同根该召回)/ P2(同根跨症状复用)高;P3/P4(异根不召)0.00 ✓。
- **双向量职责分工成立**:症状层广度(P1+P3)、根因层深度(P1+P2),互不背锅。
- **MMR 对 recall@3 影响微小**:症状层 P1 0.19->0.22(MMR 重排使同根同症 twin 略易进 top-3),其余不变;根因层完全不变(无 MMR)。

### 4.2 层聚合指标(标定模式,k=3)

| 层 | 轮次 | Precision@3 | HitRate@3 | MRR | 同标签 Recall@3 | 噪声占比 |
|---|---|---|---|---|---|---|
| 症状层 | pre-MMR | 1.000 | 1.000 | 1.000 | 0.200 | 0.000 |
| 症状层 | post-MMR | 0.997 | 1.000 | 1.000 | 0.199 | 0.003 |
| 根因层 | pre-MMR | 0.990 | 1.000 | 1.000 | 0.270 | 0.010 |
| 根因层 | post-MMR | 0.990 | 1.000 | 1.000 | 0.270 | 0.010 |

- **MMR 代价 = Precision@3 1.000 -> 0.997(症状层)**,与 §1 operational 一致;根因层不变。MRR/HitRate 不受影响。
- 同标签 Recall@3 = 0.20/0.27 是 **k-bound**(3 槽 / 同标签 15 或 11 个,3/15=0.2),非质量缺陷;增 k 或 rerank 可提。

---

## 5. 未测项目录(诚实)

| 项 | 为什么没测 | 需要什么 |
|---|---|---|
| **recency 排序** | 测试集 `created_at` 无时间差 -> recency 常数 1.0 | 造时间差 `created_at` 分布 |
| **importance 排序** | `confidence` 统一 + 无 👍闭环 -> importance 常数 | 👍 闭环写 effectiveness 数据 |
| **effectiveness 因果** | 在线观测不到反事实 | 离线 ablation:同 bug 有/无 case X 对比诊断质量 |
| **token/accuracy** | 纯检索层,不跑 agent | 端到端 A/B(另立项) |
| **§3.5 假设扰动鲁棒性** | 设计已定,扰动 fixture 未实现 | 受控文本扰动 hypothesis(措辞改写/不完整/部分错) |
| **合成上界** | 根因层用类 gold 干净文本 | 真实 agent 残缺假设下召回更低,由端到端测 |

> recency/importance/effectiveness/token 均需生产数据或端到端,不在检索层验证范围。诚实标注,不假装测过。

---

## 6. 结论 + 诚实声明

- **双向量两层职责成立**:症状层广度(P1+P3 同症状多根因)、根因层深度(P1+P2 同根因跨症状),Precision@3≈1.0 / MRR=1.0。
- **MMR 有效**:症状层 top-3 distinct-roots 2.75 -> 2.95(近最大多样性),Precision 代价 −0.003(可忽略),MRR/HitRate 不变。设计见 §7.2。
- **MMR 对召回率影响微小**:P1-P4 recall@3 基本不变(症状 P1 0.19->0.22),根因层完全不变。
- **阈值有效**:0.60/0.61 是干净操作点(同/异标签均值清晰分离,重叠不造成 top-k 噪声),MMR 无关。
- **诚实上界**:合成根因是类 gold 干净文本,真实 agent 残缺假设下根因层召回更低(由端到端测);合成数据措辞多样性部分逼近"措辞不同"失真,不等于真实 agent 假设分布。
- **同标签 Recall@3 受 k 限制**(3/15=0.2),非缺陷;rerank 作为库大时的 measured fallback(演进项,见 design 附录 A)。
