"""FastAPI 应用主入口"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings


app = FastAPI(
  title="LegalMind API",
  description="智能法律咨询系统 API",
  version="1.0.0",
)

# 跨域配置，允许前端访问
app.add_middleware(
  CORSMiddleware,
  allow_origins=settings.CORS_ORIGINS,
  allow_credentials=True,
  allow_methods=["*"],
  allow_headers=["*"],
)


@app.get("/health")
async def health_check():
  return {"status": "healthy", "version": "1.0.0"}
