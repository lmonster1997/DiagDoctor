"""长期记忆 RAG — 历史优质案例检索。

基础设施：
- qdrant_client.py: Qdrant 客户端 + historical_cases collection 生命周期
- embedding.py: bge-m3 embedding via TEI
- case_store.py: 优质案例入库 → Qdrant ✅ P0 PM1 已实现
- case_retriever.py: RAG 检索相似历史 Bug（P0 AM2 待实现）
"""
