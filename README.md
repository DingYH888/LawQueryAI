<div align="center">

# ⚖️ LawQueryAI（LegalMind）—— 智能法律咨询系统

**基于 LLM · LangGraph · Agent · RAG 构建的 AI 法律助手**

为普通用户提供 **7×24 小时** 的法律问答、案例检索与法律文书生成服务。

后端 <kbd>FastAPI</kbd> · <kbd>LangGraph</kbd> · <kbd>ChromaDB</kbd> · <kbd>MySQL</kbd>　前端 <kbd>Vue 3</kbd> · <kbd>Vite</kbd> · <kbd>Pinia</kbd> · <kbd>Tailwind CSS 4</kbd>

</div>

---

## 📖 项目简介

`LawQueryAI` 是一款面向大众的智能法律咨询系统（服务端应用名 `LegalMind`）。系统融合大语言模型（LLM）、智能体（Agent）、检索增强生成（RAG）等技术，结合真实法律案例库，把用户诉求自动分流为 **法律问答 / 案例检索 / 法律文书生成** 三类场景，并提供 SSE 流式输出、多会话管理与跨进程的对话记忆持久化。

除业务功能外，项目还包含一套 **Agent 工程化配套**：统一工具注册表与自写 Function Call 执行循环、结构化意图识别、分层容错（超时 / 重试 / 并发限流 / 空召回兜底）、无侵入可观测性（trace_id + JSON span 日志），以及离线回归评测与一键端到端验收脚本。

---

## ✨ 核心功能

| 功能模块 | 说明 |
| :--- | :--- |
| 🤖 智能法律问答 | 先检索案例，再把案例作为当轮 system 上下文生成回答；SSE 流式逐 token 输出 |
| 🔍 案例检索 | 向量检索 + BM25 关键词多路召回 → 合并去重 → 重排序 → 取 top_k；支持 `court` / `case_type` 参数（见「已知限制」） |
| 📄 法律文书生成 | 起诉状、答辩状、合同、律师函、上诉状五类模板，支持非流式与 SSE 流式生成，可带案例引用 |
| 👤 用户认证 | 注册 / 登录，JWT（HS256）鉴权，PBKDF2-SHA256 加盐哈希存储密码 |
| 💬 多会话管理 | 会话列表（最多 50 条）、历史消息、删除会话；会话按用户隔离，`session_id` 即 `thread_id` |
| 🧠 对话记忆 | LangGraph Checkpointer（AIOMySQLSaver）按 `thread_id` 落 MySQL，跨轮次、跨进程恢复；带历史截断防首 token 变慢 |
| 📡 可观测性 | 每个请求注入 `X-Trace-Id` 响应头，关键节点输出一行结构化 JSON span 日志（默认只记长度 / 哈希，不落用户原文） |
| 🖥️ 前端体验 | 会话级消息缓存 + `sessionStorage` 恢复、草稿会话、流式生成中可切会话 / 可中断、内心跳保活与超时自愈 |
| 🧪 评测与验收 | 弱标注回归评测脚本 + 一键端到端验收脚本 + pytest 用例（不依赖真实 LLM） |

---

## 🧠 AI 能力与执行链路

### 工作流（LangGraph StateGraph）

```text
POST /api/chat/{send|stream}
        │
        ▼
  intent_recognition ──(document)──► document_generation ──┐
        │                                                  │
        ├──(search)──► case_retrieval ──────────────────────┤
        │                                                  ▼
        └──(qa)──────► case_retrieval ──► qa_generation ──► final_output ──► END
```

1. **编排** —— 5 个节点（`intent_recognition` / `case_retrieval` / `qa_generation` / `document_generation` / `final_output`），条件边按意图路由；图在首次 `run` / `astream` 时懒编译并缓存（`app/agents/workflow.py`），因为 checkpointer 要等 lifespan 启动后才可用。
2. **意图识别（三档）** —— 见下方「意图识别模式」，默认 `keyword` 零 LLM 调用；`structured` 走 `with_structured_output(..., method="json_schema", include_raw=True)`，解析失败回落到 `strip().lower()` + 白名单；异常 / 超时不再二次调用 LLM，直接落 `qa`（`app/agents/intent_agent.py`）。
3. **混合检索 RAG** —— 向量检索（`top_k × 2`）+ BM25 关键词检索（用向量库中现有文档一次性构建）→ 合并去重（id 优先保留向量结果）→ 重排序 → 取 `top_k`（`app/rag/retriever.py`、`reranker.py`）。
4. **向量嵌入** —— 中文语义模型 `bge-small-zh`，优先从 `backend/models/` 本地加载，缺失时才尝试从 HuggingFace 下载；CPU 推理 + 向量归一化（`app/rag/embeddings.py`）。
5. **统一工具层** —— 3 个工具（`hybrid_search_cases` / `get_case_detail` / `search_by_article`）由 Pydantic `ArgsSchema` 约束，经 `bind_tools` + 自写有界执行循环调用；未知工具 / 参数校验失败 / 执行异常 / 调用超限均有明确降级策略（`app/agents/tools/`、`tool_router.py`）。
6. **对话记忆持久化** —— AIOMySQLSaver 按 `thread_id` 存入 MySQL，复用 `DATABASE_URL`，建表幂等（`app/llm/checkpoint.py`）；`QAAgent` 只带最近 `AGENT_HISTORY_MAX_MESSAGES`（默认 20）条历史，避免 checkpoint 累积导致首 token 越来越慢。
7. **大模型调用** —— 阿里云百炼 qwen 系列（OpenAI 兼容接口），超时 / 重试（`request_timeout` / `stream_chunk_timeout` / `max_retries`）全部由配置驱动（`app/llm/model_client.py`）。

> 案例只作为「当轮 system 上下文」注入，不写入 `messages`；`retrieved_cases` / `response` / `sources` 每轮覆盖，`messages` 由 `add_messages` 累积，`final_output` 只返回增量字段，防止历史重复。

### 两种检索模式（`AGENT_TOOL_MODE`）

| 模式 | 行为 | 适用 |
| :--- | :--- | :--- |
| `direct`（默认） | 节点内直接 `asyncio.wait_for(RetrievalAgent.retrieve(), RETRIEVAL_TIMEOUT)`；超时 / 异常降级为空召回并记录 `fallback_reason`，不打断请求 | 稳定链路 |
| `function_call` | LLM `bind_tools` 自主选工具 → 有界执行循环（`AGENT_MAX_TOOL_CALLS`）→ 结果回填；失败时降级为代码直调 | 演示工具调用能力 |

### 三种意图识别模式（`AGENT_INTENT_MODE`）

| 模式 | 行为 | 代价 |
| :--- | :--- | :--- |
| `keyword`（默认） | 命中「起诉状 / 起草一份 / 查判例 / 类似案例…」等明确指令词才判为 `document` / `search`，其余一律 `qa` | 零 LLM 调用，省掉一次 10~15s 串行调用 |
| `structured` | json_schema 结构化输出，兼容 `include_raw` 记录 token 用量；解析失败用 raw 内容走白名单 | 一次 LLM 调用 |
| `legacy` | 旧链路：Prompt → LLM → 字符串 → 白名单 | 一次 LLM 调用 |

---

## 🛠️ 技术选型

| 分类 | 技术 / 工具 | 说明 |
| :--- | :--- | :--- |
| 开发操作系统 | Windows 11 | 开发环境 |
| 后端语言 | Python 3.12 | `.python-version` 锁定 3.12，`requires-python >= 3.12` |
| 项目管理 | uv | 依赖声明见 `backend/pyproject.toml`，锁定见 `backend/uv.lock`（清华镜像） |
| Web 框架 | FastAPI + Uvicorn | 异步 API；`docs_url` 关闭并自定义 `/docs` 以规避 jsdelivr 不可达 |
| ORM | Tortoise-ORM（aiomysql） | 启动时 `generate_schemas(safe=True)` 自动建表 |
| AI 编排 | LangGraph + LangChain | StateGraph 工作流、`astream(stream_mode="messages")` 转发 token |
| 对话记忆 | langgraph-checkpoint-mysql（AIOMySQLSaver） | 按 `thread_id` 持久化到 MySQL |
| 向量数据库 | ChromaDB | 本地持久化（`backend/data/chroma`），集合 `legal_cases` |
| 关键词检索 | rank-bm25（BM25Retriever） | 与向量结果混合召回 |
| 向量嵌入 | sentence-transformers + bge-small-zh | 本地加载、CPU 推理、归一化，维度 512 |
| 大模型 | 阿里云百炼 qwen（OpenAI 兼容） | ChatOpenAI 接入，`openai` / `deepseek` 两种 provider |
| 数据库 | MySQL 8（+ Redis 7） | MySQL 存业务表与 checkpoint；Redis 仅在编排中声明，当前代码未使用 |
| 容器化 | Docker + Docker Compose | MySQL 映射宿主机 `3307` |
| 鉴权 | python-jose + PBKDF2-HMAC-SHA256 | JWT HS256；密码随机盐 + 10 万轮迭代 |
| 测试 | pytest + httpx | 用例集中在 `backend/tests/`，全部以 mock 隔离，不需真实 LLM |
| 前端框架 | Vue 3（`<script setup>`）+ Vite 8 | 组合式 API |
| 前端 UI | Tailwind CSS 4（`@tailwindcss/vite`） | 原子化 CSS，无 `tailwind.config.js` |
| 前端状态 / 路由 | Pinia 3 + Vue Router 5 | history 模式 + 登录守卫 |
| 前端请求 / 渲染 | axios + marked + fetch | 拦截器附加 JWT；`marked` 渲染 Markdown；SSE 用 fetch 手写流解析 |
| 前端包管理 | pnpm | 仅 `pnpm-lock.yaml`，无 npm / yarn 锁文件 |

---

## 🚀 快速开始

### 1️⃣ 克隆仓库

```bash
git clone https://github.com/DingYH888/LawQueryAI.git
cd LawQueryAI
```

### 2️⃣ 环境准备

- Python `3.12`
- Node.js `>= 20.19`（Vite 8 要求；推荐 pnpm，可用 `npm install -g pnpm` 安装）
- [uv](https://docs.astral.sh/uv/) 包管理器
- [Docker](https://www.docker.com/)（用于启动 MySQL / Redis）

### 3️⃣ 启动基础设施

```bash
docker compose up -d
```

> MySQL 映射到宿主机 `3307`（库名 `legal_db`，root 密码 `123456`），Redis 映射 `6379`。两个服务都带数据卷与 `restart: unless-stopped`。

### 4️⃣ 安装后端依赖并配置环境变量

```bash
cd backend
uv sync
```

在 `backend/` 下新建 `.env`（已被 `.gitignore` 忽略），至少填写 LLM 相关配置，完整字段见「环境变量说明」：

```dotenv
SECRET_KEY=请替换为随机长字符串
LLM_API_KEY=sk-xxxx
LLM_MODEL=qwen3.8-27b
DATABASE_URL=mysql://root:123456@localhost:3307/legal_db
# 若使用非默认兼容接口，再显式覆盖 LLM_BASE_URL
# LLM_BASE_URL=https://<你的兼容接口地址>/compatible-mode/v1
```

### 5️⃣ 准备 Embedding 模型

仓库已内置 `backend/models/bge-small-zh/`（约 92 MB，含 tokenizer 与 sentence-transformers 配置）。若该目录缺失，应用会尝试从 HuggingFace 下载；内网 / 离线环境请提前放置：

```text
backend/models/<模型名>/   # 目录名需与 EMBEDDING_MODEL 的最后一段一致
                          # 例如 BAAI/bge-small-zh → backend/models/bge-small-zh/
```

可从 ModelScope 或 HuggingFace 下载后解压到上述目录。

### 6️⃣ 初始化数据（首次运行必须）

> 向量索引 `backend/data/chroma/` 为本地生成物、不进版本控制（内置模型 `backend/models/bge-small-zh/` 随仓库提供），首次运行需先建库建索引：

```bash
# 1) 插入示例案例到 MySQL
uv run python scripts/seed_cases.py

# 2) 将 MySQL 案例导入 ChromaDB 向量库（重建索引）
uv run python scripts/build_index.py
```

### 7️⃣ 启动后端

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

> ⚠️ 端口请保持 `8000`：前端 Vite 代理固定指向 `http://localhost:8000`（`frontend/vite.config.js`），改端口需同步修改代理。

### 8️⃣ 启动前端

```bash
cd ../frontend
pnpm install
pnpm dev
```

### 9️⃣ 访问系统

| 入口 | 地址 |
| :--- | :--- |
| 前端页面 | http://localhost:5173 |
| 后端 API 文档（Swagger） | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

> `/docs` 默认使用国内 CDN（bootcdn）的 Swagger 静态资源；如需完全离线，先执行 `uv run python scripts/fetch_swagger_ui.py`，把 Swagger UI 下载到 `backend/static/swagger-ui/`，应用会自动改用本地文件。

---

## 📂 项目结构

```text
LawQueryAI/
├── backend/                          # 后端应用（FastAPI）
│   ├── app/
│   │   ├── main.py                   # 入口：lifespan（DB → checkpointer → 向量库）、CORS、trace 中间件、自定义 /docs、路由注册、/health
│   │   ├── config.py                 # Pydantic Settings（读取 backend/.env，字段见环境变量说明）
│   │   ├── dependencies.py           # JWT 鉴权依赖 get_current_user
│   │   ├── api/
│   │   │   ├── auth.py               # POST /register、/login
│   │   │   ├── cht.py                # 聊天：会话 CRUD、消息历史、SSE 流（含 10s 心跳与首帧 session_id）
│   │   │   ├── cases.py              # 案例检索 / 详情
│   │   │   └── documents.py          # 文书生成（含流式）与模板列表
│   │   ├── agents/
│   │   │   ├── workflow.py           # LangGraph 状态图编排 + run/astream
│   │   │   ├── intent_agent.py       # 意图识别：keyword / structured / legacy + 降级
│   │   │   ├── retrieval_agent.py    # 检索 + 重排序封装
│   │   │   ├── qa_agent.py           # 案例增强问答（历史截断、流式）
│   │   │   ├── document_agent.py     # 文书生成（流式 / 非流式共用 prompt 构造）
│   │   │   ├── resilience.py         # 并发限流、空召回原因标记
│   │   │   ├── tool_router.py        # Function Call 路由（bind_tools + 有界执行循环 + 三层降级）
│   │   │   └── tools/                # registry.py 工具注册表 / executor.py 执行器
│   │   ├── rag/                      # embeddings / vector_store / retriever / reranker
│   │   ├── llm/                      # model_client / prompts / checkpoint
│   │   ├── models/                   # Tortoise 模型：user / case / chat / document
│   │   ├── services/case_service.py  # 案例查询服务
│   │   ├── observability/            # context（trace_id）/ middleware / spans（JSON 日志）
│   │   ├── db/database.py            # Tortoise 初始化与连接关闭
│   │   └── utils/security.py         # JWT 签发、密码哈希校验
│   ├── scripts/
│   │   ├── seed_cases.py             # 写入示例案例到 MySQL
│   │   ├── build_index.py            # MySQL → ChromaDB 索引重建
│   │   ├── fetch_swagger_ui.py       # 下载本地 Swagger UI 静态资源（可选）
│   │   └── verify_demo.py            # 一键端到端验收（含 SSE 两次录制对比）
│   ├── evals/                        # 弱标注回归评测：build_golden.py / run_eval.py / golden.jsonl（报告为生成物）
│   ├── tests/                        # pytest 用例（观测基座 / Phase 1 / Phase 2）
│   ├── models/                       # 本地 embedding 模型目录（bge-small-zh，随仓库提供）
│   ├── data/chroma/                  # ChromaDB 持久化数据（gitignored，build_index.py 生成）
│   ├── static/                       # 静态资源挂载目录；swagger-ui/ 由脚本下载，gitignored
│   ├── pyproject.toml / uv.lock      # 依赖声明与锁定
│   ├── pytest.ini                    # testpaths = tests
│   └── .env                          # 环境变量（gitignored，需自行创建）
├── frontend/                         # 前端应用（Vue 3 + Vite）
│   ├── src/
│   │   ├── views/                    # Home / Login / Register / Chat / CaseSearch / DocumentGenerate
│   │   ├── components/chat/          # ChatSidebar / ChatMessage / ChatInput
│   │   ├── api/                      # client（axios + JWT 拦截器）/ chat / cases / documents
│   │   ├── stores/                   # Pinia：auth / chat（SSE 解析、会话缓存、流式取消）
│   │   ├── router/                   # 7 条路由与登录守卫
│   │   ├── App.vue / main.js / style.css
│   ├── public/favicon.svg
│   ├── index.html / vite.config.js / package.json / pnpm-lock.yaml
├── docker-compose.yml                # MySQL 8 + Redis 7
├── LICENSE                           # MIT
└── README.md
```

> 说明：`app/` 及其子包依赖 PEP 420 隐式命名空间包（仅 `app/agents/tools/`、`app/observability/` 显式带 `__init__.py`），从 `backend/` 目录启动即可正常导入。

---

## 🔌 API 一览

| 方法 | 路径 | 鉴权 | 说明 |
| :--- | :--- | :---: | :--- |
| POST | `/api/auth/register` | - | 注册并直接返回 JWT |
| POST | `/api/auth/login` | - | 登录返回 JWT |
| POST | `/api/chat/send` | ✅ | 非流式对话，返回 `response / intent / sources / session_id` |
| POST | `/api/chat/stream` | ✅ | SSE 流式对话（前端使用此接口） |
| GET | `/api/chat/sessions` | ✅ | 当前用户会话列表（最多 50 条，标题取首条提问前 30 字） |
| GET | `/api/chat/sessions/{session_id}/messages` | ✅ | 会话历史消息（`limit` 默认 50） |
| DELETE | `/api/chat/sessions/{session_id}` | ✅ | 删除会话（外键级联删除消息） |
| GET | `/api/cases/search` | ✅ | 案例检索，参数 `q`（必填）/ `court` / `case_type` / `limit`（1~100，默认 10） |
| GET | `/api/cases/{case_id}` | ✅ | 案例详情（不存在返回 404） |
| POST | `/api/documents/generate` | ✅ | 文书生成（非流式） |
| POST | `/api/documents/generate/stream` | ✅ | 文书生成（SSE 流式，前端使用此接口） |
| GET | `/api/documents/templates` | ✅ | 五类文书模板列表 |
| GET | `/health` | - | 健康检查，返回 `{"status":"healthy","version":"1.0.0"}` |
| GET | `/docs` | - | 自定义 Swagger UI（本地资源优先，缺失回退 bootcdn） |

> 请求体：聊天为 `{"message": "...", "session_id": null, "history": null}`（`history` 已废弃，历史统一由 checkpoint 管理）；文书为 `{"document_type","query","params","use_references"}`。

### SSE 帧格式（`/api/chat/stream`）

```text
data: {"session_id": "..."}          # 首帧：前端据此锁定会话
: ping                               # 每 10s 心跳注释帧（防代理/浏览器判空闲断连）
data: {"content": "增量 token"}      # 问答 / 文书增量内容
data: {"sources": [...]}             # 案例来源引用（可选）
data: [DONE]                         # 结束标记
```

- 响应头带 `Cache-Control: no-cache`、`X-Accel-Buffering: no`，禁用网关缓冲。
- 流式模式仅 `qa_generation` 节点产出 token；`search` / `document` 意图用最终 `response` 一次性兜底推送。
- 回答为空时不落库，并回传明确提示文案；生成中断开连接会 `aclose` LangGraph 流。

### 前端行为约定

- 路由：`/`、`/login`、`/register`、`/chat`、`/chat/:sessionId`、`/documents`、`/cases`；`/chat*` 与 `/documents` 需登录，`/cases` 页面公开（但 API 需鉴权）。
- SSE 客户端：连接上限 35s、空闲上限 120s，收到任意数据（含心跳）后放宽为空闲阈值；`[DONE]` 结束，支持主动取消。
- 会话状态：按 session 隔离缓存在 Pinia，并落 `sessionStorage`（`lawquery:chat-state:v1`），支持草稿会话与切换路由 / 刷新后恢复。

---

## ⚙️ 环境变量说明

配置文件为 `backend/.env`，字段定义见 `backend/app/config.py`（`case_sensitive=True`，未知变量被忽略）。

### 基础与认证

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `APP_NAME` | `LegalMind` | 应用名称 |
| `DEBUG` | `false` | 调试开关 |
| `CORS_ORIGINS` | `["http://localhost:5173","http://localhost:3000"]` | 允许的前端来源（JSON 数组） |
| `ALLOWED_ORIGINS` | `http://localhost:5173` | 历史遗留配置项，**当前未被使用** |
| `SECRET_KEY` | `secret-key` | JWT 签名密钥，**生产必须替换** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | Token 有效期（分钟） |

### 存储

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `DATABASE_URL` | `mysql://root:123456@localhost:3307/legal_db` | MySQL 连接串（业务表 + checkpoint 共用） |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接串，**当前代码未使用**（保留） |
| `VECTOR_STORE` | `chroma` | 向量库类型标记（未参与逻辑分支） |
| `VECTOR_STORE_PATH` | `./data/chroma` | Chroma 持久化目录（相对 `backend/`） |

### 大模型

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `LLM_PROVIDER` | `openai` | `openai`（自定义 base_url）/ `deepseek`（内置地址）；其他值启动报错 |
| `LLM_API_KEY` | 空 | API Key，**必填** |
| `LLM_MODEL` | `qwen3.8-27b` | 模型名称 |
| `LLM_TEMPERATURE` | `0.7` | 采样温度 |
| `LLM_MAX_TOKENS` | `4096` | 最大生成 token |
| `LLM_BASE_URL` | 百炼兼容地址 | OpenAI 兼容接口地址（默认指向阿里云百炼 MaaS 兼容端点） |
| `LLM_REQUEST_TIMEOUT` | `120.0` | 非流式单次请求超时（秒） |
| `LLM_STREAM_CHUNK_TIMEOUT` | `120.0` | 流式 chunk 间隔超时（秒） |
| `LLM_MAX_RETRIES` | `2` | LLM 请求重试次数 |
| `LLM_INTENT_TIMEOUT` | `60.0` | 意图识别超时（秒） |

### Embedding 与 RAG

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `EMBEDDING_PROVIDER` | `huggingface` | 向量化供应商（当前实现固定 HuggingFace） |
| `EMBEDDING_MODEL` | `BAAI/bge-small-zh` | 向量模型（最后一段用于匹配 `backend/models/` 下目录名） |
| `EMBEDDING_DIMENSION` | `512` | 向量维度 |
| `RAG_TOP_K` | `5` | 召回条数基数（向量检索取 `RAG_TOP_K × 2`） |
| `RAG_SCORE_THRESHOLD` | `0.7` | 相似度阈值，**已定义但未接线** |
| `RERANK_ENABLED` | `true` | 是否启用重排序 |

### Agent 工程化与容错

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `AGENT_TOOL_MODE` | `direct` | `direct`（稳定链路）/ `function_call`（bind_tools） |
| `AGENT_INTENT_MODE` | `keyword` | `keyword`（零 LLM）/ `structured`（json_schema）/ `legacy`（旧链路） |
| `AGENT_FALLBACK_ENABLED` | `true` | 关闭后降级异常会显式上抛（`IntentFallbackDisabled`） |
| `AGENT_MAX_TOOL_CALLS` | `1` | 单轮工具调用上限，超限仅跳过剩余调用 |
| `AGENT_TOOL_FOLLOWUP_ENABLED` | `false` | 工具结果二次回填 LLM（默认关闭以省一次调用） |
| `AGENT_HISTORY_MAX_MESSAGES` | `20` | 带入 LLM 的历史消息上限（含当前提问） |
| `AGENT_EMPTY_RECALL_FALLBACK` | `true` | 空召回时标记 `empty_recall` 并继续流程 |
| `AGENT_MAX_CONCURRENCY` | `4` | 同时在跑的工作流上限（每 event loop 一个信号量） |
| `RETRIEVAL_TIMEOUT` | `15.0` | 单次检索超时（秒） |
| `OBSERVABILITY_ENABLED` | `true` | 关闭后不写 `X-Trace-Id`、不输出 span 日志 |
| `OBSERVABILITY_LOG_LEVEL` | `INFO` | `app.observability` 日志级别 |

---

## 🧪 测试、评测与验收

> 以下命令均在 `backend/` 目录执行。

```bash
# 单元测试（pytest 只收集 tests/，全部 mock，无需 MySQL / LLM / 向量库）
uv run pytest -q

# 弱标注回归评测：先构造评测集，再跑召回 / 意图对比（需 MySQL、向量库、LLM）
uv run python evals/build_golden.py
uv run python evals/run_eval.py            # 输出 evals/report.json 与 evals/report.md
uv run python evals/run_eval.py --render-only   # 不重跑，仅按 report.json 重新渲染 md

# 一键端到端验收：docker compose → 建库 → 建索引 → 起服务 → 三类请求 → SSE 两次录制对比 → pytest
uv run python scripts/verify_demo.py       # 可用 VERIFY_PORT 指定端口，--index-twice 校验索引幂等
```

覆盖范围：

- `tests/test_observability.py` —— trace_id 上下文隔离、span JSON 结构与脱敏、异常 / 取消日志、`/health` 响应头、中间件不改变 SSE 帧字节。
- `tests/test_phase1.py` —— 工具注册表 schema 与隐私脱敏、执行器成功 / 未知工具 / 参数非法 / 调用上限、路由降级链、结构化意图与白名单兜底。
- `tests/test_phase2.py` —— 超时默认值与接线、意图超时/校验失败/取消传播、followup 开关、调用超限跳过、空召回开关、并发限流、异步生成器 span。

> ⚠️ `evals/` 为**自动弱标注、固定小样本**的回归基线（cases=3、query=6），仅用于相对回归对比，**不能据此宣称 Recall@5 / MRR 提升**；`evals/report.*`、`sse_recording.json`、`verify_demo_report.json` 均为脚本生成产物，可随时删除重建。
>
> `backend/tests/` 为唯一的测试目录（`pytest.ini` 中 `testpaths = tests`），全部用 mock 隔离外部依赖，可在无 MySQL / LLM / 向量库的环境下运行。

---

## 🧭 可观测性

- 每个 HTTP 请求生成 / 透传 `X-Trace-Id`（合法字符 `[A-Za-z0-9._:-]{1,128}`，非法值丢弃并重新生成），并写入响应头。
- 关键节点（意图识别 → 检索 → 工具绑定 / 执行 / 降级 → 问答 → 文书）输出一行 JSON span 日志，字段含 `event`、`trace_id`、`duration_ms`、`fallback_reason` 等。
- 涉及用户隐私的参数（query、case_id、article、token 等）只记录**长度 + SHA256 前 12 位**，不落明文。
- 可观测层为纯旁路：不包装响应体，SSE 逐帧时序不受影响（有回归用例保证字节一致）；`OBSERVABILITY_ENABLED=false` 时完全关闭。

---

## 🕒 最新变更（相对上一版 README 记录）

| 变更 | 位置 | 影响 |
| :--- | :--- | :--- |
| 新增 `keyword` 意图模式并设为默认（`AGENT_INTENT_MODE=keyword`） | `app/agents/intent_agent.py`、`app/config.py` | 意图识别移出关键路径，省一次 10~15s LLM 调用；`structured` / `legacy` 仍保留 |
| 新增 `AGENT_HISTORY_MAX_MESSAGES`（默认 20） | `app/agents/qa_agent.py`、`app/config.py` | 历史截断，抑制 checkpointer 累积带来的首 token 延迟增长 |
| SSE 增加 10s 心跳 `: ping` 与首帧 `session_id`；空回答不落库并回传提示；关闭网关缓冲 | `app/api/cht.py` | 长耗时生成不再被代理判空闲断连；前端可可靠锁定会话 |
| 前端 SSE 解析重写：连接 35s / 空闲 120s 双阈值、心跳计数、`[DONE]` 显式结束、取消即 `abort` | `frontend/src/api/chat.js`、`frontend/src/stores/chat.js` | 弱网 / 首 token 慢时更稳，避免误判超时 |
| 会话缓存与草稿会话（`sessionStorage`） | `frontend/src/stores/chat.js` | 切换路由 / 刷新后可恢复未完成对话 |
| `pytest` 用例 36 项全绿（观测 / Phase 1 / Phase 2） | `backend/tests/` | 不依赖真实 LLM 即可回归降级链与可观测层 |
| 仓库瘦身：移除前端脚手架死文件、早期调试脚本、评测报告快照；向量库与 IDE 工具目录改为 gitignore | `frontend/`、`backend/`、`.gitignore` | 仓库不再跟踪生成物，克隆后按 6️⃣ 重建索引 |

---

## ⚠️ 已知限制

- `RAG_SCORE_THRESHOLD` 已定义但未接线；`court` / `case_type` 过滤条件实际传入的是 `ainvoke` 的 `config`，**未作用到 Chroma 过滤**（`app/api/cases.py`）。
- 重排序为**关键词命中计数**实现，非语义重排模型；中文无空格时可能全 0，`evals` 中 hybrid 与 hybrid_rerank 排序完全一致，不主张重排收益。
- 文书生成结果**未持久化**（`GeneratedDocument` 模型已定义但未写入），刷新即丢失。
- `Redis`、`ALLOWED_ORIGINS`、`EMBEDDING_PROVIDER`、`VECTOR_STORE` 等配置项暂未参与逻辑。
- 少量死代码保留未删：`LLMClient`、`VectorStoreManager`、`CaseService.search()`、`RetrievalAgent.search_by_keywords()`。
- SSE 客户端断连后，LangGraph 节点任务可能仍执行到结束（已在 `scripts/verify_demo.py` 的 `KNOWN_LIMITATIONS` 中登记）。
- 前端 `marked` 渲染结果通过 `v-html` 直接插入（未做 DOMPurify 消毒）；`CaseSearch` 页面路由公开但接口需鉴权，未登录调用会 401。
- 案例库为少量示例数据，检索指标偏乐观，不代表线上效果。
- `frontend/dist/`、`backend/data/chroma/`、`backend/static/swagger-ui/` 属本地生成物（已 gitignore），克隆后按「快速开始 6️⃣」重建即可；`backend/models/bge-small-zh/` 随仓库提供。

---

## 🔒 安全说明

- 请勿将 `.env`、密钥、数据库凭证提交至仓库；`.gitignore` 已排除 `.env`、`.venv/`、`node_modules/`、`dist/`、`__pycache__/`、`.pytest_cache/` 等。
- `SECRET_KEY` 默认值仅用于本地开发，生产必须替换为随机长字符串，并优先使用密钥管理服务注入；`docker-compose.yml` 中的 root 口令同样仅限本地。
- 如果怀疑 API Key / `SECRET_KEY` 曾外泄，请立即轮换，并清理历史提交中的敏感内容（含 `backend/.env` 若曾被误提交）。
- 项目日志默认不记录法律咨询原文与个人信息，请勿在自定义日志中破坏该约定。

---

## 📄 许可证

仓库根目录 `LICENSE` 为 **MIT** 协议。如需将本项目用于商业场景（对外提供有偿服务、商业部署、转售等），请先与作者联系确认授权；若本页说明与 `LICENSE` 文本有差异，以 `LICENSE` 文件为准。

---

<div align="center">

**⭐ 如果本项目对您有帮助，欢迎 Star 支持！**

Made with ❤️ by [DingYH888](https://github.com/DingYH888)

</div>
