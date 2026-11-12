"""节点 span：结构化 JSON 日志（Phase 0 观测基座）。

- @span 装饰器：同步函数 / 协程 / 异步生成器均可，自动记录 span.start / span.end、
  可见参数、耗时、状态（ok / error / cancelled），异常原样抛出（含 CancelledError）。
  异步生成器分支会让 span 覆盖「生成器被实际消费」的全过程，而不是创建即结束。
- span_event()：任意结构化事件埋点，后续阶段可传 fallback_reason、retrieval_attempts 等字段。
- start_span()：手动埋点的作用域版本。

日志只写 app.observability 命名 logger 的 stdout handler（propagate=False），
不改动 root / uvicorn 日志配置，不进入 SSE 响应体，也不包装响应流。"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import sys
import time
import uuid
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.observability.context import get_trace_id

LOGGER_NAME = "app.observability"
_SENSITIVE_KEYS = (
  "password", "passwd", "token", "api_key", "apikey",
  "secret", "authorization", "credential",
)
_REDACTED = "***"
_MAX_VALUE_LEN = 200

_logger: logging.Logger | None = None


def _clip(text: str) -> str:
  if len(text) <= _MAX_VALUE_LEN:
    return text
  return f"{text[:_MAX_VALUE_LEN]}...[+{len(text) - _MAX_VALUE_LEN} chars]"


def _is_sensitive(key: str) -> bool:
  low = key.lower()
  return any(word in low for word in _SENSITIVE_KEYS)


def _short(value: Any) -> Any:
  """把参数渲染成可安全入日志的短值：脱敏 + 截断 + 非序列化对象降级为 repr。"""
  if value is None or isinstance(value, (bool, int, float)):
    return value
  if isinstance(value, str):
    return _clip(value)
  try:
    if isinstance(value, dict):
      items = {
        str(k): (_REDACTED if _is_sensitive(str(k)) else _short(v))
        for k, v in list(value.items())[:20]
       }
      return _clip(json.dumps(items, ensure_ascii=False, default=str))
    if isinstance(value, (list, tuple, set)):
      return _clip(json.dumps([_short(v) for v in list(value)[:10]], ensure_ascii=False, default=str))
    return _clip(repr(value))
  except Exception:
    return _clip(repr(value))


def _capture_params(
  func: Callable[..., Any],
  args: tuple,
  kwargs: dict,
  capture_args: bool | Iterable[str],
) -> dict:
  if capture_args is False:
    return {}
  try:
    bound = inspect.signature(func).bind_partial(*args, **kwargs)
  except (TypeError, ValueError):
    return {"params": {"args": _short(list(args)), "kwargs": _short(kwargs)}}
  allowed = None if capture_args is True else {str(k) for k in capture_args}
  params: dict[str, Any] = {}
  for key, value in bound.arguments.items():
    if key in ("self", "cls"):
      continue
    if allowed is not None and key not in allowed:
      continue
    params[key] = _REDACTED if _is_sensitive(key) else _short(value)
  return {"params": params}


class _JsonFormatter(logging.Formatter):
  """单行 JSON：ts / level / logger / trace_id / event + 业务字段。"""

  def format(self, record: logging.LogRecord) -> str:
    payload = getattr(record, "observability", None)
    data = dict(payload) if isinstance(payload, dict) else {"message": record.getMessage()}
    data.setdefault(
      "ts",
      datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
     )
    data.setdefault("level", record.levelname.lower())
    data.setdefault("logger", record.name)
    data.setdefault("trace_id", get_trace_id())
    return json.dumps(data, ensure_ascii=False, default=str)


def _get_logger() -> logging.Logger:
  global _logger
  if _logger is None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False # 不重复进入 uvicorn / root 日志
    level = getattr(logging, str(settings.OBSERVABILITY_LOG_LEVEL).upper(), logging.INFO)
    logger.setLevel(level)
    if not any(getattr(h, "_lawquery_json", False) for h in logger.handlers):
      handler = logging.StreamHandler(sys.stdout)
      handler.setFormatter(_JsonFormatter())
      handler._lawquery_json = True # type: ignore[attr-defined]
      logger.addHandler(handler)
    _logger = logger
  return _logger


def _emit(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
  if not settings.OBSERVABILITY_ENABLED:
    return
  payload = {"event": event, **fields, "trace_id": get_trace_id()}
  _get_logger().log(level, event, extra={"observability": payload})


def span_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
  """任意结构化事件埋点。例：span_event('tool.fallback', fallback_reason='invalid_args')。"""
  _emit(event, level=level, **fields)


@contextmanager
def start_span(name: str, *, level: int = logging.INFO, **fields: Any) -> Generator[dict[str, str], None, None]:
  """手动 span 作用域；yield {"span", "span_id"}，异常一律原样抛出。"""
  span_id = uuid.uuid4().hex[:8]
  started = time.perf_counter()
  _emit("span.start", level=level, span=name, span_id=span_id, **fields)
  status = "ok"
  error = None
  try:
    yield {"span": name, "span_id": span_id}
  except asyncio.CancelledError:
    status = "cancelled"
    error = "CancelledError"
    raise
  except BaseException as exc: # 记录后原样抛出，不吞异常
    status = "error"
    error = f"{type(exc).__name__}: {exc}"
    raise
  finally:
    end_fields: dict[str, Any] = {
      "span": name,
      "span_id": span_id,
      "status": status,
      "duration_ms": round((time.perf_counter() - started) * 1000, 3),
     }
    if error:
      end_fields["error"] = error
    _emit(
      "span.end",
      level=logging.ERROR if status == "error" else level,
      **end_fields,
     )


def span(
  name: str | Callable[..., Any] | None = None,
  *,
  capture_args: bool | Iterable[str] = True,
  level: int = logging.INFO,
):
  """节点 span 装饰器。

  用法：@span / @span("case_retrieval") / @span(capture_args=["query"])。
  默认记录参数（脱敏 + 截断），capture_args=False 时不记录参数。
  """
  def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
    span_name = name if isinstance(name, str) else func.__qualname__

    if inspect.isasyncgenfunction(func):
      # 必须先于 iscoroutinefunction / 同步分支判断：
      # inspect.iscoroutinefunction(asyncgen) 为 False，会退化成同步包装导致 span 创建即结束。
      @functools.wraps(func)
      async def asyncgen_wrapper(*args: Any, **kwargs: Any) -> Any:
        fields = _capture_params(func, args, kwargs, capture_args)
        with start_span(span_name, level=level, **fields):
          async for item in func(*args, **kwargs):
            yield item
      return asyncgen_wrapper

    if inspect.iscoroutinefunction(func):
      @functools.wraps(func)
      async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        fields = _capture_params(func, args, kwargs, capture_args)
        with start_span(span_name, level=level, **fields):
          return await func(*args, **kwargs)
      return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
      fields = _capture_params(func, args, kwargs, capture_args)
      with start_span(span_name, level=level, **fields):
        return func(*args, **kwargs)
    return sync_wrapper

  if callable(name): # @span 无括号用法
    return decorate(name)
  return decorate
