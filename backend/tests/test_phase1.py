"""Phase 1 用例：工具层 / 真实 bind_tools 形状 / 三层降级 / 结构化意图。

全程不连 MySQL、不调真实 LLM、不加载向量库；LLM 响应用替身注入。
"""
import asyncio
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import unittest.mock as mock
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import ValidationError

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
  sys.path.insert(0, str(BACKEND_DIR))

from app.agents.intent_agent import IntentAgent, IntentResult
from app.agents.tool_router import ToolRouter
from app.agents.tools.executor import FailureKind, ToolExecutor
from app.agents.tools.registry import (
  HybridSearchCasesArgs,
  ToolContext,
  openai_tool_specs,
  sanitize_args,
)
from app.config import settings
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
  def __init__(self, cases=None, fail_first=False):
    self.cases = cases if cases is not None else [{"id": "c1", "title": "案例一"}]
    self.fail_first = fail_first
    self.calls = []

  async def retrieve(self, query, top_k=5, filters=None):
    self.calls.append((query, top_k, filters))
    if self.fail_first and len(self.calls) == 1:
      raise RuntimeError("chroma down")
    return self.cases

  async def search_by_article(self, article, top_k=5):
    return self.cases


class FakeCaseService:
  async def get_by_id(self, case_id):
    return {"id": case_id, "title": "详情"}


def _ctx(retrieval=None, service=None):
  return ToolContext(retrieval_agent=retrieval or FakeRetrieval(),
                     case_service=service or FakeCaseService())


class FakeRunnable:
  def __init__(self, responses):
    self.responses = list(responses)
    self.messages_seen = []

  async def ainvoke(self, messages, config=None):
    self.messages_seen.append(list(messages))
    return self.responses.pop(0)


class FakeLLM:
  def __init__(self, responses):
    self.bound_specs = None
    self.runnable = FakeRunnable(responses)

  def bind_tools(self, specs):
    self.bound_specs = specs
    return self.runnable


def _tool_call(name, args, call_id="call_1"):
  return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(calls):
  return SimpleNamespace(tool_calls=calls, content="")


def test_registry_schemas_and_privacy():
  assert [s["function"]["name"] for s in openai_tool_specs()] == [
    "hybrid_search_cases", "get_case_detail", "search_by_article",
   ]
  assert HybridSearchCasesArgs(query="离婚", top_k=3).top_k == 3
  with pytest.raises(ValidationError):
    HybridSearchCasesArgs(query="离婚", top_k=0)
  with pytest.raises(ValidationError):
    HybridSearchCasesArgs(query="")
  safe = sanitize_args({"query": "张三与李四的离婚案", "top_k": 5, "court": "朝阳法院"})
  assert safe["top_k"] == 5
  assert "redacted" in safe["query"] and "redacted" in safe["court"]
  assert "张三" not in str(safe) and "朝阳" not in str(safe)


def test_executor_success_and_toolresult_fields():
  retrieval = FakeRetrieval()
  executor = ToolExecutor(context=_ctx(retrieval))
  with _events() as items:
    result = asyncio.run(executor.execute(
      "hybrid_search_cases", {"query": "离婚财产", "top_k": 2, "court": "某法院"}
     ))
  assert result.ok is True and result.data == retrieval.cases
  assert result.fallback_reason is None and result.latency_ms >= 0
  assert result.trace_id == ""  # 请求外（脚本/测试）不伪造 trace
  assert retrieval.calls == [("离婚财产", 2, {"court": "某法院"})]
  call = next(e for e in items if e["event"] == "tool.call")
  assert call["tool"] == "hybrid_search_cases"
  assert "离婚财产" not in str(call["params"])  # 参数脱敏


def test_executor_unknown_tool_invalid_args_and_limit():
  ex1 = ToolExecutor(context=_ctx())
  r1 = asyncio.run(ex1.execute("not_registered", {"query": "x"}))
  assert r1.ok is False and r1.fallback_reason == "tool_invalid_args"
  assert ex1.last_failure_kind == FailureKind.NON_RETRYABLE

  ex2 = ToolExecutor(context=_ctx())
  r2 = asyncio.run(ex2.execute("hybrid_search_cases", {"query": "x", "top_k": -3}))
  assert r2.ok is False and r2.fallback_reason == "tool_invalid_args"
  assert ex2.calls_made == 0

  ex3 = ToolExecutor(context=_ctx(), max_calls=1)
  assert asyncio.run(ex3.execute("hybrid_search_cases", {"query": "x"})).ok is True
  limited = asyncio.run(ex3.execute("hybrid_search_cases", {"query": "y"}))
  assert limited.ok is False and limited.error == "tool_call_limit_exceeded"


def test_router_success_binds_tools_and_backfills_toolmessage():
  retrieval = FakeRetrieval()
  llm = FakeLLM([
    _ai([_tool_call("hybrid_search_cases", {"query": "离婚", "top_k": 5})]),
    SimpleNamespace(tool_calls=[], content="已检索"),
   ])
  router = ToolRouter(llm=llm, retrieval_agent=retrieval,
                      executor=ToolExecutor(context=_ctx(retrieval), max_calls=1))
  with mock.patch.object(settings, "AGENT_TOOL_FOLLOWUP_ENABLED", True):
    with _events() as items:
      out = asyncio.run(router.retrieve("离婚财产怎么分"))
  assert llm.bound_specs is not None  # 真实走过 bind_tools
  assert [s["function"]["name"] for s in llm.bound_specs][0] == "hybrid_search_cases"
  assert out["cases"] == retrieval.cases and out["fallback_reason"] is None
  assert out["tool_calls_made"] == 1 and out["tool_names"] == ["hybrid_search_cases"]
  assert any(isinstance(m, ToolMessage) for m in llm.runnable.messages_seen[1])
  assert any(e["event"] == "tool.bind_tools" and e["bound"] for e in items)
  call_log = next(e for e in items if e["event"] == "tool.call")
  assert call_log["tool"] == "hybrid_search_cases"
  assert "离婚" not in str(call_log["params"])


@pytest.mark.parametrize("call", [
  _tool_call("not_registered", {"query": "离婚"}),
  _tool_call("hybrid_search_cases", {"query": "离婚", "top_k": -9}),
 ])
def test_router_invalid_name_or_args_falls_back_to_direct(call):
  retrieval = FakeRetrieval(cases=[{"id": "direct", "title": "直调结果"}])
  llm = FakeLLM([_ai([call])])
  router = ToolRouter(llm=llm, retrieval_agent=retrieval,
                      executor=ToolExecutor(context=_ctx(retrieval), max_calls=1))
  with _events() as items:
    out = asyncio.run(router.retrieve("离婚财产怎么分"))
  assert out["fallback_reason"] == "tool_invalid_args"
  assert out["cases"] == [{"id": "direct", "title": "直调结果"}]
  assert retrieval.calls[-1][:2] == ("离婚财产怎么分", 5)  # 代码直调 top_k=5
  fb = next(e for e in items if e["event"] == "tool.fallback")
  assert fb["fallback_reason"] == "tool_invalid_args"


def test_router_execution_exception_still_returns_answer():
  retrieval = FakeRetrieval(cases=[{"id": "direct", "title": "直调结果"}], fail_first=True)
  llm = FakeLLM([_ai([_tool_call("hybrid_search_cases", {"query": "离婚"})])])
  router = ToolRouter(llm=llm, retrieval_agent=retrieval,
                      executor=ToolExecutor(context=_ctx(retrieval), max_calls=1))
  out = asyncio.run(router.retrieve("离婚财产怎么分"))
  assert out["fallback_reason"] == "tool_invalid_args"
  assert out["cases"] == [{"id": "direct", "title": "直调结果"}]


class FakeStructured:
  def __init__(self, result=None, exc=None):
    self.result = result
    self.exc = exc
    self.calls = 0

  async def ainvoke(self, messages, config=None):
    self.calls += 1
    if self.exc:
      raise self.exc
    return self.result


class FakeIntentLLM:
  def __init__(self, structured):
    self.structured = structured
    self.wso_kwargs = None
    self.wso_calls = 0

  def with_structured_output(self, schema, method=None, include_raw=False):
    self.wso_calls += 1
    self.wso_kwargs = {"method": method, "include_raw": include_raw, "schema": schema}
    return self.structured


def _intent_agent(structured):
  agent = IntentAgent.__new__(IntentAgent) # 不触发 get_llm() / 向量库加载
  agent.llm = FakeIntentLLM(structured)
  agent.prompt = ChatPromptTemplate.from_template(INTENT_RECOGNITION_PROMPT)
  agent.chain = None
  return agent


def test_intent_structured_success_uses_json_schema_include_raw():
  agent = _intent_agent(FakeStructured({
    "raw": AIMessage(content='{"intent": "document"}'),
    "parsed": IntentResult(intent="document"), "parsing_error": None,
   }))
  with mock.patch.object(settings, "AGENT_INTENT_MODE", "structured"):
    out = asyncio.run(agent.recognize_structured("帮我写起诉状"))
  assert out == {"intent": "document", "fallback_reason": None}
  assert agent.llm.wso_kwargs["method"] == "json_schema"
  assert agent.llm.wso_kwargs["include_raw"] is True


def test_intent_parse_failure_falls_back_to_existing_whitelist():
  agent = _intent_agent(FakeStructured({
    "raw": AIMessage(content="SeArCh"), "parsed": None,
    "parsing_error": ValueError("bad json"),
   }))
  with mock.patch.object(settings, "AGENT_INTENT_MODE", "structured"), \
       mock.patch.object(settings, "AGENT_FALLBACK_ENABLED", True), _events() as items:
    out = asyncio.run(agent.recognize_structured("查找判例"))
  assert out == {"intent": "search", "fallback_reason": "intent_parse_failed"}
  fb = next(e for e in items if e["event"] == "intent.fallback")
  assert fb["fallback_reason"] == "intent_parse_failed" and fb["intent"] == "search"


def test_intent_exception_falls_back_to_qa_without_second_llm_call():
  agent = _intent_agent(FakeStructured(exc=TimeoutError("llm timeout")))
  called = {"n": 0}

  async def legacy(query):
    called["n"] += 1
    return "document"

  with mock.patch.object(settings, "AGENT_INTENT_MODE", "structured"), \
       mock.patch.object(settings, "AGENT_FALLBACK_ENABLED", True), \
       mock.patch.object(agent, "recognize", legacy):
    out = asyncio.run(agent.recognize_structured("帮我写起诉状"))
  assert out == {"intent": "qa", "fallback_reason": "intent_parse_failed"}
  assert called["n"] == 0 # Phase 2(b)：异常不再二次调用 LLM


def test_intent_legacy_mode_uses_old_recognize_only():
  agent = _intent_agent(FakeStructured())

  async def legacy(query):
    return "qa"

  with mock.patch.object(settings, "AGENT_INTENT_MODE", "legacy"), \
       mock.patch.object(agent, "recognize", legacy):
    out = asyncio.run(agent.recognize_structured("随便"))
  assert out == {"intent": "qa", "fallback_reason": None}
  assert agent.llm.wso_calls == 0
