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

  class Config:
    env_file = ".env"
    case_sensitive = True
    extra = "ignore" # 允许 .env 中有未定义的变量，后续章节会逐步添加


settings = Settings()
