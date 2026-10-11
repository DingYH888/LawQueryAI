"""测试 LangGraph 法律咨询工作流"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.db.database import init_db, close_db
from app.llm.checkpoint import init_checkpointer, close_checkpointer
from app.rag.vector_store import init_vector_store
from app.agents.workflow import workflow


async def test():
  # 按应用启动顺序初始化依赖：数据库 → checkpointer → 向量库
  await init_db()
  await init_checkpointer()
  await init_vector_store()
  try:
    # run(query, thread_id)：thread_id 即会话 ID，相同 ID 可恢复历史对话
    result = await workflow.run("劳动合同", thread_id="test-session-1")
    print(result["response"])
  finally:
    await close_checkpointer()
    await close_db()


asyncio.run(test())