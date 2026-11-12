"""可观测性基座（Phase 0，无侵入）。

只提供 trace_id 上下文、JSON span 日志与 ASGI 中间件；
不参与 Agent 编排，也不改变 direct 执行链路。"""
from app.observability.context import (
  TRACE_HEADER,
  get_trace_id,
  new_trace_id,
  reset_trace_id,
  set_trace_id,
  trace_context,
)
from app.observability.middleware import TraceIdMiddleware
from app.observability.spans import span, span_event, start_span

__all__ = [
  "TRACE_HEADER",
  "get_trace_id",
  "new_trace_id",
  "reset_trace_id",
  "set_trace_id",
  "trace_context",
  "TraceIdMiddleware",
  "span",
  "span_event",
  "start_span",
]
