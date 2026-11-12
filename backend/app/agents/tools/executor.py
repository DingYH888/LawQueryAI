"""统一工具执行器（Phase 1）。

职责：参数校验、失败分类（可重试 / 不可重试）、调用上限、统一 ToolResult。
本阶段只分类不重试（重试策略属 Phase 2）；CancelledError 原样传播。
"""
import asyncio
import logging
import time
from enum import Enum
from typing import Any

from pydantic import ValidationError

from app.agents.tools.registry import (
  ToolContext,
  ToolResult,
  get_tool,
  sanitize_args,
  validate_args,
)
from app.config import settings
from app.observability.context import get_trace_id
from app.observability.spans import span_event

FALLBACK_INVALID_ARGS = "tool_invalid_args"


class FailureKind(str, Enum):
  NONE = "none"
  RETRYABLE = "retryable"       # 网络 / 超时等瞬时故障
  NON_RETRYABLE = "non_retryable" # 未知工具 / 参数非法 / 业务异常


def classify_failure(exc: BaseException) -> FailureKind:
  """只分类，不重试。Phase 2 依据此分类决定重试策略。"""
  if isinstance(exc, (ValidationError, ValueError, TypeError, KeyError)):
    return FailureKind.NON_RETRYABLE
  if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
    return FailureKind.RETRYABLE
  return FailureKind.NON_RETRYABLE


class ToolExecutor:
  def __init__(self, context: ToolContext | None = None, max_calls: int | None = None):
    self.context = context
    self.max_calls = int(settings.AGENT_MAX_TOOL_CALLS if max_calls is None else max_calls)
    self.calls_made = 0
    self.last_failure_kind: FailureKind = FailureKind.NONE

  @property
  def remaining(self) -> int:
    return max(0, self.max_calls - self.calls_made)

  def _ctx(self) -> ToolContext:
    if self.context is not None:
      return self.context
    from app.agents.retrieval_agent import RetrievalAgent
    from app.services.case_service import CaseService
    self.context = ToolContext(retrieval_agent=RetrievalAgent(), case_service=CaseService())
    return self.context

  async def execute(self, name: str, raw_args: Any) -> ToolResult:
    started = time.perf_counter()
    trace_id = get_trace_id()
    self.last_failure_kind = FailureKind.NONE
    span_event("tool.call", tool=name, params=sanitize_args(raw_args),
               max_calls=self.max_calls, calls_made=self.calls_made)

    def fail(error: str, reason: str, kind: FailureKind) -> ToolResult:
      self.last_failure_kind = kind
      latency = round((time.perf_counter() - started) * 1000, 3)
      span_event("tool.result", level=logging.WARNING, tool=name, ok=False,
                 error=error, failure_kind=kind.value, fallback_reason=reason,
                 latency_ms=latency, trace_id=trace_id)
      return ToolResult(ok=False, data=None, error=error, latency_ms=latency,
                        trace_id=trace_id, fallback_reason=reason)

    if self.remaining <= 0:
      return fail("tool_call_limit_exceeded", "tool_call_limit_exceeded",
                  FailureKind.NON_RETRYABLE)

    spec = get_tool(name)
    if spec is None:
      return fail(f"unknown_tool:{name}", FALLBACK_INVALID_ARGS,
                  FailureKind.NON_RETRYABLE)

    try:
      args = validate_args(spec, raw_args)
    except (ValidationError, ValueError) as exc:
      short = exc.errors()[0].get("type", "validation_error") if isinstance(exc, ValidationError) else str(exc)[:120]
      return fail(f"invalid_args:{short}", FALLBACK_INVALID_ARGS,
                  FailureKind.NON_RETRYABLE)

    self.calls_made += 1
    try:
      # Phase 2：工具执行同样受 RETRIEVAL_TIMEOUT 约束（实测检索 12~52ms）
      data = await asyncio.wait_for(
        spec.handler(args, self._ctx()),
        timeout=settings.RETRIEVAL_TIMEOUT,
       )
    except asyncio.CancelledError:
      raise
    except Exception as exc:
      return fail(f"execution_error:{type(exc).__name__}", FALLBACK_INVALID_ARGS,
                  classify_failure(exc))

    latency = round((time.perf_counter() - started) * 1000, 3)
    span_event("tool.result", tool=name, ok=True, fallback_reason=None,
               failure_kind=FailureKind.NONE.value, latency_ms=latency,
               trace_id=trace_id)
    return ToolResult(ok=True, data=data, latency_ms=latency,
                      trace_id=trace_id, fallback_reason=None)
