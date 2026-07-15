# -*- coding: utf-8 -*-
"""一次性修补 history_gold_bugs.md 的两处含全角破折号/箭头的段落。
Python 按 UTF-8 读取，用 \\u 转义精确匹配 U+2014/U+2192。失败不写盘。"""
import sys

PATH = "docs/history_gold_bugs.md"

# 每项: (label, old, new)
REPLACEMENTS = []

# ── 1. §3.2 标题行 -> 对称原则引子 ──
REPLACEMENTS.append((
    "3.2 header",
    '**索引端 passage 构造**（重点：加结构化锚，避免 root_cause 太短导致 embedding 飘）：',
    '**关键原则：索引/查询 passage 对称，只含"查询时刻可得的字段"**\n\n'
    '匹配空间里只能放查询端（诊断前）也拿得到的字段。`root_cause` / `fix_suggestion` / '
    '`primary_category` / `affected_files` 都是诊断**输出**，查询时刻未知；若塞进 embedding，'
    '查询向量用不上这一维，反而会把"症状相似但根因/类别不同"的同类 case 推开、降低召回'
    '（bug 诊断里很常见：页面卡死可能是 N+1 / 死循环 / 死锁）。其中 `fix_suggestion` 是代码片段，'
    '塞进 NL embedding 模型更是最弱项。因此这些字段**只进 payload、不进向量**，注入时从 payload 取出拼 prompt。\n\n'
    '索引端与查询端共用同一段 passage（仅含 evidence 可得字段，见 §6.1）：',
))

# ── 2. §3.2 passage 模板 -> 对称（去掉 类别/涉及文件/root_cause/fix）──
REPLACEMENTS.append((
    "3.2 passage template",
    '[诊断元数据] 信号类型: {逗号分隔 evidence.golden_signals[].signal_type} | 类别: {primary_category} | 层级: {symptom_tier} | 涉及文件: {affected_files}\n\n'
    '{user_report}\n\n'
    '{root_cause}\n\n'
    '{fix_suggestion 全文}',
    '[诊断元数据] 信号类型: {逗号分隔 evidence.golden_signals[].signal_type} | 层级: {evidence.correlations ? "cross_layer" : 单端 tier}\n\n'
    '{user_report}',
))

# ── 3. §3.2 解释段尾部（破折号在"确保"之前，匹配其后的纯文本）+ tier 一致性 note ──
REPLACEMENTS.append((
    "3.2 explanation tail",
    '确保同类 case 的信号类型/类别能参与相似度匹配。`fix_suggestion` 保留全文不截断'
    '（含代码片段如字段名/函数签名，截断可能丢失关键语义；bge-m3 支持 8192 token，全文 500 字完全够用）。'
    '结构化字段**同时**进 Qdrant payload（见下节），双路保险。',
    '确保同类 case 的信号类型能参与相似度匹配（category 已移出向量、只进 payload）。'
    '`root_cause`/`fix_suggestion` 移出后 passage 偏短，可选地在末尾追加 **signal 摘要**'
    '（每个 golden_signal 的简短描述/content）补足语义密度。'
    '结构化字段**同时**进 Qdrant payload（见下节），双路保险。\n\n'
    '> 📌 层级（tier）解析两端必须一致：当前索引端用 `report.symptom_tier`、查询端默认 `"backend"`，'
    '实现时统一为同一来源（建议都从 evidence 推导：有 `correlations` 即 `cross_layer`，否则取 evidence 的单端 tier）。',
))

# ── 4. §3.3 整块（标题+intro+PointStruct+陈旧性 note）──
old_33 = '''### 3.3 Payload 字段

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

---'''
new_33 = '''### 3.3 Payload 字段

诊断输出字段（root_cause / fix_suggestion / category / affected_files）现在**只存 payload**（不进向量），注入时从这里取，故 fix_suggestion 存全文而非片段：

```python
PointStruct(
    id=run_id,  # = thread_id，per-session 幂等 upsert
    vector=embedding,
    payload={
        "trace_id": trace_id,                 # 去重用，取自 evidence.trigger_trace_ids[0]
        "case_id": run_id,
        "category": report.primary_category,   # "performance"/"frontend_crash"/...（payload，不进向量）
        "symptom_tier": report.symptom_tier,    # "frontend"/"backend"/"cross_layer"
        "signal_types": [s.signal_type for s in evidence.golden_signals],
        "affected_files": _resolve_affected_files(report),  # report.affected_file 是单数 str|None，包成 list
        "root_cause": report.root_cause,        # 全文（注入用）
        "fix_suggestion": report.fix_suggestion, # 全文（注入用，含代码片段）
        "confidence": report.confidence,
        "source": "user_upvote",                # P0 只有这一种
        "created_at": isoformat,
        # 陈旧性预留（P0 不填，P1 见 §11）
        # "code_fingerprint": "<affected_file 符号/hash>",  # 用于剔除引用符号已不存在的过期案例
    }
)
```

> 📌 `affected_files` 留作 P1 陈旧性检测的锚点：检索/清理时可核对引用的文件与符号是否仍存在于当前代码库。P0 schema 先留位，避免日后改 payload 要重建 collection。

---'''
REPLACEMENTS.append(("3.3 block", old_33, new_33))

# ── 5. §7 maybe_index 块（含箭头 U+2192）──
old_7 = '''async def maybe_index_diagnosis(
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

    await client.upsert(collection_name="historical_cases", points=[point])'''
new_7 = '''async def maybe_index_diagnosis(
    report: DiagnosisReport,
    evidence: NormalizedEvidence,
    *,
    source: Literal["user_upvote"] = "user_upvote",
    trace_id: str = "",
    case_id: str = "",   # = run_id(thread_id)，作 point id
) -> bool:
    # 硬拦截：报告完整性
    if not (report.root_cause and report.affected_file and report.fix_suggestion):
        return False

    passage = _build_passage_text(evidence)   # 见 §3.2（索引/查询对称，不含 root_cause/fix）
    vector = (await embed_texts([passage]))[0]
    point = _build_point(report, evidence, vector, source, case_id, trace_id)

    # 同 trace_id 重复写入仅告警，不拦截（P0；P1 升级三元组去重）
    if trace_id and await _dedup_exists(trace_id=trace_id):
        logger.info("duplicate_trace_id_upvote", trace_id=trace_id)

    await client.upsert(collection_name="historical_cases", points=[point])
    return True'''
REPLACEMENTS.append(("7 maybe_index", old_7, new_7))


def main() -> int:
    with open(PATH, encoding="utf-8") as f:
        content = f.read()

    for label, old, new in REPLACEMENTS:
        n = content.count(old)
        if n != 1:
            print(f"FAIL [{label}]: expected 1 match, found {n}")
            return 1
        content = content.replace(old, new)
        print(f"ok   [{label}]")

    with open(PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print("written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
