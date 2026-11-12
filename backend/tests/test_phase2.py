"""Phase 2 用例：按实测定值、超时/重试接线、followup 关闭、超限保留、降级自身兜底、并发限流。"""
import asyncio
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import unittest.mock as mock
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
  sys.path.insert(0, str(BACKEND_DIR))

from app.agents.intent_agent import IntentAgent, IntentFallbackDisabled, IntentResult
from app.agents.resilience import concurrency_guard, empty_recall_reason
from app.agents.tool_router import FallbackDisabled, ToolRouter
from app.agents.tools.executor import FailureKind, ToolExecutor
from app.agents.tools.registry import ToolContext
from app.config import Settings, settings
from app.llm.model_client import _init_llm
from app.llm.prompts import INTENT_RECOGNITION_PROMPT
from app.observability.spans import LOGGER_NAME


@contextmanager
def _events():
  class Capture(logging.Handler):
    def __init__(self):
      super().__init__(level=logging.DEBUG)
      self.items = []

    def emit(self, record):
      payload = getattr(record, "observability", None)
      if isinstance(payload, dict):
        self.items.append(dict(payload))

  logger = logging.getLogger(LOGGER_NAME)
  handler = Capture()
  old = logger.level
  logger.setLevel(logging.DEBUG)
  logger.addHandler(handler)
  try:
    yield handler.items
  finally:
    logger.removeHandler(handler)
    logger.setLevel(old)


class FakeRetrieval:
  def __init__(self, cases=None, exc=None, delay=0.0):
    self.cases = cases if cases is not None else [{"id": "c1", "title": "案例一"}]
    self.exc = exc
    self.delay = delay
    self.calls = []

  async def retrieve(self, query, top_k=5, filters=None):
    self.calls.append((query, top_k, filters))
    if self.delay:
      await asyncio.sleep(self.delay)
    if self.exc:
      raise self.exc
    return self.cases

  async def search_by_article(self, article, top_k=5):
    self.calls.append((article, top_k, None))
    return self.cases


def _ctx(retrieval):
  return ToolContext(retrieval_agent=retrieval)


class FakeRunnable:
  def __init__(self, responses):
    self.responses = list(responses)
    self.messages_seen = []

  async def ainvoke(self, messages, config=None):
    self.messages_seen.append(list(messages))
    if not self.responses:
      raise AssertionError("unexpected extra LLM call")
    return self.responses.pop(0)


class FakeLLM:
  def __init__(self, responses):
    self.runnable = FakeRunnable(responses)

  def bind_tools(self, specs):
    return self.runnable


def _tool_call(name, args, call_id="call_1"):
  return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(calls):
  return SimpleNamespace(tool_calls=calls, content="")


class FakeStructured:
  def __init__(self, result=None, exc=None):
    self.result = result
    self.exc = exc

  async def ainvoke(self, messages, config=None):
    if self.exc:
      raise self.exc
    return self.result


class SlowStructured:
  def __init__(self):
    self.calls = 0

  async def ainvoke(self, messages, config=None):
    self.calls += 1
    await asyncio.sleep(0.1)
    return {"raw": AIMessage(content='{"intent": "qa"}'),
            "parsed": IntentResult(intent="qa"), "parsing_error": None}


class CancelledStructured:
  async def ainvoke(self, messages, config=None):
    raise asyncio.CancelledError()


class FakeIntentLLM:
  def __init__(self, structured):
    self.structured = structured

  def with_structured_output(self, schema, method=None, include_raw=False):
    return self.structured


def _intent_agent(structured):
  agent = IntentAgent.__new__(IntentAgent)
  agent.llm = FakeIntentLLM(structured)
  agent.prompt = ChatPromptTemplate.from_template(INTENT_RECOGNITION_PROMPT)
  agent.chain = None
  return agent


def test_timeout_defaults_are_measured_based_not_30s():
  fields = Settings.model_fields
  assert fields["LLM_REQUEST_TIMEOUT"].default == 120.0
  assert fields["LLM_STREAM_CHUNK_TIMEOUT"].default == 120.0
  assert fields["LLM_MAX_RETRIES"].default == 2
  assert fields["LLM_INTENT_TIMEOUT"].default == 60.0
  assert fields["RETRIEVAL_TIMEOUT"].default == 15.0
  assert fields["AGENT_TOOL_FOLLOWUP_ENABLED"].default is False
  assert fields["AGENT_MAX_CONCURRENCY"].default == 4
  assert fields["AGENT_EMPTY_RECALL_FALLBACK"].default is True
  assert fields["LLM_REQUEST_TIMEOUT"].default > 30 # 不写死 30s
  assert fields["LLM_INTENT_TIMEOUT"].default > 30


def test_model_client_wires_timeout_and_retries():
  with mock.patch.object(settings, "LLM_API_KEY", "test-key"):
    llm = _init_llm()
  assert llm.request_timeout == settings.LLM_REQUEST_TIMEOUT
  assert llm.stream_chunk_timeout == settings.LLM_STREAM_CHUNK_TIMEOUT
  assert llm.max_retries == settings.LLM_MAX_RETRIES


def test_intent_timeout_falls_back_to_qa_without_second_call():
  structured = SlowStructured()
  agent = _intent_agent(structured)
  called = {"n": 0}

  async def legacy(query):
    called["n"] += 1
    return "document"

  with mock.patch.object(settings, "AGENT_INTENT_MODE", "structured"), \
       mock.patch.object(settings, "AGENT_FALLBACK_ENABLED", True), \
       mock.patch.object(settings, "LLM_INTENT_TIMEOUT", 0.01), \
       mock.patch.object(agent, "recognize", legacy):
    out = asyncio.run(agent.recognize_structured("帮我写起诉状"))
  assert out == {"intent": "qa", "fallback_reason": "intent_parse_failed"}
  assert structured.calls == 1 and called["n"] == 0


def test_intent_sdk_validation_error_falls_back_to_qa():
  """Phase 3 评测发现：SDK 层 json_schema 解析失败抛 pydantic ValidationError（ValueError 子类）。"""
  from pydantic import ValidationError
  try:
    IntentResult.model_validate_json('[{"intent": "qa"}]')
  except ValidationError as exc:
    sdk_error = exc
  agent = _intent_agent(FakeStructured(exc=sdk_error))
  with mock.patch.object(settings, "AGENT_INTENT_MODE", "structured"), \
       mock.patch.object(settings, "AGENT_FALLBACK_ENABLED", True):
    out = asyncio.run(agent.recognize_structured("随便"))
  assert out == {"intent": "qa", "fallback_reason": "intent_parse_failed"}


def test_intent_fallback_disabled_raises_sentinel():
  agent = _intent_agent(FakeStructured({
    "raw": AIMessage(content="qa"), "parsed": None, "parsing_error": None,
   }))
  with mock.patch.object(settings, "AGENT_INTENT_MODE", "structured"), \
       mock.patch.object(settings, "AGENT_FALLBACK_ENABLED", False):
    with pytest.raises(IntentFallbackDisabled):
      asyncio.run(agent.recognize_structured("随便"))


def test_intent_cancelled_error_propagates():
  agent = _intent_agent(CancelledStructured())
  with mock.patch.object(settings, "AGENT_INTENT_MODE", "structured"):
    with pytest.raises(asyncio.CancelledError):
      asyncio.run(agent.recognize_structured("随便"))


def test_router_followup_disabled_by_default():
  retrieval = FakeRetrieval()
  llm = FakeLLM([_ai([_tool_call("hybrid_search_cases", {"query": "离婚"})])])
  router = ToolRouter(llm=llm, retrieval_agent=retrieval,
                      executor=ToolExecutor(context=_ctx(retrieval), max_calls=1))
  with mock.patch.object(settings, "AGENT_TOOL_FOLLOWUP_ENABLED", False), _events() as items:
    out = asyncio.run(router.retrieve("离婚财产怎么分"))
  assert out["cases"] == retrieval.cases
  assert len(llm.runnable.messages_seen) == 1 # 只调用一次 LLM
  assert any(e["event"] == "tool.followup_skipped" for e in items)


def test_router_limit_skips_remaining_and_keeps_cases():
  retrieval = FakeRetrieval(cases=[{"id": "c1", "title": "保留结果"}])
  calls = [
    _tool_call("hybrid_search_cases", {"query": "a"}, "c1"),
    _tool_call("search_by_article", {"article": "民法典"}, "c2"),
   ]
  llm = FakeLLM([_ai(calls)])
  router = ToolRouter(llm=llm, retrieval_agent=retrieval,
                      executor=ToolExecutor(context=_ctx(retrieval), max_calls=1))
  with _events() as items:
    out = asyncio.run(router.retrieve("离婚财产怎么分"))
  assert out["cases"] == retrieval.cases
  assert out["fallback_reason"] is None # 不整单降级
  assert out["tool_calls_made"] == 1 and out["skipped_calls"] == 1
  assert retrieval.calls == [("a", 5, None)] # 只执行了第一个工具
  assert any(e["event"] == "tool.call_skipped" and e["count"] == 1 for e in items)
  assert not any("tool_call_limit_exceeded" == e.get("fallback_reason") for e in items)


def test_router_fallback_self_failure_returns_empty():
  retrieval = FakeRetrieval(exc=RuntimeError("chroma down"))
  llm = FakeLLM([_ai([_tool_call("not_registered", {"query": "x"})])])
  router = ToolRouter(llm=llm, retrieval_agent=retrieval,
                      executor=ToolExecutor(context=_ctx(retrieval), max_calls=1))
  with _events() as items:
    out = asyncio.run(router.retrieve("离婚"))
  assert out["cases"] == [] and out["fallback_reason"] == "tool_invalid_args"
  assert any(e["event"] == "tool.fallback_error" for e in items)


def test_router_fallback_propagates_cancelled_error():
  retrieval = FakeRetrieval(exc=asyncio.CancelledError())
  llm = FakeLLM([_ai([_tool_call("not_registered", {"query": "x"})])])
  router = ToolRouter(llm=llm, retrieval_agent=retrieval,
                      executor=ToolExecutor(context=_ctx(retrieval), max_calls=1))
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(router.retrieve("离婚"))


def test_router_fallback_disabled_raises_sentinel():
  retrieval = FakeRetrieval()
  llm = FakeLLM([_ai([_tool_call("not_registered", {"query": "x"})])])
  router = ToolRouter(llm=llm, retrieval_agent=retrieval,
                      executor=ToolExecutor(context=_ctx(retrieval), max_calls=1))
  with mock.patch.object(settings, "AGENT_FALLBACK_ENABLED", False):
    with pytest.raises(FallbackDisabled):
      asyncio.run(router.retrieve("离婚"))


def test_empty_recall_reason_switch():
  with mock.patch.object(settings, "AGENT_EMPTY_RECALL_FALLBACK", True):
    assert empty_recall_reason([], "") == "empty_recall"
    assert empty_recall_reason([{"id": "c1"}], "") == ""
    assert empty_recall_reason([], "tool_invalid_args") == "tool_invalid_args"
  with mock.patch.object(settings, "AGENT_EMPTY_RECALL_FALLBACK", False):
    assert empty_recall_reason([], "") == ""


def test_concurrency_guard_limits_peak():
  state = {"cur": 0, "peak": 0}

  async def worker():
    async with concurrency_guard("test"):
      state["cur"] += 1
      state["peak"] = max(state["peak"], state["cur"])
      await asyncio.sleep(0.02)
      state["cur"] -= 1

  async def main():
    await asyncio.gather(*(worker() for _ in range(4)))

  with mock.patch.object(settings, "AGENT_MAX_CONCURRENCY", 1):
    asyncio.run(main())
  assert state["peak"] == 1


def test_span_asyncgen_cancel_propagates_and_logs_cancelled():
  from app.observability.spans import span as span_decorator

  @span_decorator("unit.gen.cancel", capture_args=False)
  async def gen():
    while True:
      await asyncio.sleep(0.01)
      yield 1

  async def consume():
    async for _ in gen():
      pass

  async def main():
    task = asyncio.create_task(consume())
    await asyncio.sleep(0.03)
    task.cancel()
    try:
      await task
    except asyncio.CancelledError:
      return "cancelled"
    return "not_cancelled"

  with _events() as items:
    result = asyncio.run(main())
  assert result == "cancelled"
  ends = [e for e in items
          if e["event"] == "span.end" and e["span"] == "unit.gen.cancel"]
  assert ends and ends[0]["status"] == "cancelled"


def test_non_streaming_timeout_boundary_recorded():
  """仅记录已知边界（不改行为）：非流式文书生成没有应用层总墙钟超时，
  只受 ChatOpenAI(httpx) 的 request_timeout 约束；本用例锁定该现状。"""
  import inspect
  from app.agents.document_agent import DocumentAgent

  source = inspect.getsource(DocumentAgent.generate)
  assert "wait_for" not in source
  with mock.patch.object(settings, "LLM_API_KEY", "test-key"):
    llm = _init_llm()
  assert llm.request_timeout == settings.LLM_REQUEST_TIMEOUT


def test_executor_timeout_is_retryable():
  retrieval = FakeRetrieval(delay=0.2)
  executor = ToolExecutor(context=_ctx(retrieval))
  with mock.patch.object(settings, "RETRIEVAL_TIMEOUT", 0.01):
    result = asyncio.run(executor.execute("hybrid_search_cases", {"query": "x"}))
  assert result.ok is False
  assert result.fallback_reason == "tool_invalid_args"
  assert executor.last_failure_kind == FailureKind.RETRYABLE
