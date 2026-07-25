"""长期记忆 RAG - 历史优质案例检索。

基础设施：
- qdrant_client.py: Qdrant 客户端 + historical_cases collection 生命周期
- embedding.py: bge-m3 embedding via TEI
- encoding.py: 索引/查询共享的症状编码（召回/利用三分离 §4）
- case_store.py: 优质案例入库 -> Qdrant ✅ P0 PM1 已实现
- case_retriever.py: RAG 检索相似历史 Bug ✅ P0 AM2 已实现（§6 三因子检索 + §6.5 注入）
"""
