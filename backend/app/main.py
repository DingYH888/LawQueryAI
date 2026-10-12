"""FastAPI 应用主入口"""
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html

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

# 静态资源目录：backend/static（用于本地托管 Swagger UI 等前端资源）
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(
  title="LegalMind API",
  description="智能法律咨询系统 API",
  version="1.0.0",
  lifespan=lifespan,
  # 禁用默认的 /docs、/redoc，改由下方自定义端点提供，
  # 以解决默认 CDN（cdn.jsdelivr.net）被墙导致页面空白的问题
  docs_url=None,
  redoc_url=None,
)

# 跨域配置，允许前端访问
app.add_middleware(
  CORSMiddleware,
  allow_origins=settings.CORS_ORIGINS,
  allow_credentials=True,
  allow_methods=["*"],
  allow_headers=["*"],
)

# 挂载静态资源目录，/docs 依赖的 swagger-ui 文件从本地读取
# 先确保目录存在，避免 StaticFiles 因目录缺失启动报错
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Swagger UI 资源：优先使用本地文件（需先运行 scripts/fetch_swagger_ui.py 下载），
# 若本地缺失则回退到国内 CDN（bootcdn）
def _swagger_asset_url(filename: str, cdn_url: str) -> str:
  local_file = STATIC_DIR / "swagger-ui" / filename
  if local_file.exists():
    return f"/static/swagger-ui/{filename}"
  return cdn_url

SWAGGER_CSS_URL = _swagger_asset_url(
  "swagger-ui.css",
  "https://cdn.bootcdn.net/ajax/libs/swagger-ui/5.17.14/swagger-ui.css",
)
SWAGGER_JS_URL = _swagger_asset_url(
  "swagger-ui-bundle.js",
  "https://cdn.bootcdn.net/ajax/libs/swagger-ui/5.17.14/swagger-ui-bundle.js",
)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
  """自定义 Swagger UI 页面，静态资源走本地或国内 CDN，避免 jsdelivr 被墙"""
  return get_swagger_ui_html(
    openapi_url=app.openapi_url,
    title=app.title + " - Swagger UI",
    swagger_js_url=SWAGGER_JS_URL,
    swagger_css_url=SWAGGER_CSS_URL,
  )

from app.api import auth, cht, cases, documents
app.include_router(auth.router, prefix="/api/auth", tags=["认证"])
app.include_router(cht.router, prefix="/api/chat", tags=["聊天"])
app.include_router(cases.router, prefix="/api/cases", tags=["案例"])
app.include_router(documents.router, prefix="/api/documents", tags=["文书"])

@app.get("/health")
async def health_check():
  return {"status": "healthy", "version": "1.0.0"}

"""uv run uvicorn app.main:app --port 8000
# 启动时日志显示向量库初始化成功
# 不会出现 HuggingFace 下载提示（从本地加载）"""