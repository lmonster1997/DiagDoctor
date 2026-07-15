"""长期记忆 RAG 子包。

基础设施（P0 AM1 已实现）：
- qdrant_client.py: Qdrant 客户端 + collection 创建/迁移
- embedding.py: bge-m3 embedding via TEI

业务逻辑（P0 PM1 已实现）：
- case_store.py: maybe_index_diagnosis() + passage 构造 + dedup
- case_retriever.py: search_historical_cases() + prompt 注入（P0 AM2 待实现）
"""
