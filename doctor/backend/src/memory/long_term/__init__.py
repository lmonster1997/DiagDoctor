"""长期记忆 RAG 子包。

基础设施（P0 AM1 已实现）：
- qdrant_client.py: Qdrant 客户端 + collection 创建/迁移
- embedding.py: bge-m3 embedding via TEI

业务逻辑（P0 PM1/AM2 已实现）：
- encoding.py: derive_tier + build_symptom_passage（索引/查询共享,召回/利用三分离 §4）
- case_store.py: maybe_index_diagnosis() + payload + dedup（写侧）
- case_retriever.py: search_historical_cases() + format_similar_cases()（读侧,§6）
"""
