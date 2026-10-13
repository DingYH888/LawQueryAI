<div align="center">

# ⚖️ LawQueryAI —— 智能法律咨询系统

**基于 LLM 大模型 · Agent · RAG · LangGraph · LangChain 构建的 AI 法律助手**

为普通用户提供 **7×24 小时** 的法律问答、案例检索与法律文书生成服务。

</div>

---

## 📖 项目简介

`LawQueryAI` 是一款面向大众的智能法律咨询系统。系统融合大语言模型（LLM）、智能体（Agent）、检索增强生成（RAG）等前沿 AI 技术，结合真实法律案例库，通过意图识别将用户诉求自动分流为**法律问答**、**案例检索**与**法律文书生成**三类场景，实现精准的法律解答与专业的文书撰写。

---

## ✨ 核心功能

| 功能模块 | 说明 |
| :--- | :--- |
| 🤖 智能法律问答 | 基于真实案例检索增强的法律咨询，支持流式对话输出 |
| 🔍 案例检索 | 混合检索（向量 + BM25 关键词）+ 重排序，支持按法院/案件类型过滤 |
| 📄 法律文书生成 | 起诉状、答辩状、合同、律师函、上诉状等文书一键生成，支持流式 |
| 👤 用户认证 | 注册 / 登录，JWT 鉴权，会话按用户隔离 |
| 💬 多会话管理 | 侧边栏会话列表，对话记忆跨轮次、跨进程持久化恢复 |

---

## 🧠 AI 能力

项目内置完整的 AI 应用编排架构：

1. **LangGraph Agent 工作流** —— 意图识别 → 案例检索 → 问答生成 / 文书生成 → 最终输出（`app/agents/workflow.py`）
2. **意图识别** —— 自动区分 `qa / search / document` 三类意图（`app/agents/intent_agent.py`）
3. **混合检索 RAG** —— 向量检索 + BM25 关键词检索多路召回，合并去重后重排序（`app/rag/retriever.py`、`reranker.py`）
4. **向量嵌入** —— 中文语义向量模型 `bge-small-zh`（本地加载，CPU 推理，`app/rag/embeddings.py`）
5. **对话记忆持久化** —— LangGraph Checkpointer（AIOMySQLSaver）按 `thread_id` 存入 MySQL（`app/llm/checkpoint.py`）
6. **大模型调用** —— 阿里云百炼 qwen 系列（OpenAI 兼容接口，`app/llm/model_client.py`）

---

## 🛠️ 技术选型

| 分类 | 技术 / 工具 | 说明 |
| :--- | :--- | :--- |
| 开发操作系统 | Windows 11 | 开发环境 |
| Python 版本 | Python 3.12 | 后端语言 |
| 项目管理工具 | uv | 高速 Python 包管理器 |
| 数据库 | MySQL 8 + Redis 7 | 关系型数据存储 + 缓存 |
| 容器化 / 基础设施 | Docker + Docker Compose | 编排本地 MySQL 8 / Redis 7 开发环境 |
| Web 框架 | FastAPI | 高性能异步 API 框架 |
| 服务器 | Uvicorn | ASGI 服务器 |
| ORM 框架 | Tortoise-ORM | 异步 ORM |
| AI 应用编排 | LangGraph + LangChain | Agent 工作流编排与链式调用 |
| 向量数据库 | ChromaDB | RAG 知识检索 |
| 大模型调用 | LangChain + 阿里云百炼 qwen | LLM 接入（OpenAI 兼容） |
| 向量嵌入 | sentence-transformers + bge-small-zh | 中文语义向量模型（本地加载） |
| 鉴权 | python-jose | JWT 认证 |
| 前端框架 | Vue 3 + Vite | 响应式前端 |
| 前端 UI | Tailwind CSS | 原子化 CSS 框架 |
| 状态管理 | Pinia + Vue Router | 状态管理与路由 |

---

## 🚀 快速开始

### 1️⃣ 克隆仓库

```bash
git clone https://github.com/DingYH888/LawQueryAI.git
cd LawQueryAI
```

### 2️⃣ 环境准备

- Python `3.12`
- Node.js `>= 18`（推荐使用 pnpm，未安装可执行 `npm install -g pnpm`）
- [uv](https://docs.astral.sh/uv/) 包管理器
- [Docker](https://www.docker.com/)（用于启动 MySQL / Redis）

### 3️⃣ 启动基础设施

```bash
docker compose up -d
```

> 启动 MySQL（映射端口 `3307`，库名 `legal_db`）与 Redis（端口 `6379`）。

### 4️⃣ 后端配置

```bash
cd backend
uv sync

# 复制并编辑环境变量文件（内容见下方「环境变量说明」）
# 至少需填写 LLM_API_KEY 等 LLM 相关配置
```

### 5️⃣ 准备 Embedding 模型

向量检索依赖中文语义向量模型，需提前放置到 `backend/models/` 目录（已加入 `.gitignore`）。应用启动时会优先从本地 `models/` 加载，本地缺失才尝试从 HuggingFace 下载：

```bash
# 目录名需与 EMBEDDING_MODEL（如 BAAI/bge-small-zh → bge-small-zh）保持一致
# 可从 ModelScope 或 HuggingFace 下载后解压到 backend/models/<模型名>/
```

### 6️⃣ 初始化数据（可选）

```bash
# 插入示例案例到 MySQL
uv run python scripts/seed_cases.py

# 将 MySQL 案例导入 ChromaDB 向量库
uv run python scripts/build_index.py
```

### 7️⃣ 启动后端

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 8️⃣ 启动前端

```bash
cd ../frontend
pnpm install
pnpm dev
```

### 9️⃣ 访问系统

- 后端 API 文档：http://localhost:8000/docs
- 后端健康检查：http://localhost:8000/health
- 前端页面：http://localhost:5173

---

## 📂 项目结构

```
LawQueryAI/
├── backend/                # 后端应用（FastAPI）
│   ├── app/
│   │   ├── main.py         # FastAPI 应用入口（自定义本地 Swagger UI）
│   │   ├── config.py       # 配置（Pydantic Settings，读取 .env）
│   │   ├── api/            # 路由层（auth/chat/cases/documents）
│   │   ├── agents/         # LangGraph Agent（意图/问答/文书/检索/工作流）
│   │   ├── rag/            # RAG（embeddings/vector_store/retriever/reranker）
│   │   ├── llm/            # LLM 客户端 / prompt / checkpoint
│   │   ├── models/         # Tortoise ORM 模型（user/case/chat/document）
│   │   ├── services/       # 业务服务（case_service）
│   │   ├── db/             # 数据库连接管理
│   │   ├── utils/          # 工具（JWT 安全）
│   │   └── dependencies.py # 依赖注入（JWT 鉴权）
│   ├── scripts/            # 数据脚本（seed_cases/build_index/fetch_swagger_ui）
│   ├── models/             # 本地 embedding 模型（gitignored）
│   ├── data/               # ChromaDB 持久化数据（gitignored）
│   ├── static/             # 本地 Swagger UI 静态资源
│   ├── .env                # 环境变量（gitignored）
│   └── pyproject.toml      # uv 项目配置
├── frontend/               # 前端应用（Vue 3 + Vite）
│   └── src/
│       ├── views/          # 页面（Home/Chat/CaseSearch/DocumentGenerate/Login/Register）
│       ├── api/            # API 封装
│       ├── stores/         # Pinia 状态管理（auth/chat）
│       ├── components/     # 通用组件
│       ├── router/         # 路由与导航守卫
│       └── main.js
├── docker-compose.yml      # MySQL + Redis 容器编排
├── LICENSE                 # MIT 许可证
└── README.md
```

---

## ⚙️ 环境变量说明

| 变量 | 说明 | 示例 |
| :--- | :--- | :--- |
| `APP_NAME` | 应用名称 | `法律咨询助手` |
| `SECRET_KEY` | JWT 签名密钥 | `secret123456` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token 有效期（分钟） | `1440` |
| `DATABASE_URL` | MySQL 连接串 | `mysql://root:123456@localhost:3307/legal_db` |
| `REDIS_URL` | Redis 连接串 | `redis://localhost:6379/0` |
| `LLM_PROVIDER` | LLM 供应商 | `openai` / `deepseek` |
| `LLM_API_KEY` | LLM API Key | `sk-...` |
| `LLM_BASE_URL` | OpenAI 兼容接口地址 | `https://.../compatible-mode/v1` |
| `LLM_MODEL` | 模型名称 | `qwen3.8-27b` |
| `LLM_TEMPERATURE` | 采样温度 | `0.7` |
| `LLM_MAX_TOKENS` | 最大生成 token 数 | `4096` |
| `EMBEDDING_PROVIDER` | 向量化供应商 | `huggingface` |
| `EMBEDDING_MODEL` | 向量模型 | `BAAI/bge-small-zh` |
| `EMBEDDING_DIMENSION` | 向量维度 | `512` |
| `VECTOR_STORE` | 向量库类型 | `chroma` |
| `VECTOR_STORE_PATH` | 向量库持久化目录 | `./data/chroma` |
| `RAG_TOP_K` | RAG 检索返回条数 | `5` |
| `RAG_SCORE_THRESHOLD` | 相似度阈值 | `0.7` |
| `RERANK_ENABLED` | 是否启用重排序 | `True` |

---

## 🔒 安全说明

- 请勿将 `.env`、密钥、数据库凭证等敏感信息提交至仓库
- `.gitignore` 已默认排除 `.env`、`.venv`、`models/`、`data/` 等敏感文件与本地资源
- 生产环境请使用环境变量或密钥管理服务注入凭证

---

## 📄 许可证

本项目仅供学习与个人使用：

- ❌ **禁止**：未经作者书面许可，不得将本项目或其衍生作品用于任何商业用途（包括但不限于对外提供有偿服务、商业部署、转售等）
- 📮 **商业授权**：如需商用，请联系作者获取授权，联系方式见仓库主页

> 注：仓库根目录 `LICENSE` 文件沿用 MIT 协议，与本页声明存在差异，请以作者最新说明为准。

---

<div align="center">

**⭐ 如果本项目对您有帮助，欢迎 Star 支持！**

Made with ❤️ by [DingYH888](https://github.com/DingYH888)

</div>