"""ASGI trace 中间件（Phase 0，无侵入）。

纯 ASGI 实现：绑定 contextvar trace_id，并只在 http.response.start 上追加
X-Trace-Id 响应头；不包装 / 缓冲响应体，因此 SSE 逐帧内容与时序不受影响。
入站头可透传（便于前端/上游串联），非法值忽略并重新生成。
"""
from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from app.config import settings
from app.observability.context import (
  TRACE_HEADER_BYTES,
  new_trace_id,
  reset_trace_id,
  set_trace_id,
)
from app.observability.spans import span_event

_MAX_TRACE_LEN = 128
_SAFE_TRACE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class TraceIdMiddleware:
  """注册在 main.py：app.add_middleware(TraceIdMiddleware)。"""

  def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
    self.app = app

  async def __call__(
    self,
    scope: MutableMapping[str, Any],
    receive: Callable[..., Awaitable[Any]],
    send: Callable[..., Awaitable[None]],
  ) -> None:
    if scope.get("type") != "http" or not settings.OBSERVABILITY_ENABLED:
      await self.app(scope, receive, send)
      return

    incoming = ""
    for key, value in scope.get("headers", []):
      if key.lower() == TRACE_HEADER_BYTES:
        incoming = value.decode("latin-1").strip()
        break
    if not _SAFE_TRACE_ID.fullmatch(incoming):
      incoming = ""
    trace_id = incoming or new_trace_id()

    token = set_trace_id(trace_id)
    status_code: int | None = None
    started = time.perf_counter()

    async def send_with_trace(message: MutableMapping[str, Any]) -> None:
      nonlocal status_code
      if message["type"] == "http.response.start":
        status_code = message.get("status")
        headers = [
          (k, v) for k, v in message.get("headers", [])
          if k.lower() != TRACE_HEADER_BYTES
         ]
        headers.append((TRACE_HEADER_BYTES, trace_id.encode("latin-1")))
        message["headers"] = headers
      await send(message)

    try:
      await self.app(scope, receive, send_with_trace)
    finally:
      try:
        # 结构化访问日志（一行 JSON），此时 trace_id 仍在上下文中
        span_event(
          "http.request",
          method=scope.get("method"),
          path=scope.get("path"),
          status=status_code,
          duration_ms=round((time.perf_counter() - started) * 1000, 3),
         )
      finally:
        # 即使 span_event 抛异常，也必须复原 contextvar，避免污染后续请求
        reset_trace_id(token)
