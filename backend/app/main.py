"""FastAPI 应用主入口"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.config import settings
from app.db.database import init_db, close_db

@asynccontextmanager
async def lifespan(app: FastAPI):
  """应用生命周期：启动时连数据库，关闭时断开"""
  await init_db()
  yield
  await close_db()

app = FastAPI(
  title="LegalMind API",
  description="智能法律咨询系统 API",
  version="1.0.0",
  lifespan=lifespan,
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
