"""Phase 2 分层容错公共件。

- concurrency_guard：按 AGENT_MAX_CONCURRENCY 限制同时在跑的 Agent 工作流。
- empty_recall_reason：空召回统一兜底标记（不让流程失败）。
不依赖任何业务分支，direct / function_call 两种模式共用。
"""
import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.config import settings
from app.observability.spans import span_event

# 每个事件循环一个信号量，避免跨 loop 绑定（测试里 asyncio.run 会创建新 loop）
_semaphores: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _semaphore() -> asyncio.Semaphore:
  loop = asyncio.get_running_loop()
  sem = _semaphores.get(loop)
  if sem is None:
    sem = asyncio.Semaphore(max(1, int(settings.AGENT_MAX_CONCURRENCY)))
    _semaphores[loop] = sem
  return sem


@asynccontextmanager
async def concurrency_guard(stage: str) -> AsyncIterator[None]:
  """并发限流：达到上限时排队，等待行为会写一条 concurrency.wait 日志。"""
  sem = _semaphore()
  if sem.locked():
    span_event("concurrency.wait", stage=stage,
               limit=settings.AGENT_MAX_CONCURRENCY)
  async with sem:
    yield


def empty_recall_reason(cases: list, reason: str = "") -> str:
  """空召回兜底：保留已有 reason；无 reason 时标记 empty_recall。"""
  if cases or not settings.AGENT_EMPTY_RECALL_FALLBACK:
    return reason
  return reason or "empty_recall"
