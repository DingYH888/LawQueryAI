"""FastAPI 应用主入口"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.config import settings
from app.db.database import init_db, close_db
from app.rag.vector_store import init_vector_store
from app.llm.checkpoint import init_checkpointer, close_checkpointer

@asynccontextmanager
async def lifespan(app: FastAPI):
  """应用生命周期：启动时连数据库 + 初始化 checkpointer + 向量库，关闭时逐一释放"""
  await init_db()
  await init_checkpointer()
  await init_vector_store()
  yield
  await close_db()
  await close_checkpointer()

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

from app.api import auth
app.include_router(auth.router, prefix="/api/auth", tags=["认证"])

@app.get("/health")
async def health_check():
  return {"status": "healthy", "version": "1.0.0"}

"""uv run uvicorn app.main:app --port 8000
# 启动时日志显示向量库初始化成功
# 不会出现 HuggingFace 下载提示（从本地加载）"""