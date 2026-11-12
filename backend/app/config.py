"""应用配置管理"""
from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
  APP_NAME: str = "LegalMind"
  DEBUG: bool = False
  CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:3000"]
  ALLOWED_ORIGINS: str = "http://localhost:5173"

  # 认证配置
  SECRET_KEY: str = "secret-key"
  ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

  # 数据库配置（后续章节会添加更多配置项）
  DATABASE_URL: str = "mysql://root:123456@localhost:3307/legal_db"
  REDIS_URL: str = "redis://localhost:6379/0"

    # LLM 配置
  LLM_PROVIDER: str = "openai" # openai, anthropic, deepseek
  LLM_API_KEY: str = ""
  LLM_MODEL: str = "qwen3.8-27b"
  LLM_TEMPERATURE: float = 0.7
  LLM_MAX_TOKENS: int = 4096
  # OpenAI 兼容接口地址（阿里云百炼）
  LLM_BASE_URL: str = "https://ws-iyypdocg6wq1k3e4.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

  # Embedding 配置
  EMBEDDING_PROVIDER: str = "huggingface" # openai, huggingface
  EMBEDDING_MODEL: str = "BAAI/bge-small-zh"
  EMBEDDING_DIMENSION: int = 512

  # 向量数据库配置
  VECTOR_STORE: str = "chroma" # chroma, milvus
  VECTOR_STORE_PATH: str = "./data/chroma"
  # RAG 配置
  RAG_TOP_K: int = 5
  RAG_SCORE_THRESHOLD: float = 0.7
  RERANK_ENABLED: bool = True

  # 可观测性（Phase 0 观测基座）：仅旁路观测，不改变 direct 执行链路
  OBSERVABILITY_ENABLED: bool = True # 总开关；关闭后不写 X-Trace-Id、不输出 span JSON 日志
  OBSERVABILITY_LOG_LEVEL: str = "INFO" # app.observability logger 级别

  # Agent 工程化（Phase 1）：全部可由 .env 覆盖；默认 direct 走原有稳定链路
  AGENT_TOOL_MODE: str = "direct" # direct(默认) | function_call
  # keyword(默认)：关键词短路，零 LLM 调用，省掉 10~15s 意图识别耗时
  # structured：结构化输出；legacy：旧 Prompt + 白名单
  AGENT_INTENT_MODE: str = "keyword" # keyword | structured | legacy
  AGENT_FALLBACK_ENABLED: bool = True
  AGENT_MAX_TOOL_CALLS: int = 1
  # 带入 LLM 的历史消息上限（含当前提问），防止 checkpointer 累积导致首 token 越来越慢
  AGENT_HISTORY_MAX_MESSAGES: int = 20

  # 分层容错（Phase 2）：默认值按标准 3307 实测分段耗时设定，不写死 30s
  LLM_REQUEST_TIMEOUT: float = 120.0 # 非流式整请求；实测最长生成段 40839ms
  LLM_STREAM_CHUNK_TIMEOUT: float = 120.0 # 流式 chunk 间隔；实测最大间隔 1738ms、首 token ~55436ms
  LLM_MAX_RETRIES: int = 2 # 与原 OpenAI 客户端默认一致
  LLM_INTENT_TIMEOUT: float = 60.0 # 实测意图 3893~15365ms
  RETRIEVAL_TIMEOUT: float = 15.0 # 实测检索 12~52ms（含工具执行）
  AGENT_EMPTY_RECALL_FALLBACK: bool = True
  AGENT_MAX_CONCURRENCY: int = 4
  AGENT_TOOL_FOLLOWUP_ENABLED: bool = False # followup 结果未被使用，默认关闭省一次调用


  class Config:
    env_file = ".env"
    case_sensitive = True
    extra = "ignore" # 允许 .env 中有未定义的变量，后续章节会逐步添加


settings = Settings()
