# Jonex Wiki

Jonex Wiki 是一个独立的 **LLM-Wiki** 服务：把文档解析成结构化来源，编译成可增量更新的 Wiki bundle，再通过检索与多跳遍历回答问题，并返回可追溯的引用。

它不依赖特定的前端或平台，既可以作为独立 HTTP 服务运行，也可以作为一个 Python 包嵌入其他应用。

```text
文件 / URL
   │
   ▼
统一输入路由 ──► 文本/结构化直通 ──► SourceArtifact
   │
   └──► MinerU（PDF / Office / 图片）──► SourceArtifact
                                              │
                                              ▼
                                   分层编译（来源理解 → 页面组织 → 发布）
                                              │
                                              ▼
                                   bundle / index（不可变版本）
                                              │
                                              ▼
                                   查询（BM25 + 多跳遍历）
                                              │
                                              ▼
                                      answer + citations
```

## 特性

- **多种输入**：`.txt`、`.md`、`.json`、`.csv`、`.xlsx` 等走确定性归一化；PDF、DOCX、PPTX、图片、HTML 走 MinerU。
- **分层编译**：来源理解与页面组织分离，支持增量编译、单文档重编译、不可变 bundle 与版本回滚。
- **可追溯引用**：答案中的引用绑定到具体文档、chunk、页码/位置与原文摘录。
- **检索**：内置 BM25 词法检索、证据句柄与多跳遍历；不依赖向量库。
- **异步任务**：上传、解析、编译、查询都是任务，支持轮询、取消与中断恢复。
- **可部署**：本地 SQLite 快速体验；生产可用 PostgreSQL，单实例互斥锁 + bundle GC。
- **OpenAI-compatible**：LLM 接入任意兼容 `chat/completions` 的服务；MinerU 可用云端或自托管。

## 快速开始

### 1. 环境要求

| 依赖 | 要求 |
| --- | --- |
| Python | 3.12+ |
| [uv](https://docs.astral.sh/uv/) | 最新版 |
| PostgreSQL | 可选；本地体验可直接用 SQLite |
| LLM | 任意 OpenAI-compatible `/v1` 服务 |
| MinerU | 可选；解析 PDF / Office / 图片时需要 |

### 2. 安装

```bash
git clone <this-repo>
cd jonex-wiki
uv sync
```

### 3. 配置

```bash
cp .env.example .env
```

本地最小配置（SQLite + 任意 OpenAI-compatible LLM）：

```dotenv
JONEX_WIKI_API_KEY=dev-secret
DATABASE_URL=sqlite+aiosqlite:///./jonex_wiki.db
JONEX_WIKI_DATA_ROOT=./.results

LLM_BINDING_HOST=https://api.openai.com/v1
LLM_BINDING_API_KEY=sk-your-key
LLM_MODEL=gpt-4o-mini

# 只有解析 PDF / Office / 图片时才需要
MINERU_API_TOKEN=
```

> `LLM_BINDING_HOST` 只要是 OpenAI-compatible 的 `/v1` 地址即可，不要求项目自带的网关。
> 如果上游不支持顶层 `thinking` 字段，引擎会自动去掉该字段重试。

### 4. 初始化数据库

```bash
uv run alembic upgrade head
```

SQLite 与 PostgreSQL 都支持。PostgreSQL 下请同时设置 `DB_HOST`、`DB_PORT`、`DB_USERNAME`、`DB_PASSWORD`、`DB_NAME`、`DB_SCHEMA`，或直接用 `DATABASE_URL` 覆盖。

### 5. 启动

```bash
uv run jonex-wiki-api
# 默认 0.0.0.0:8007；可用 JONEX_WIKI_API_PORT 覆盖
```

健康检查：

```bash
curl http://127.0.0.1:8007/healthz
# {"ok":true}
```

### 6. 上传与查询

所有业务接口都需要两个请求头：

- `X-API-Key`：值为 `JONEX_WIKI_API_KEY`
- `X-Tenant-ID`：调用方传入的租户标识；引擎不做用户体系

上传一个 Markdown 文件（不依赖 MinerU）：

```bash
curl -X POST http://127.0.0.1:8007/api/v1/kbs/demo/documents \
  -H "X-API-Key: dev-secret" \
  -H "X-Tenant-ID: demo" \
  -F "file=@README.md;filename=readme.md"
# {"task_id":"...","document_id":"...","status":"queued"}
```

解析完成后会自动触发编译。轮询任务：

```bash
curl http://127.0.0.1:8007/api/v1/tasks/<task_id> \
  -H "X-API-Key: dev-secret" \
  -H "X-Tenant-ID: demo"
```

也可以手动触发全库编译：

```bash
curl -X POST http://127.0.0.1:8007/api/v1/kbs/demo/compile \
  -H "X-API-Key: dev-secret" \
  -H "X-Tenant-ID: demo"
```

查询（异步）：

```bash
curl -X POST http://127.0.0.1:8007/api/v1/kbs/demo/query \
  -H "X-API-Key: dev-secret" \
  -H "X-Tenant-ID: demo" \
  -H "Content-Type: application/json" \
  -d '{"question":"这个项目是做什么的？"}'
# {"task_id":"qry-...","kind":"query"}
```

轮询同一个任务接口，`status=done` 后 `result` 中会包含 `answer` 与 `citations`。

## Docker

```bash
cp .env.example .env
# 填入 JONEX_WIKI_API_KEY、LLM_BINDING_*、可选 MINERU_API_TOKEN
docker compose up --build
```

Compose 默认使用 SQLite，数据落在 `data` volume 的 `/data/jonex-wiki`。

生产环境建议改用 PostgreSQL：在 `.env` 中设置 `DATABASE_URL=postgresql+asyncpg://...`，并移除或覆盖 compose 中的 SQLite `DATABASE_URL`。

## 配置

完整变量清单见 [`.env.example`](.env.example)。最常用的：

| 变量 | 说明 |
| --- | --- |
| `JONEX_WIKI_API_KEY` | 必填；`X-API-Key` 校验值 |
| `X-Tenant-ID` | 请求头；租户标识，必填 |
| `DATABASE_URL` | 显式数据库 URL；优先级高于 `DB_*` |
| `JONEX_WIKI_DATA_ROOT` | 解析/编译产物根目录 |
| `LLM_BINDING_HOST` | OpenAI-compatible `/v1` 地址 |
| `LLM_BINDING_API_KEY` | LLM API key |
| `LLM_MODEL` | 生成模型名；缺失时回退 `DEFAULT_LLM_MODEL` |
| `MINERU_API_TOKEN` | MinerU 云解析 token；解析 PDF/Office/图片时必填 |
| `JONEX_WIKI_COMPILE_CONCURRENCY` | 页面 passage 编译并发度，默认 20 |
| `JONEX_WIKI_KEEP_BUNDLES` | 每个 KB 保留的历史 bundle 版本数，默认 2 |
| `JONEX_WIKI_API_PORT` | HTTP 端口，默认 8007 |

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 进程健康检查（免认证） |
| `GET` | `/readyz` | 数据库连通性检查（免认证） |
| `POST` | `/api/v1/kbs/{kb}/documents` | 上传文档（multipart） |
| `GET` | `/api/v1/kbs/{kb}/documents` | 文档列表 |
| `POST` | `/api/v1/kbs/{kb}/documents/{id}/reparse` | 重新解析 |
| `POST` | `/api/v1/kbs/{kb}/documents/{id}/recompile` | 单文档重编译 |
| `POST` | `/api/v1/kbs/{kb}/compile` | 全库编译 |
| `GET` | `/api/v1/kbs/{kb}/versions` | bundle 版本列表 |
| `POST` | `/api/v1/kbs/{kb}/versions/{sha}/rollback` | 回滚到指定版本 |
| `POST` | `/api/v1/kbs/{kb}/query` | 异步查询 |
| `GET` | `/api/v1/tasks/{task_id}` | 任务状态 / 结果 |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | 取消任务 |
| `GET` | `/api/v1/kbs/{kb}/pages` | 页面列表 |
| `GET` | `/api/v1/kbs/{kb}/pages/{path}` | 页面详情 |
| `GET` | `/api/v1/kbs/{kb}/claims` | claim 列表 |

启动后可访问 `http://127.0.0.1:8007/docs` 查看 OpenAPI 文档。

## 设计约束

- **单实例**：PostgreSQL 下用 session 级 advisory lock 强制互斥；不要用 `--workers N`。SQLite 同样只适合单进程。
- **BM25 only**：dense / embedding 检索已移除，不依赖向量数据库。
- **MinerU 可选但必需**：纯文本、Markdown、JSON、CSV、XLSX 可直通；PDF、DOCX、PPTX、图片、HTML 需要 MinerU 或已解析产物。
- **认证模型**：单一 API key + `X-Tenant-ID`，没有用户/角色体系；对公网暴露时请放在反向代理、TLS 和额外的访问控制之后。
- **0.x API**：接口和产物格式仍可能变化；升级前请先备份 `JONEX_WIKI_DATA_ROOT`。

## License

Apache License 2.0，见 [LICENSE](LICENSE)。

Copyright 2026 Jonex.
