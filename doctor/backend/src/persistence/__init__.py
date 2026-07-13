"""状态持久化 — LangGraph Checkpoint 封装。

（待实现）
- checkpoint_store.py: LangGraph checkpointer 封装 (SqliteSaver/PostgresSaver)
- session_manager.py: 会话管理 (thread_id ↔ checkpoint)
- resume_handler.py: 恢复逻辑（加载 checkpoint → 继续 ReAct 循环）
"""
