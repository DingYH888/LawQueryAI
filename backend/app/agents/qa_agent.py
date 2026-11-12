"""法律问答 Agent — 基于检索到的案例回答法律问题
对话历史以 LangChain messages 形式由 workflow 的 checkpoint 提供，
本 Agent 负责把 system 指令 + 案例上下文 + 历史消息组装后交给 LLM。"""
import logging
from typing import List, Dict, AsyncIterator
from langchain_core.messages import BaseMessage, SystemMessage, AIMessageChunk

from app.config import settings
from app.llm.model_client import get_llm
from app.llm.prompts import QA_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class QAAgent:
  """法律问答 Agent"""

  def __init__(self):
    self.llm = get_llm()
    # 只保留最近 N 条历史：checkpointer 会累积整段对话，prompt 越滚越大，
    # 首 token 延迟随之增长；末尾一定包含当前提问。
    self.max_history = max(2, int(settings.AGENT_HISTORY_MAX_MESSAGES))

  def build_messages(self, cases: List[Dict], messages: List[BaseMessage]) -> List[BaseMessage]:
    """组装 LLM 输入：system（人设 + 案例上下文）+ 对话历史（含当前问题）。
     案例作为当轮 system 注入，绝不进入长期累积的 messages。"""
    system_content = QA_SYSTEM_PROMPT + "\n\n## 相关案例\n" + self._format_cases(cases)
    history = list(messages or [])
    if len(history) > self.max_history:
      logger.info("历史消息 %d 条，截断为最近 %d 条", len(history), self.max_history)
      history = history[-self.max_history:]
    return [SystemMessage(content=system_content)] + history

  async def answer(self, cases: List[Dict], messages: List[BaseMessage]) -> Dict:
    """非流式回答"""
    response = await self.llm.ainvoke(self.build_messages(cases, messages))
    return {"answer": response.content, "sources": self._extract_sources(cases)}

  async def astream_answer(
    self, cases: List[Dict], messages: List[BaseMessage]
   ) -> AsyncIterator[AIMessageChunk]:
    """流式回答，逐个产出 AIMessageChunk（token 在 chunk.content）。

     在 workflow 的 _qa_node 内调用，使 graph.astream(stream_mode='messages')
     能转发 token 给流式消费者。消费中断时显式 aclose 上游 LLM 流。"""
    stream = self.llm.astream(self.build_messages(cases, messages))
    try:
      async for chunk in stream:
        yield chunk
    finally:
      try:
        await stream.aclose()
      except Exception as exc: # 不让关闭上游流的异常覆盖原始 CancelledError
        logger.warning("关闭 QA 上游流失败: %s", exc)

  def _format_cases(self, cases: List[Dict]) -> str:
    if not cases:
      return "暂无相关案例参考。"
    formatted = []
    for i, case in enumerate(cases, 1):
      formatted.append(
        f"【案例{i}】\n"
        f"标题：{case.get('title', '未知')}\n"
        f"案号：{case.get('case_number', '未知')}\n"
        f"法院：{case.get('court', '未知')}\n"
        f"裁判要旨：{case.get('summary', '未知')}\n"
        f"相关法条：{case.get('laws', '未知')}\n"
       )
    return "\n".join(formatted)

  def _extract_sources(self, cases: List[Dict]) -> List[Dict]:
    sources = []
    for case in cases[:3]:
      sources.append({
        "title": case.get("title"),
        "case_number": case.get("case_number"),
        "court": case.get("court"),
        "date": case.get("judgment_date"),
       })
    return sources
