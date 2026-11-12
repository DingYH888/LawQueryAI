"""LangGraph 工作流编排 — 定义 Agent 之间的协作流程
对话记忆通过 AIOMySQLSaver（MySQL checkpointer）持久化，
按 thread_id（= session_id）跨轮次、跨进程重启恢复。"""
import asyncio
import hashlib
import json
import logging
from typing import TypedDict, Annotated

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

from app.agents.intent_agent import IntentAgent
from app.agents.qa_agent import QAAgent
from app.agents.document_agent import DocumentAgent
from app.agents.retrieval_agent import RetrievalAgent
from app.agents.resilience import concurrency_guard, empty_recall_reason
from app.config import settings
from app.llm.checkpoint import get_checkpointer
from app.observability.context import get_trace_id
from app.observability.spans import span_event, start_span


def _fp(text: str) -> str:
  """节点 span 只记 query 长度/指纹，绝不写用户原文（法律咨询含个人信息）。"""
  return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

class AgentState(TypedDict):
  """Agent 状态定义"""
  messages: Annotated[list[BaseMessage], add_messages] # 对话历史，自动累积
  query: str          # 当前用户问题
  intent: str          # 识别的意图
  retrieved_cases: list     # 检索到的案例（每轮覆盖，不进入 messages）
  document_type: str      # 文书类型
  document_params: dict     # 文书参数
  response: str         # 最终响应
  sources: list         # 来源引用
  trace_id: str          # 请求级 trace_id（Phase 1）
  fallback_reason: str     # 最近一次降级原因（Phase 1）
  tool_calls_made: int      # 本轮实际工具执行次数（Phase 1）

class LegalMindWorkflow:
  """法律咨询 Agent 工作流"""

  def __init__(self):
    self.intent_agent = IntentAgent()
    self.qa_agent = QAAgent()
    self.document_agent = DocumentAgent()
    self.retrieval_agent = RetrievalAgent()
    # 延迟编译：checkpointer 在 lifespan 启动后才初始化，模块导入时尚不可用
    self._graph = None
    # 仅 function_call 模式才构造工具路由，direct 默认链路零额外开销
    self._tool_router = None

  def _build_graph(self):
    """构建并编译工作流图（使用持久化 checkpointer）。"""
    workflow = StateGraph(AgentState)

    # 添加节点
    workflow.add_node("intent_recognition", self._intent_node)
    workflow.add_node("case_retrieval", self._retrieval_node)
    workflow.add_node("qa_generation", self._qa_node)
    workflow.add_node("document_generation", self._document_node)
    workflow.add_node("final_output", self._output_node)

    # 设置入口
    workflow.set_entry_point("intent_recognition")

    # 意图识别后按意图类型路由
    workflow.add_conditional_edges("intent_recognition", self._route_by_intent, {
      "qa": "case_retrieval",
      "search": "case_retrieval",
      "document": "document_generation",
     })

    # 检索后根据意图决定下一步
    workflow.add_conditional_edges("case_retrieval", self._route_after_retrieval, {
      "qa": "qa_generation",
      "search": "final_output",
     })

    workflow.add_edge("qa_generation", "final_output")
    workflow.add_edge("document_generation", "final_output")
    workflow.add_edge("final_output", END)

    return workflow.compile(checkpointer=get_checkpointer())

  def get_graph(self):
    """懒加载编译图。首次调用时编译并缓存（此时 checkpointer 已在 lifespan 中就绪）。"""
    if self._graph is None:
      self._graph = self._build_graph()
    return self._graph

  def _get_tool_router(self):
    """仅 AGENT_TOOL_MODE=function_call 时构造，复用既有 LLM / 检索实例。"""
    if self._tool_router is None:
      from app.agents.tool_router import ToolRouter
      self._tool_router = ToolRouter(
        llm=self.qa_agent.llm, retrieval_agent=self.retrieval_agent
       )
    return self._tool_router

  async def _intent_node(self, state: AgentState) -> dict:
    """意图节点：structured 主路径，legacy 为开关/降级分支。
     用 start_span 而非装饰器，避免 LangGraph 对节点签名的探测受影响。"""
    query = state["query"]
    with start_span("intent_recognition", query_len=len(query), query_sha256=_fp(query)):
      outcome = await self.intent_agent.recognize_structured(query)
      span_event("intent.result", intent=outcome["intent"],
                 fallback_reason=outcome.get("fallback_reason"))
      return {
        "intent": outcome["intent"],
        "fallback_reason": outcome.get("fallback_reason") or "",
        "trace_id": get_trace_id(),
       }

  async def _retrieval_node(self, state: AgentState) -> dict:
    """案例检索节点：按 AGENT_TOOL_MODE 分流；条件边与 SSE 过滤规则不变。"""
    query = state["query"]
    prior_reason = state.get("fallback_reason") or ""
    with start_span("case_retrieval", query_len=len(query), query_sha256=_fp(query),
                    mode=settings.AGENT_TOOL_MODE):
      if settings.AGENT_TOOL_MODE == "function_call":
        outcome = await self._get_tool_router().retrieve(query=query, top_k=5)
        cases = outcome["cases"]
        reason = empty_recall_reason(
          cases, outcome.get("fallback_reason") or prior_reason
         )
        span_event("retrieval.result", mode="function_call",
                   cases=len(cases), fallback_reason=reason or None,
                   tool_calls_made=outcome.get("tool_calls_made", 0),
                   skipped_calls=outcome.get("skipped_calls", 0))
        return {
          "retrieved_cases": cases,
          "fallback_reason": reason,
          "tool_calls_made": outcome.get("tool_calls_made", 0),
          "trace_id": get_trace_id(),
         }

      # direct（默认稳定链路）：检索超时/异常都降级为空召回，不打断请求
      reason = prior_reason
      try:
        cases = await asyncio.wait_for(
          self.retrieval_agent.retrieve(query=query, top_k=5),
          timeout=settings.RETRIEVAL_TIMEOUT,
         )
      except asyncio.CancelledError:
        raise
      except asyncio.TimeoutError:
        cases, reason = [], "retrieval_timeout"
      except Exception as exc:
        cases, reason = [], "retrieval_error"
        span_event("retrieval.error", level=logging.WARNING,
                   error=f"{type(exc).__name__}: {exc}")
      reason = empty_recall_reason(cases, reason)
      span_event("retrieval.result", mode="direct", cases=len(cases),
                 fallback_reason=reason or None)
      return {"retrieved_cases": cases, "fallback_reason": reason,
              "tool_calls_made": 0}

  async def _qa_node(self, state: AgentState) -> dict:
    """问答节点：从 checkpoint 恢复的 state['messages'] 读取历史（已含当前 HumanMessage），
     节点内部用 llm.astream 生成回答，使 graph.astream(stream_mode='messages') 能转发 token。"""
    cases = state["retrieved_cases"]
    messages = state["messages"]
    with start_span("qa_generation", cases=len(cases)):
      full_text = ""
      async for chunk in self.qa_agent.astream_answer(cases, messages):
        full_text += chunk.content or ""
      span_event("qa_generation.result", chars=len(full_text))
      return {
        "messages": [AIMessage(content=full_text)],
        "response": full_text,
        "sources": self.qa_agent._extract_sources(cases),
       }

  async def _document_node(self, state: AgentState) -> dict:
    """文书生成节点"""
    with start_span("document_generation",
                    document_type=state.get("document_type", "起诉状")):
      response = await self.document_agent.generate(
        document_type=state.get("document_type", "起诉状"),
        params=state.get("document_params", {}),
        query=state["query"]
       )
      content = response["content"]
      span_event("document_generation.result", chars=len(content))
      return {
        "messages": [AIMessage(content=content)],
        "response": content,
        "sources": response.get("references", []),
       }

  async def _output_node(self, state: AgentState) -> dict:
    """输出节点。注意只返回需更新的字段——不能 return 整个 state，
     否则 messages 会被 add_messages 二次追加导致历史重复。search 意图把检索结果格式化为响应。"""
    if state.get("intent") == "search" and not state.get("response"):
      cases = state.get("retrieved_cases", [])
      text = json.dumps(cases, ensure_ascii=False, indent=2)
      return {
        "messages": [AIMessage(content=text)],
        "response": text,
        "sources": self.qa_agent._extract_sources(cases),
       }
    return {}

  def _route_by_intent(self, state: AgentState) -> str:
    intent = state["intent"]
    if intent == "document":
      return "document"
    elif intent == "search":
      return "search"
    return "qa"

  def _route_after_retrieval(self, state: AgentState) -> str:
    if state["intent"] == "search":
      return "search"
    return "qa"

  async def run(self, query: str, thread_id: str) -> dict:
    """执行工作流（非流式）。thread_id 即 session_id，相同 thread_id 会从 checkpoint 恢复历史。"""
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = {
      "query": query,
      "messages": [HumanMessage(content=query)],
      "retrieved_cases": [],
      "sources": [],
      "trace_id": get_trace_id(),
      "fallback_reason": "",
      "tool_calls_made": 0,
     }
    async with concurrency_guard("workflow.run"):
      return await self.get_graph().ainvoke(initial_state, config=config)

  async def astream(self, query: str, thread_id: str):
    """执行工作流（流式）。按 stream_mode='messages' 产出 (AIMessageChunk, metadata) 元组。
     意图识别/检索不调用流式 LLM，仅 qa_generation 节点产出 token；消费者按 metadata['langgraph_node'] 过滤。"""
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = {
      "query": query,
      "messages": [HumanMessage(content=query)],
      "retrieved_cases": [],
      "sources": [],
      "trace_id": get_trace_id(),
      "fallback_reason": "",
      "tool_calls_made": 0,
     }
    stream = self.get_graph().astream(
      initial_state, config=config, stream_mode="messages"
     )
    async with concurrency_guard("workflow.astream"):
      try:
        async for chunk, metadata in stream:
          yield chunk, metadata
      finally:
        # 消费者取消/断开时 async for 不会自动关闭内层生成器，
        # 显式 aclose 让 LangGraph 立刻取消节点任务，而不是后台跑完
        await stream.aclose()


# 全局工作流实例（图在首次 run/astream 时才编译，供 API 层直接调用）
workflow = LegalMindWorkflow()
