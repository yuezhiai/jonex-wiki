<p align="center">
  <strong>Jonex Wiki</strong><br />
  LLM-Wiki compilation and question answering for enterprise knowledge bases
</p>

<hr />
<br />

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose" />
  <img src="https://img.shields.io/badge/License-Apache_2.0-0051D8" alt="Apache License 2.0" />
</p>

<p align="center">
  English | <a href="README.zh.md">中文</a>
</p>

<p align="center">
  <a href="#overview">Overview</a> |
  <a href="#quick-start">Quick Start</a> |
  <a href="#cli-reference">CLI Reference</a> |
  <a href="#docker">Docker</a> |
  <a href="#configuration">Configuration</a> |
  <a href="#http-api">HTTP API</a> |
  <a href="#license">License</a>
</p>

<p align="center">
  <strong>
  If you find value in Jonex, please ⭐ Star the project to support us.
  </strong>
</p>

---

## Overview

Jonex Wiki is a standalone **LLM-Wiki** service. It parses documents into structured sources, compiles them into incrementally updatable Wiki bundles, and answers questions through retrieval and multi-hop traversal with traceable citations.

It does not depend on a specific frontend or platform. It can run as a standalone HTTP service or be embedded in another application as a Python package. Day-to-day usage goes through the `jonex-wiki` CLI: uploading, compiling, querying, and inspecting tasks and pages all work without writing HTTP requests by hand.

```text
Files / URLs
   │
   ▼
Unified input routing ──► Text/structured passthrough ──► SourceArtifact
   │
   └──► MinerU (PDF / Office / images) ──► SourceArtifact
                                                  │
                                                  ▼
                            Layered compilation (source understanding → page organization → publication)
                                                  │
                                                  ▼
                                       bundle / index (immutable versions)
                                                  │
                                                  ▼
                                    Query (BM25 + multi-hop traversal)
                                                  │
                                                  ▼
                                          answer + citations
```

### Features

- **Multimodal input**: `.txt`, `.md`, `.json`, `.csv`, `.xlsx` and similar formats use deterministic normalization; PDF, DOCX, PPTX, images, and HTML go through MinerU.
- **Layered compilation**: source understanding is separated from page organization, with incremental compilation, single-document recompilation, immutable bundles, and version rollback.
- **Traceable citations**: answer citations bind to specific documents, chunks, page numbers/locations, and source excerpts.
- **Retrieval**: built-in BM25 lexical retrieval, evidence handles, and multi-hop traversal; no vector database required.
- **Asynchronous tasks**: upload, parse, compile, and query are all tasks, supporting polling, cancellation, and interruption recovery.

## Quick Start

### Requirements

| Dependency | Requirement |
| --- | --- |
| Python | 3.12+ |
| [uv](https://docs.astral.sh/uv/) | Latest |
| PostgreSQL | Optional; SQLite works for local evaluation |
| LLM | Any OpenAI-compatible `/v1` service |
| MinerU | Optional; required for PDF / Office / image parsing |

### 1. Clone the repository

```bash
git clone https://github.com/yuezhiai/jonex-wiki.git
cd jonex-wiki
uv sync
```

Optionally, activate the virtual environment so `jonex-wiki` is directly available; otherwise use `uv run jonex-wiki`.

```bash
source .venv/bin/activate
```

> The examples below assume an activated virtual environment. If it is not activated, replace `jonex-wiki` with `uv run jonex-wiki`.

### 2. Configure the server

```bash
cp .env.example .env
```

Minimal local configuration (SQLite + any OpenAI-compatible LLM):

```dotenv
JONEX_WIKI_API_KEY=change-me
DATABASE_URL=sqlite+aiosqlite:///./jonex_wiki.db
JONEX_WIKI_DATA_ROOT=./.results

LLM_BINDING_HOST=https://api.openai.com/v1
LLM_BINDING_API_KEY=sk-your-key
LLM_MODEL=gpt-4o-mini

# Only needed for PDF / Office / image parsing
MINERU_API_TOKEN=
```

> `LLM_BINDING_HOST` only needs to be an OpenAI-compatible `/v1` endpoint; the bundled gateway is not required.
> If the upstream does not support the top-level `thinking` field, the engine removes it and retries automatically.

### 3. Initialize the database

```bash
uv run alembic upgrade head
```

Both SQLite and PostgreSQL are supported. For PostgreSQL, also set `DB_HOST`, `DB_PORT`, `DB_USERNAME`, `DB_PASSWORD`, `DB_NAME`, and `DB_SCHEMA`, or override everything with `DATABASE_URL`.

### 4. Start the service

```bash
uv run jonex-wiki-api
# Defaults to 0.0.0.0:8007; override with JONEX_WIKI_API_PORT
```

Configure the CLI in another terminal. `JONEX_WIKI_API_KEY` must match the value in the server `.env`:

```bash
export JONEX_WIKI_BASE_URL=http://127.0.0.1:8007
export JONEX_WIKI_API_KEY=change-me
export JONEX_WIKI_TENANT_ID=demo
```

Check the service:

```bash
jonex-wiki status
# healthz: {'ok': True}
# readyz:  {'ok': True}
```

### 5. Upload, compile, and query

Upload a Markdown file (MinerU is not required), wait for parsing, and continue waiting for the compilation automatically queued by the parser:

```bash
jonex-wiki upload ./my-doc.md --kb demo --wait --compile
```

`--compile` implies `--wait`. To upload without waiting:

```bash
jonex-wiki upload ./my-doc.md --kb demo
# uploaded: document_id=doc-... task_id=task-...
```

Compilation can also be triggered separately; if one is already running, the same task is reused:

```bash
jonex-wiki compile --kb demo --wait
```

Ask a question after compilation finishes. `query` waits for the answer by default:

```bash
jonex-wiki query "What is this project about?" --kb demo
```

The output contains the answer and citations:

```text
task=qry-... kind=query status=done

Answer:
...answer text...

Citations:
  [E1] topics/overview.md
    ...source excerpt...
```

For deeper traversal or multi-hop reasoning:

```bash
jonex-wiki query "Compare the differences between these two documents" --kb demo --thinking-mode deep
```

To submit a task first and fetch the result later:

```bash
jonex-wiki query "..." --kb demo --no-wait
# query: task_id=qry-...
jonex-wiki task qry-... --wait
```

List pages in the published Wiki:

```bash
jonex-wiki pages --kb demo
```

## CLI Reference

All commands support `--json` for script consumption. Connection options can be placed before or after the subcommand:

```bash
jonex-wiki --json pages --kb demo
jonex-wiki pages --kb demo --json
```

| Command | Purpose |
| --- | --- |
| `jonex-wiki status` | Check `/healthz` and `/readyz` |
| `jonex-wiki upload FILE --kb KB [--wait] [--compile]` | Upload a file and trigger parsing; `--compile` also waits for compilation |
| `jonex-wiki compile --kb KB [--wait]` | Trigger a full knowledge-base compilation; reuses an existing task |
| `jonex-wiki query QUESTION --kb KB [--thinking-mode fast\|standard\|deep] [--no-wait]` | Ask a question; waits for the answer by default |
| `jonex-wiki task TASK_ID [--wait]` | Inspect task status or query results |
| `jonex-wiki pages --kb KB` | List published Wiki pages |

Common options:

| Option | Description |
| --- | --- |
| `--base-url URL` | Service address; defaults to `JONEX_WIKI_BASE_URL`, otherwise `http://127.0.0.1:8007` |
| `--api-key KEY` | API key; defaults to `JONEX_WIKI_API_KEY` |
| `--tenant ID` | Tenant identifier; defaults to `JONEX_WIKI_TENANT_ID`, otherwise `default` |
| `--json` | Output raw JSON |
| `--wait-timeout SEC` | Timeout for waiting on a task, default 1800 |
| `--poll-interval SEC` | Task polling interval, default 1 |
| `--http-timeout SEC` | Per-request HTTP timeout, default 60 |

The CLI only calls the HTTP API and never reads or writes the engine database directly, so `jonex-wiki-api` must be running first. The CLI ignores `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` by default so local proxy settings do not affect access to the service address.

## Docker

```bash
cp .env.example .env
# Fill in JONEX_WIKI_API_KEY, LLM_BINDING_*, and optionally MINERU_API_TOKEN
docker compose up --build -d
```

Compose uses SQLite by default, stores data in the `data` volume at `/data/jonex-wiki`, and exposes port `8007` on the host. Use the CLI after startup:

```bash
export JONEX_WIKI_BASE_URL=http://127.0.0.1:8007
export JONEX_WIKI_API_KEY=change-me
export JONEX_WIKI_TENANT_ID=demo

jonex-wiki status
jonex-wiki upload ./my-doc.md --kb demo --wait --compile
jonex-wiki query "What is this project about?" --kb demo
```

For production, use PostgreSQL instead: set `DATABASE_URL=postgresql+asyncpg://...` in `.env` and remove or override the SQLite `DATABASE_URL` in the Compose file.

## Configuration

See [`.env.example`](.env.example) for the complete variable list. The most commonly used server variables:

| Variable | Description |
| --- | --- |
| `JONEX_WIKI_API_KEY` | Required; the `X-API-Key` validation value |
| `DATABASE_URL` | Explicit database URL; takes precedence over `DB_*` |
| `JONEX_WIKI_DATA_ROOT` | Root directory for parsing/compilation artifacts |
| `LLM_BINDING_HOST` | OpenAI-compatible `/v1` endpoint |
| `LLM_BINDING_API_KEY` | LLM API key |
| `LLM_MODEL` | Generation model name; falls back to `DEFAULT_LLM_MODEL` when missing |
| `MINERU_API_TOKEN` | MinerU cloud parsing token; required for PDF/Office/image parsing |
| `JONEX_WIKI_COMPILE_CONCURRENCY` | Page passage compilation concurrency, default 20 |
| `JONEX_WIKI_KEEP_BUNDLES` | Number of historical bundle versions retained per KB, default 2 |
| `JONEX_WIKI_API_PORT` | HTTP port, default 8007 |

CLI-side variables:

| Variable | Description |
| --- | --- |
| `JONEX_WIKI_BASE_URL` | Service address |
| `JONEX_WIKI_API_KEY` | Must match the server-side `JONEX_WIKI_API_KEY` |
| `JONEX_WIKI_TENANT_ID` | Tenant identifier, sent as `X-Tenant-ID` |

## HTTP API

The CLI is a wrapper around the following API. Integrations can call it directly; after startup, visit `http://127.0.0.1:8007/docs` for the OpenAPI documentation.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/healthz` | Process health check (unauthenticated) |
| `GET` | `/readyz` | Database connectivity check (unauthenticated) |
| `POST` | `/api/v1/kbs/{kb}/documents` | Upload a document (multipart) |
| `GET` | `/api/v1/kbs/{kb}/documents` | List documents |
| `POST` | `/api/v1/kbs/{kb}/documents/{id}/reparse` | Reparse |
| `POST` | `/api/v1/kbs/{kb}/documents/{id}/recompile` | Recompile a single document |
| `POST` | `/api/v1/kbs/{kb}/compile` | Compile the entire knowledge base |
| `GET` | `/api/v1/kbs/{kb}/versions` | List bundle versions |
| `POST` | `/api/v1/kbs/{kb}/versions/{sha}/rollback` | Roll back to a specific version |
| `POST` | `/api/v1/kbs/{kb}/query` | Asynchronous query |
| `GET` | `/api/v1/tasks/{task_id}` | Task status / result |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | Cancel a task |
| `GET` | `/api/v1/kbs/{kb}/pages` | List pages |
| `GET` | `/api/v1/kbs/{kb}/pages/{path}` | Page details |
| `GET` | `/api/v1/kbs/{kb}/claims` | List claims |

Business requests require both the `X-API-Key` and `X-Tenant-ID` headers; the CLI adds them automatically.

## License

This repository is licensed under the [Apache License 2.0](LICENSE).

---

<div align="center">

### **Jonex Wiki** — LLM-Wiki services for enterprise knowledge bases

*Compile raw documents into searchable, traceable, incrementally updatable knowledge*

<br/>

### Enjoying Jonex?

<a href="https://github.com/yuezhiai/jonex-wiki/stargazers">
  <img src="https://img.shields.io/github/stars/yuezhiai/jonex-wiki?style=social" alt="Stars"/>
</a>
<a href="https://github.com/yuezhiai/jonex-wiki/network/members">
  <img src="https://img.shields.io/github/forks/yuezhiai/jonex-wiki?style=social" alt="Forks"/>
</a>

**Click ⭐ Star to support us, thank you!**

</div>

© 2026 JONEX
