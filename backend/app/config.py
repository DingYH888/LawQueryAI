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

  class Config:
    env_file = ".env"
    case_sensitive = True
    extra = "ignore" # 允许 .env 中有未定义的变量，后续章节会逐步添加


settings = Settings()
