"""LangGraph 对话记忆持久化（checkpointer）

使用 AIOMySQLSaver 将对话状态按 thread_id 持久化到 MySQL，
实现跨轮次、跨进程重启的对话记忆。复用 settings.DATABASE_URL（mysql:// 格式）。"""
import aiomysql
from langgraph.checkpoint.mysql.aio import AIOMySQLSaver

from app.config import settings


_conn: aiomysql.Connection | None = None
_checkpointer: AIOMySQLSaver | None = None


async def init_checkpointer() -> AIOMySQLSaver:
  """初始化 checkpointer：建立 MySQL 连接 + 幂等建表。在 lifespan startup 调用。"""
  global _conn, _checkpointer
  if _checkpointer is not None:
    return _checkpointer
  # parse_conn_string 将 DATABASE_URL 解析为 aiomysql.connect 所需的关键字参数
  conn_params = AIOMySQLSaver.parse_conn_string(settings.DATABASE_URL)
  # autocommit=True 是 setup() 建表能够提交的必需条件（见 langgraph-checkpoint-mysql 文档）
  _conn = await aiomysql.connect(**conn_params, autocommit=True)
  _checkpointer = AIOMySQLSaver(conn=_conn)
  await _checkpointer.setup() # 幂等：创建 checkpoints/checkpoint_blobs/checkpoint_writes 表
  return _checkpointer


def get_checkpointer() -> AIOMySQLSaver:
  """获取 checkpointer 单例，供 LangGraph 编译图时使用。
   必须在 init_checkpointer() 之后调用。"""
  if _checkpointer is None:
    raise RuntimeError("Checkpointer 尚未初始化，请在应用启动时调用 init_checkpointer()")
  return _checkpointer


async def close_checkpointer() -> None:
  """关闭 MySQL 连接。在 lifespan shutdown 调用。"""
  global _conn, _checkpointer
  if _conn is not None:
    _conn.close()
  _conn = None
  _checkpointer = None