"""统一工具注册表（Phase 1）。

3 个工具的参数全部用 Pydantic ArgsSchema 约束；handler 只调用现有
RetrievalAgent / CaseService，不改 rag/* 业务逻辑。
日志侧提供 sanitize_args()：法律咨询 query 等含个人信息的参数只记录长度与哈希。
"""
import hashlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

from app.observability.spans import span
from app.services.case_service import CaseService


class ToolResult(BaseModel):
  """所有工具的统一返回/失败态。"""
  ok: bool
  data: Any = None
  error: str | None = None
  latency_ms: float = 0.0
  trace_id: str = ""
  fallback_reason: str | None = None


class HybridSearchCasesArgs(BaseModel):
  """混合检索法律案例（向量 + BM25 + 重排序）。"""
  query: str = Field(..., min_length=1, max_length=500, description="法律问题或检索关键词")
  top_k: int = Field(default=5, ge=1, le=20, description="返回案例条数")
  court: str | None = Field(default=None, max_length=200, description="按法院过滤（可选）")


class GetCaseDetailArgs(BaseModel):
  """按案例 ID 读取单条案例详情。"""
  case_id: str = Field(..., min_length=1, max_length=100, description="案例 ID")


class SearchByArticleArgs(BaseModel):
  """按法条检索相关案例。"""
  article: str = Field(..., min_length=1, max_length=200, description="法条名称或编号")
  top_k: int = Field(default=5, ge=1, le=20, description="返回案例条数")


@dataclass
class ToolContext:
  """工具执行依赖，便于工作流复用既有实例、测试注入替身。"""
  retrieval_agent: Any
  case_service: Any = None


@dataclass(frozen=True)
class ToolSpec:
  name: str
  description: str
  args_schema: type[BaseModel]
  handler: Callable[[BaseModel, ToolContext], Awaitable[Any]]


@span("tool.hybrid_search_cases", capture_args=False)
async def _hybrid_search_cases(args: HybridSearchCasesArgs, ctx: ToolContext) -> Any:
  filters = {"court": args.court} if args.court else None
  return await ctx.retrieval_agent.retrieve(
    query=args.query, top_k=args.top_k, filters=filters
   )


@span("tool.get_case_detail", capture_args=False)
async def _get_case_detail(args: GetCaseDetailArgs, ctx: ToolContext) -> Any:
  service = ctx.case_service or CaseService()
  return await service.get_by_id(args.case_id)


@span("tool.search_by_article", capture_args=False)
async def _search_by_article(args: SearchByArticleArgs, ctx: ToolContext) -> Any:
  return await ctx.retrieval_agent.search_by_article(args.article, args.top_k)


REGISTRY: dict[str, ToolSpec] = {
  "hybrid_search_cases": ToolSpec(
    name="hybrid_search_cases",
    description="混合检索与用户问题相关的法律案例，返回案例列表",
    args_schema=HybridSearchCasesArgs,
    handler=_hybrid_search_cases,
   ),
  "get_case_detail": ToolSpec(
    name="get_case_detail",
    description="按案例 ID 获取单条案例的完整详情",
    args_schema=GetCaseDetailArgs,
    handler=_get_case_detail,
   ),
  "search_by_article": ToolSpec(
    name="search_by_article",
    description="按法条名称或编号检索引用该法条的案例",
    args_schema=SearchByArticleArgs,
    handler=_search_by_article,
   ),
 }


def get_tool(name: str) -> ToolSpec | None:
  return REGISTRY.get(name)


def validate_args(spec: ToolSpec, raw_args: Any) -> BaseModel:
  """Pydantic 强校验；非法参数抛 ValidationError，由执行器分类降级。"""
  if not isinstance(raw_args, dict):
    raise ValueError("tool args must be a dict")
  return spec.args_schema.model_validate(raw_args)


def openai_tool_specs() -> list[dict]:
  """从 ArgsSchema 生成 bind_tools 所需的 OpenAI function 规格（不手写 Schema）。"""
  return [
    convert_to_openai_tool({
      "name": spec.name,
      "description": spec.description,
      "parameters": spec.args_schema.model_json_schema(),
     })
    for spec in REGISTRY.values()
   ]


_PRIVATE_KEYS = (
  "query", "article", "case_id", "court", "content",
  "password", "token", "secret", "api_key", "authorization",
 )


def sanitize_args(raw_args: Any) -> dict:
  """工具参数日志脱敏：敏感键只留长度 + 哈希，绝不写明文。"""
  if not isinstance(raw_args, dict):
    return {"value_type": type(raw_args).__name__}
  safe: dict[str, Any] = {}
  for key, value in raw_args.items():
    name = str(key)
    if any(word in name.lower() for word in _PRIVATE_KEYS):
      text = "" if value is None else str(value)
      safe[name] = {
        "redacted": True,
        "len": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
       }
    elif value is None or isinstance(value, (bool, int, float)):
      safe[name] = value
    else:
      safe[name] = f"<{type(value).__name__}>"
  return safe


def text_fingerprint(text: str) -> str:
  """节点 span 用的 query 指纹（不落明文）。"""
  return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
