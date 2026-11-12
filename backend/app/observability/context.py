"""请求级 trace_id 上下文（Phase 0 观测基座）。

基于 contextvars：同一请求内跨 await / task 自动透传，不同请求之间天然隔离。
不依赖任何 Agent 业务代码，也不改变执行链路；请求外（脚本/后台任务）返回空串，
可用 trace_context() 或 set_trace_id() 显式绑定。"""
from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token

# 对外 HTTP 头名；ASGI header 名统一按小写字节比较
TRACE_HEADER = "X-Trace-Id"
TRACE_HEADER_BYTES = b"x-trace-id"

_trace_id_var: ContextVar[str] = ContextVar("lawquery_trace_id", default="")


def new_trace_id() -> str:
  """生成 32 位十六进制 trace_id。"""
  return uuid.uuid4().hex


def set_trace_id(trace_id: str | None = None) -> Token[str]:
  """绑定 trace_id（None 时新建），返回 Token 供 reset_trace_id 复原。"""
  return _trace_id_var.set(trace_id or new_trace_id())


def get_trace_id() -> str:
  """读取当前 trace_id；请求外返回空串，不隐式生成，避免日志出现假 trace。"""
  return _trace_id_var.get()


def reset_trace_id(token: Token[str]) -> None:
  _trace_id_var.reset(token)


@contextmanager
def trace_context(trace_id: str | None = None) -> Generator[str, None, None]:
  """供脚本 / 非 HTTP 入口使用的作用域绑定，退出后自动复原上下文。"""
  token = set_trace_id(trace_id)
  try:
    yield get_trace_id()
  finally:
    reset_trace_id(token)
