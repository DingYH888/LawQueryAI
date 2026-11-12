"""意图识别 Agent — 识别用户意图：法律问答、文书生成、案例检索

Phase 1：新增结构化输出主路径（json_schema + include_raw）。
解析失败时复用【现有】strip().lower() + 白名单逻辑降级；
Phase 2：异常/超时不再二次调用 LLM，直接落默认 qa。
旧 recognize() 保留不删（legacy 开关分支），未新写任何正则解析器。"""
import asyncio
import logging
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from pydantic import BaseModel, Field

from app.config import settings
from app.llm.model_client import get_llm
from app.llm.prompts import INTENT_RECOGNITION_PROMPT
from app.observability.spans import span_event


class IntentResult(BaseModel):
  """结构化意图输出 schema。"""
  intent: Literal["qa", "document", "search"] = Field(description="识别出的意图类型")


class IntentFallbackDisabled(RuntimeError):
  """AGENT_FALLBACK_ENABLED=False 时显式上抛，避免被通用异常兜底吞掉。"""


def _usage_of(result) -> dict | None:
  """结构化调用只是旁路记录 token 用量，供离线评测脚本汇总，不改变任何业务返回。"""
  if not isinstance(result, dict):
    return None
  raw = result.get("raw")
  if raw is None:
    return None
  usage = getattr(raw, "usage_metadata", None)
  if isinstance(usage, dict) and usage:
    return {
      "input_tokens": usage.get("input_tokens"),
      "output_tokens": usage.get("output_tokens"),
      "total_tokens": usage.get("total_tokens"),
     }
  token_usage = (getattr(raw, "response_metadata", None) or {}).get("token_usage")
  if isinstance(token_usage, dict) and token_usage:
    return {
      "input_tokens": token_usage.get("prompt_tokens"),
      "output_tokens": token_usage.get("completion_tokens"),
      "total_tokens": token_usage.get("total_tokens"),
     }
  return None


def _raw_content(result) -> str:
  """从 include_raw 结果里取 raw.content，供白名单兜底。"""
  if not isinstance(result, dict):
    return ""
  raw = result.get("raw")
  content = getattr(raw, "content", "") if raw is not None else ""
  if isinstance(content, str):
    return content
  if isinstance(content, list):
    return "".join(
      str(block.get("text", "")) if isinstance(block, dict) else str(block)
      for block in content
     )
  return str(content or "")


class IntentAgent:
  """意图识别 Agent"""
  INTENTS = Literal["qa", "document", "search"]
  VALID_INTENTS = ["qa", "document", "search"]

  def __init__(self):
    self.llm = get_llm()
    self.prompt = ChatPromptTemplate.from_template(INTENT_RECOGNITION_PROMPT)
    self.chain = self.prompt | self.llm | StrOutputParser()

  @staticmethod
  def _normalize_intent(text: str) -> str:
    """现有 strip().lower() + 白名单逻辑（未写正则），两条路径共用。"""
    intent = text.strip().lower()
    if intent not in IntentAgent.VALID_INTENTS:
      intent = "qa"
    return intent

  async def recognize(self, query: str) -> str:
    """旧链路，保留不删：Prompt → LLM → 字符串 → 白名单。"""
    response = await self.chain.ainvoke({"query": query})
    return self._normalize_intent(response)

  def _structured_llm(self):
    return self.llm.with_structured_output(
      IntentResult, method="json_schema", include_raw=True
     )

  def _fallback_from_raw(self, raw_content: str, reason: str) -> dict:
    if not settings.AGENT_FALLBACK_ENABLED:
      raise IntentFallbackDisabled(f"intent fallback disabled: {reason}")
    intent = self._normalize_intent(raw_content)
    span_event("intent.fallback", level=logging.WARNING,
               fallback_reason=reason, intent=intent, raw_len=len(raw_content))
    return {"intent": intent, "fallback_reason": reason}

  async def recognize_structured(self, query: str) -> dict:
    """返回 {"intent", "fallback_reason"}。

    legacy 模式走旧 recognize()；解析失败走 raw.content 白名单；
    异常/超时直接落 qa，不再发起第二次 LLM 调用（Phase 2）。
    """
    if settings.AGENT_INTENT_MODE != "structured":
      return {"intent": await self.recognize(query), "fallback_reason": None}

    try:
      messages = self.prompt.format_messages(query=query)
      result = await asyncio.wait_for(
        self._structured_llm().ainvoke(messages),
        timeout=settings.LLM_INTENT_TIMEOUT,
       )
      parsed = result.get("parsed") if isinstance(result, dict) else None
      if isinstance(parsed, IntentResult):
        span_event("intent.structured", intent=parsed.intent, fallback_reason=None,
                   usage=_usage_of(result))
        return {"intent": parsed.intent, "fallback_reason": None}
      # parsed 为 None / parsing_error：取 raw.content 交给现有白名单逻辑
      return self._fallback_from_raw(_raw_content(result), "intent_parse_failed")
    except asyncio.CancelledError:
      raise
    except IntentFallbackDisabled:
      raise # AGENT_FALLBACK_ENABLED=False 时显式抛出，不静默降级
    except Exception as exc:
      # 注意：pydantic.ValidationError 是 ValueError 子类，SDK 层解析失败也走这里降级
      span_event("intent.structured_error", level=logging.WARNING,
                 error=f"{type(exc).__name__}: {exc}")
      if not settings.AGENT_FALLBACK_ENABLED:
        raise
      # 异常/超时：不再二次调用 LLM，直接落默认 qa（Phase 2(b)）
      span_event("intent.fallback", level=logging.WARNING,
                 fallback_reason="intent_parse_failed", intent="qa", raw_len=0,
                 reason=type(exc).__name__)
      return {"intent": "qa", "fallback_reason": "intent_parse_failed"}
