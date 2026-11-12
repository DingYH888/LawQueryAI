"""Function Call 路由（Phase 1）。

自写有界执行循环，不使用 langgraph.prebuilt.ToolNode：
  ChatOpenAI.bind_tools → 解析 tool_calls → ToolExecutor 执行 → ToolMessage 回填。
Phase 2：followup 结果未被使用，改为 AGENT_TOOL_FOLLOWUP_ENABLED 默认关闭，省一次 LLM 调用。
未知工具名 / 参数非法 / 执行异常 / LLM 异常 → 降级为代码直调
retrieval_agent.retrieve(query, top_k=5)，并写 fallback_reason；
调用超限只跳过剩余调用、保留已拿到的 cases；降级路径自身失败返回空列表，不抛给上层。
"""
import asyncio
import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.agents.tools.executor import FALLBACK_INVALID_ARGS, ToolExecutor
from app.agents.tools.registry import (
  ToolContext,
  openai_tool_specs,
  sanitize_args,
  text_fingerprint,
)
from app.config import settings
from app.llm.model_client import get_llm
from app.observability.spans import span_event, start_span

_TOOL_SYSTEM_PROMPT = (
  "你是法律案例检索助手。请调用提供的工具检索与用户问题相关的案例；"
  "不要自行编造案例，也不要在工具结果之外添加无关内容。"
)


def _normalize_cases(data: Any) -> list[dict]:
  if isinstance(data, list):
    return [d for d in data if isinstance(d, dict)]
  if isinstance(data, dict):
    return [data]
  return []


def _compact_data(data: Any) -> Any:
  """ToolMessage 只回填必要字段，避免把案例全文/PII 再送回模型。"""
  if isinstance(data, list):
    return [_compact_data(d) for d in data[:5]]
  if isinstance(data, dict):
    keep = {k: data.get(k) for k in
            ("id", "title", "case_number", "court", "judgment_date") if k in data}
    if "summary" in data:
      keep["summary"] = str(data["summary"] or "")[:200]
    return keep
  return str(data)[:200]


class FallbackDisabled(RuntimeError):
  """AGENT_FALLBACK_ENABLED=False 时显式上抛，不被内部兜底吞掉。"""


class ToolRouter:
  def __init__(self, llm: Any = None, retrieval_agent: Any = None,
               case_service: Any = None, executor: ToolExecutor | None = None):
    self.llm = llm if llm is not None else get_llm()
    if retrieval_agent is None:
      from app.agents.retrieval_agent import RetrievalAgent
      retrieval_agent = RetrievalAgent()
    self.retrieval_agent = retrieval_agent
    self.context = ToolContext(retrieval_agent=retrieval_agent, case_service=case_service)
    self.executor = executor or ToolExecutor(context=self.context)
    self._bound = None

  @property
  def bound_tools(self) -> Any:
    """真实 bind_tools 只做一次，日志可证明绑定发生。"""
    if self._bound is None:
      specs = openai_tool_specs()
      self._bound = self.llm.bind_tools(specs)
      span_event("tool.bind_tools", bound=True,
                 tools=[s["function"]["name"] for s in specs])
    return self._bound

  async def _fallback(self, query: str, out: dict, reason: str) -> dict:
    span_event("tool.fallback", level=logging.WARNING,
               fallback_reason=reason, tool_calls_made=out["tool_calls_made"])
    if not settings.AGENT_FALLBACK_ENABLED:
      raise FallbackDisabled(f"tool fallback disabled: {reason}")
    try:
      out["cases"] = await asyncio.wait_for(
        self.retrieval_agent.retrieve(query=query, top_k=5),
        timeout=settings.RETRIEVAL_TIMEOUT,
       )
    except asyncio.CancelledError:
      raise
    except Exception as exc:
      # Phase 2(c)：降级路径自身兜底——直调也失败时返回空列表，由 QA 以无案例模式继续
      span_event("tool.fallback_error", level=logging.WARNING,
                 fallback_reason=reason, error=f"{type(exc).__name__}: {exc}")
      out["cases"] = []
    out["fallback_reason"] = reason
    return out

  async def retrieve(self, query: str, top_k: int = 5) -> dict:
    """返回 {"cases", "fallback_reason", "tool_calls_made", "tool_names"}。"""
    out: dict[str, Any] = {
      "cases": [], "fallback_reason": None,
      "tool_calls_made": 0, "tool_names": [], "skipped_calls": 0,
     }
    with start_span("tool_router", query_len=len(query),
                    query_sha256=text_fingerprint(query), top_k=top_k):
      try:
        messages: list[Any] = [
          SystemMessage(content=_TOOL_SYSTEM_PROMPT),
          HumanMessage(content=query),
         ]
        bound = self.bound_tools
        response = await bound.ainvoke(messages)
        calls = list(getattr(response, "tool_calls", None) or [])
        span_event("tool.llm_call", bound_tools=True,
                   tool_calls=[c.get("name") for c in calls],
                   params=[sanitize_args(c.get("args")) for c in calls])
        if not calls:
          return await self._fallback(query, out, "tool_no_calls")

        messages.append(response)
        cases: list[dict] = []
        skipped = 0
        for call in calls:
          call_id = str(call.get("id") or f"call_{len(messages)}")
          if self.executor.remaining <= 0:
            # Phase 2(c)：超限只跳过剩余调用，保留已拿到的 cases，不整单降级
            skipped += 1
            messages.append(ToolMessage(
              content=json.dumps({"ok": False, "error": "tool_call_limit_exceeded",
                                  "skipped": True}, ensure_ascii=False),
              tool_call_id=call_id,
             ))
            continue

          name = str(call.get("name") or "")
          raw_args = call.get("args")
          result = await self.executor.execute(name, raw_args if isinstance(raw_args, dict) else {})
          out["tool_calls_made"] = self.executor.calls_made

          if result.ok:
            out["tool_names"].append(name)
            tool_content = json.dumps(
              {"ok": True, "tool": name, "data": _compact_data(result.data)},
              ensure_ascii=False, default=str,
             )
            cases = _normalize_cases(result.data)
          else:
            tool_content = json.dumps(
              {"ok": False, "error": result.error, "fallback_reason": result.fallback_reason},
              ensure_ascii=False,
             )
          messages.append(ToolMessage(content=tool_content, tool_call_id=call_id))
          if not result.ok:
            return await self._fallback(query, out, result.fallback_reason or FALLBACK_INVALID_ARGS)

        out["skipped_calls"] = skipped
        if skipped:
          span_event("tool.call_skipped", level=logging.WARNING, count=skipped,
                     reason="tool_call_limit_exceeded", kept_cases=len(cases))

        # ToolMessage 已回填；followup 结果未被使用，默认关闭以省一次 LLM 调用
        if settings.AGENT_TOOL_FOLLOWUP_ENABLED:
          try:
            final = await bound.ainvoke(messages)
            span_event("tool.followup",
                       tool_calls=[c.get("name") for c in (getattr(final, "tool_calls", None) or [])],
                       has_content=bool(getattr(final, "content", "")))
          except asyncio.CancelledError:
            raise
          except Exception as exc:
            span_event("tool.followup_error", level=logging.WARNING,
                       error=f"{type(exc).__name__}: {exc}")
        else:
          span_event("tool.followup_skipped", reason="disabled")

        if not cases:
          return await self._fallback(query, out, "tool_no_cases")
        out["cases"] = cases
        return out
      except asyncio.CancelledError:
        raise
      except FallbackDisabled:
        raise
      except Exception as exc:
        span_event("tool.router_error", level=logging.WARNING,
                   error=f"{type(exc).__name__}: {exc}")
        return await self._fallback(query, out, FALLBACK_INVALID_ARGS)
