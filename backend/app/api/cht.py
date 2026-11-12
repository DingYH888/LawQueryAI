"""聊天 API 路由 — 支持流式响应，会话按用户隔离
对话记忆由 LangGraph 的 AIOMySQLSaver（checkpoint）持久化，
按 session_id（= thread_id）跨轮次/跨进程恢复；ChatMessageRecord 供侧边栏 UI。"""
import uuid
import json
import asyncio
import contextlib
import logging
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

from app.agents.workflow import workflow
from app.dependencies import get_current_user
from app.models.chat import ChatSession, ChatMessageRecord
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

# SSE 心跳间隔（秒）：首 token 实测可达 80s+，长时间无数据会让浏览器/代理判定空闲
HEARTBEAT_INTERVAL = 10.0

class ChatMessage(BaseModel):
  role: str
  content: str

class ChatRequest(BaseModel):
  message: str
  session_id: Optional[str] = None
  history: Optional[List[ChatMessage]] = None # 已废弃：历史改由 checkpoint 管理，此处忽略

class ChatResponse(BaseModel):
  response: str
  intent: str
  sources: List[dict]
  session_id: str

async def _get_user_obj(user_info: dict) -> User:
  return await User.get(id=user_info["user_id"])

async def _get_owned_session(session_id: str, user_info: dict) -> ChatSession:
  session = await ChatSession.get_or_none(session_id=session_id)
  if not session or str(session.user_id) != user_info["user_id"]:
    raise HTTPException(status_code=404, detail="会话不存在")
  return session


async def _ensure_session(session_id: str, user_obj: User) -> ChatSession:
  """确保会话存在并属于当前用户（不存在则创建）"""
  session, _ = await ChatSession.get_or_create(
    session_id=session_id, defaults={"user": user_obj}
   )
  return session

async def _session_alive(session: ChatSession) -> bool:
  """防御：流式生成耗时较长，期间用户可能已删除该会话，此时写入会触发外键约束(1452)错误，
  先复核会话是否仍存在，不存在则跳过，避免把错误冒泡成 "Stream error"。"""
  if await ChatSession.filter(id=session.pk).exists():
    return True
  logger.warning("会话 %s 已删除，跳过消息写入", session.pk)
  return False


async def _record_user_message(session: ChatSession, user_msg: str) -> None:
  """流式开始时就落库用户提问：
  前端在生成过程中切换会话/刷新后重新拉取历史时，提问不会因为回答未生成完而丢失。"""
  if not await _session_alive(session):
    return
  await ChatMessageRecord.create(chat_session=session, role="user", content=user_msg)


async def _record_assistant_message(session: ChatSession, assistant_msg: str) -> None:
  """回答生成完毕后落库"""
  if not await _session_alive(session):
    return
  await ChatMessageRecord.create(chat_session=session, role="assistant", content=assistant_msg)


async def _record_messages(session: ChatSession, user_msg: str, assistant_msg: str) -> None:
  """把一轮对话写入 ChatMessageRecord，供侧边栏 UI 读取"""
  await _record_user_message(session, user_msg)
  await _record_assistant_message(session, assistant_msg)


async def _iter_with_heartbeat(source, interval: float = HEARTBEAT_INTERVAL):
  """把上游异步流包装成「空闲时产出 None」的流，用于发送 SSE 注释帧保活。

  - 上游（workflow.astream）有产出时原样转发；
  - 心跳由独立的 ticker 任务定时投递，主循环只做一次 queue.get()，
    避免 wait_for 超时取消 queue.get 导致后续计时失效（实测第 2 个心跳会丢）；
  - 上游异常在消费端重新抛出，让 generate() 的 except 统一兜底；
  - 消费中断时取消 pump / ticker，由 workflow.astream 自身的 finally 关闭 LangGraph 流。
  """
  queue: "asyncio.Queue" = asyncio.Queue()
  sentinel = object()
  beat = object()

  async def pump():
    try:
      async for item in source:
        queue.put_nowait(item)
    except asyncio.CancelledError:
      raise
    except Exception as exc: # 交给消费端统一处理
      queue.put_nowait(exc)
    finally:
      queue.put_nowait(sentinel)

  async def ticker():
    while True:
      await asyncio.sleep(interval)
      queue.put_nowait(beat)

  tasks = [asyncio.create_task(pump()), asyncio.create_task(ticker())]
  try:
    while True:
      item = await queue.get()
      if item is sentinel:
        return
      if item is beat:
        yield None
        continue
      if isinstance(item, Exception):
        raise item
      yield item
  finally:
    for task in tasks:
      if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
      await asyncio.wait(tasks, timeout=5)

@router.post("/send", response_model=ChatResponse)
async def send_message(request: ChatRequest, user=Depends(get_current_user)):
  """发送消息（非流式）"""
  user_obj = await _get_user_obj(user)
  # session_id 为空时为该用户生成新会话，避免回退到共享 "default" 造成跨用户串扰
  session_id = request.session_id or str(uuid.uuid4())
  session = await _ensure_session(session_id, user_obj)

  result = await workflow.run(query=request.message, thread_id=session_id)

  await _record_messages(session, request.message, result["response"])

  return ChatResponse(
    response=result["response"],
    intent=result.get("intent", ""),
    sources=result.get("sources", []),
    session_id=session_id,
   )

@router.post("/stream")
async def stream_message(request: ChatRequest, user=Depends(get_current_user)):
  """发送消息（流式响应）"""
  user_obj = await _get_user_obj(user)
  session_id = request.session_id or str(uuid.uuid4())
  session = await _ensure_session(session_id, user_obj)
  query = request.message

  async def generate():
    full_text = ""
    try:
      # 首帧回传 session_id：前端依赖该字段锁定当前会话（chat.js 中 if (chunk.session_id)），
      # 否则每次发消息 session_id 恒为 null，后端每轮都新建会话，导致会话无法续接、产生孤儿会话
      yield f"data: {json.dumps({'session_id': session_id}, ensure_ascii=False)}\n\n"

      # 先落库用户提问：即使回答还在生成、前端切换/刷新，历史接口也能读到本轮 question
      await _record_user_message(session, query)
      # 走 workflow 流式：仅 qa_generation 节点的 LLM 调用产出 token
      async for item in _iter_with_heartbeat(workflow.astream(query=query, thread_id=session_id)):
        # None = 心跳：SSE 注释帧，前端解析时忽略，仅用于保活与「连接仍在」的判断
        if item is None:
          yield ": ping\n\n"
          continue
        chunk, metadata = item
        node = metadata.get("langgraph_node") if metadata else None
        if node == "qa_generation" and chunk.content:
          full_text += chunk.content
          yield f"data: {json.dumps({'content': chunk.content}, ensure_ascii=False)}\n\n"

      # 流式模式不暴露最终 state，需单独读取（取 sources；并为 search/document 意图兜底）
      graph = workflow.get_graph()
      config = {"configurable": {"thread_id": session_id}}
      state = await graph.aget_state(config)
      values = state.values or {}

      # 非 qa 意图（search/document）未走流式 LLM，用最终 response 兜底一次性输出
      if not full_text:
        fallback = values.get("response", "")
        if fallback:
          full_text = fallback
          yield f"data: {json.dumps({'content': fallback}, ensure_ascii=False)}\n\n"

      if full_text:
        # 回答生成完毕后再落库 assistant 消息（user 消息已在流开始时写入，避免重复）
        await _record_assistant_message(session, full_text)
      else:
        # 零内容兜底：不落库空回答（否则前端刷新后只剩一个空气泡），
        # 并回传明确提示，让前端把「思考中」切换成可读文案
        logger.warning("本轮回答为空，不落库 session=%s", session.pk)
        yield f"data: {json.dumps({'content': '抱歉，本轮未能生成有效回答，请重试或换个问法。'}, ensure_ascii=False)}\n\n"

      # 最后发送 sources
      sources = values.get("sources") or []
      if sources:
        yield f"data: {json.dumps({'sources': sources}, ensure_ascii=False)}\n\n"

      yield "data: [DONE]\n\n"

    except Exception as e:
      logger.exception("Stream error")
      yield f"data: {json.dumps({'content': f'抱歉，处理出错：{e}'}, ensure_ascii=False)}\n\n"
      yield "data: [DONE]\n\n"

  # 关闭代理/网关缓冲，保证心跳与 token 逐帧到达前端
  return StreamingResponse(
    generate(),
    media_type="text/event-stream",
    headers={
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
    },
  )


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user=Depends(get_current_user)):
  """删除会话"""
  session = await _get_owned_session(session_id, user)
  await session.delete() # 外键 CASCADE 自动删除关联消息
  return {"message": "Session deleted"}


@router.get("/sessions")
async def list_sessions(user=Depends(get_current_user)):
  """获取当前用户的会话列表"""
  user_obj = await _get_user_obj(user)
  sessions = await ChatSession.filter(user=user_obj).order_by("-created_at").limit(50)
  result = []
  for s in sessions:
    last_msg = await ChatMessageRecord.filter(chat_session=s, role="user").order_by("created_at").first()
    result.append({
      "session_id": s.session_id,
      "title": last_msg.content[:30] if last_msg else "新对话",
      "created_at": s.created_at.isoformat() if s.created_at else None,
     })
  return {"sessions": result}


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, user=Depends(get_current_user), limit: int = 50):
  """获取某个会话的消息"""  
  session = await _get_owned_session(session_id, user)
  messages = await ChatMessageRecord.filter(chat_session=session).order_by("created_at").limit(limit)
  return {"messages": [{"role": m.role, "content": m.content} for m in messages]}
