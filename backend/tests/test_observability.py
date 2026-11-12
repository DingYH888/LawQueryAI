"""Phase 0 观测基座用例（不依赖 MySQL / LLM / 向量库）。

可用 `python -m pytest` 或 `python -m unittest discover -s tests` 运行。
"""
import asyncio
import io
import json
import logging
import re
import sys
import unittest.mock as mock
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
  sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings
from app.observability.context import get_trace_id, set_trace_id, trace_context
from app.observability.middleware import TraceIdMiddleware
from app.observability.spans import LOGGER_NAME, _JsonFormatter, span, span_event


class _Capture(logging.Handler):
  def __init__(self):
    super().__init__(level=logging.DEBUG)
    self.events = []

  def emit(self, record):
    payload = getattr(record, "observability", None)
    if isinstance(payload, dict):
      self.events.append(dict(payload))


@contextmanager
def _capture_events():
  logger = logging.getLogger(LOGGER_NAME)
  handler = _Capture()
  old_level = logger.level
  logger.setLevel(logging.DEBUG)
  logger.addHandler(handler)
  try:
    yield handler.events
  finally:
    logger.removeHandler(handler)
    logger.setLevel(old_level)


@contextmanager
def _capture_json_lines():
  logger = logging.getLogger(LOGGER_NAME)
  stream = io.StringIO()
  handler = logging.StreamHandler(stream)
  handler.setFormatter(_JsonFormatter())
  old_level = logger.level
  logger.setLevel(logging.DEBUG)
  logger.addHandler(handler)
  try:
    yield stream
  finally:
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def _make_app(with_middleware: bool) -> FastAPI:
  app = FastAPI()

  @app.get("/health")
  async def health():
    return {"status": "healthy", "version": "1.0.0"}

  @app.post("/sse")
  async def sse():
    async def gen():
      yield 'data: {"session_id": "s1"}\n\n'
      yield 'data: {"content": "你"}\n\n'
      yield 'data: {"content": "好"}\n\n'
      yield 'data: {"sources": [{"title": "t"}]}\n\n'
      yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

  if with_middleware:
    app.add_middleware(TraceIdMiddleware)
  return app


def _client(app: FastAPI) -> httpx.AsyncClient:
  return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_trace_context_and_task_isolation():
  assert get_trace_id() == ""
  with trace_context("trace-A") as tid:
    assert tid == "trace-A"
    assert get_trace_id() == "trace-A"
  assert get_trace_id() == ""

  async def worker(name):
    set_trace_id(name)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return get_trace_id()

  async def main():
    return await asyncio.gather(worker("trace-1"), worker("trace-2"))

  assert asyncio.run(main()) == ["trace-1", "trace-2"]
  assert get_trace_id() == ""


def test_span_json_logs_params_and_redaction():
  @span("unit.echo")
  def echo(query, api_key, top_k=5):
    return query

  @span
  async def async_echo(query):
    await asyncio.sleep(0)
    return query

  with trace_context("trace-span-1"):
    with _capture_json_lines() as stream:
      assert echo("离婚财产分割", "sk-should-not-leak", top_k=3) == "离婚财产分割"
      assert asyncio.run(async_echo("劳动合同")) == "劳动合同"
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines] # 每行必须是合法 JSON

  assert len(parsed) == 4
  assert all(p["trace_id"] == "trace-span-1" for p in parsed)
  assert all("ts" in p and "level" in p and "logger" in p for p in parsed)
  starts = [p for p in parsed if p["event"] == "span.start"]
  ends = [p for p in parsed if p["event"] == "span.end"]
  assert len(starts) == 2 and len(ends) == 2
  assert all(p["status"] == "ok" and p["duration_ms"] >= 0 for p in ends)
  assert {e["span_id"] for e in starts} == {e["span_id"] for e in ends}
  echo_start = next(p for p in starts if p["span"] == "unit.echo")
  assert echo_start["params"]["query"] == "离婚财产分割" # span 可见参数
  assert echo_start["params"]["top_k"] == 3
  assert echo_start["params"]["api_key"] == "***"
  assert "sk-should-not-leak" not in "\n".join(lines)


def test_span_error_and_cancelled_are_logged_and_reraised():
  @span("unit.fail")
  def fail():
    raise ValueError("boom")

  @span("unit.cancel")
  async def cancel():
    raise asyncio.CancelledError()

  with _capture_events() as events:
    try:
      fail()
    except ValueError:
      pass
    else:
      raise AssertionError("ValueError 必须原样抛出")
    try:
      asyncio.run(cancel())
    except asyncio.CancelledError:
      pass
    else:
      raise AssertionError("CancelledError 必须原样传播")

  ends = [e for e in events if e["event"] == "span.end"]
  assert {e["span"]: e["status"] for e in ends} == {
    "unit.fail": "error", "unit.cancel": "cancelled",
   }
  assert any("ValueError" in e.get("error", "") for e in ends)


def test_span_event_accepts_fallback_reason_field():
  with trace_context("trace-fb"):
    with _capture_events() as events:
      span_event("tool.fallback", fallback_reason="invalid_tool_args", mode="direct")
  assert len(events) == 1
  assert events[0]["fallback_reason"] == "invalid_tool_args"
  assert events[0]["trace_id"] == "trace-fb"


def test_health_returns_trace_header():
  async def main():
    async with _client(_make_app(True)) as c:
      r1 = await c.get("/health")
      r2 = await c.get("/health", headers={"X-Trace-Id": "caller-supplied-123"})
      r3 = await c.get("/health")
      r4 = await c.get("/health", headers={"X-Trace-Id": "a" * 300})
      return r1, r2, r3, r4

  r1, r2, r3, r4 = asyncio.run(main())
  assert r1.status_code == 200
  assert r1.json() == {"status": "healthy", "version": "1.0.0"}
  assert re.fullmatch(r"[0-9a-f]{32}", r1.headers["X-Trace-Id"])
  assert r2.headers["X-Trace-Id"] == "caller-supplied-123" # 入站透传
  assert r3.headers["X-Trace-Id"] != r1.headers["X-Trace-Id"] # 请求间隔离
  # 超长入站值不符合安全字符规则，被拒绝后重新生成（不会原样回显）
  assert re.fullmatch(r"[0-9a-f]{32}", r4.headers["X-Trace-Id"])


def test_sse_frames_byte_identical_with_middleware():
  async def main():
    async with _client(_make_app(False)) as c:
      before = await c.post("/sse", json={})
    async with _client(_make_app(True)) as c:
      after = await c.post("/sse", json={})
    return before, after

  before, after = asyncio.run(main())
  assert before.content == after.content # 逐帧字节一致，无新增/丢失帧
  assert before.content.count(b"data: ") == 5
  assert before.content.endswith(b"data: [DONE]\n\n")
  assert after.headers["X-Trace-Id"]


def test_observability_switch_off():
  with mock.patch.object(settings, "OBSERVABILITY_ENABLED", False):
    async def main():
      async with _client(_make_app(True)) as c:
        return await c.get("/health")
    r = asyncio.run(main())
    assert r.status_code == 200
    assert "x-trace-id" not in r.headers
    with _capture_events() as events:
      span_event("should.not.emit")
    assert events == []


def test_span_async_generator_covers_full_consumption():
  """复核约束 1：asyncgen 不能被当成同步函数，span 应覆盖生成器被消费的全过程。"""
  @span("unit.asyncgen", capture_args=False)
  async def gen(n):
    for i in range(n):
      await asyncio.sleep(0.01)
      yield i

  with trace_context("trace-agen"):
    with _capture_events() as events:
      async def consume():
        return [x async for x in gen(3)]
      values = asyncio.run(consume())
  assert values == [0, 1, 2]
  start = next(e for e in events if e["event"] == "span.start" and e["span"] == "unit.asyncgen")
  end = next(e for e in events if e["event"] == "span.end" and e["span"] == "unit.asyncgen")
  assert start["span_id"] == end["span_id"]
  assert end["status"] == "ok" and end["duration_ms"] >= 10


def test_middleware_resets_trace_even_if_span_event_raises():
  """第一步顺手修复：span_event 抛异常时 contextvar 仍必须复原。"""
  import app.observability.middleware as mw

  async def app_stub(scope, receive, send):
    assert get_trace_id() != ""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})

  async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}

  async def send(message):
    return None

  middleware = TraceIdMiddleware(app_stub)
  scope = {"type": "http", "headers": [], "method": "GET", "path": "/health"}

  async def main():
    assert get_trace_id() == ""
    with mock.patch.object(mw, "span_event", side_effect=RuntimeError("logger down")):
      try:
        await middleware(scope, receive, send)
      except RuntimeError:
        pass
    assert get_trace_id() == "" # 未复原时这里会残留上一次请求的 trace_id

  asyncio.run(main())
