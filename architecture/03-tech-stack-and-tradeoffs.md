# 03 — Technology Stack & Senior-Engineer Tradeoffs

> **Guiding Principle:** Every technology in this stack must earn its keep. Nothing is added purely for resume buzzwords — and nothing forces unnecessary infrastructure onto local users.

---

## 1. Two-Layer Packaging Strategy

A classic mistake in open-source Python frameworks is forcing users to install Docker, Redis, and Postgres just to run "Hello World." `runlatch` avoids this via strict **optional extras** in `pyproject.toml`:

| Layer | Install Command | External Infra Required | Target Usage |
|---|---|---|---|
| **Layer 1: Core SDK + CLI** | `pip install runlatch` | **None** (SQLite built into Python stdlib) | Local scripts, CLI workflows, GitHub Actions, unit tests, pip-installable library. |
| **Layer 2: Production Server & Workers** | `pip install "runlatch[server]"` | PostgreSQL + Redis (1-command `docker compose up`) | Event-driven webhook processing, concurrent background agents, HITL pause/resume, scheduled audits, multi-channel notifications. |

---

## 2. Core SDK Stack (Layer 1 — Zero-Infra)

| Concern | Choice | Why This Choice (and What We Rejected) |
|---|---|---|
| **Language** | **Python 3.11+** | Native `asyncio`, `StrEnum`, `tomllib` in stdlib, fast typing (`X \| Y`), and dominant ecosystem for AI/LLM tooling. |
| **Data Validation & Tool Schemas** | **Pydantic v2** | Automatically compiles typed Python function signatures into strict OpenAI/Anthropic JSON Tool Schemas and validates LLM tool arguments before execution (preventing malformed tool calls from crashing tools). |
| **HTTP Client** | **`httpx` (async)** | Clean async + sync HTTP client with timeouts, retries, and streaming SSE support for LLM APIs, GitHub REST API, and outgoing webhooks. |
| **LLM Integration** | **Thin Provider Adapters over `httpx` / official SDKs** (`openai`, `anthropic` as optional extras, or direct REST via `httpx`) | **Decision:** Use direct `httpx` adapters for OpenAI-compatible APIs (covers OpenAI, Groq, Azure OpenAI, Ollama, DeepSeek, OpenRouter in *one* adapter!) + native Anthropic & Gemini adapters. **Why not LiteLLM?** LiteLLM pulls in 40+ transitive dependencies and introduces opaque proxy errors. Our thin adapter is ~250 lines of crystal-clear code we fully control. |
| **Local & Prod Persistence (Unified)** | **SQLAlchemy 2.0 (async) + `aiosqlite` (Layer 1) / `asyncpg` (Layer 2)** | **Single storage layer for both gears.** One async `SQLAlchemyCheckpointStore` runs on zero-infra `sqlite+aiosqlite` locally and `postgresql+asyncpg` in production using dialect-aware `JSONType`/`UUIDType` shims — zero duplicate storage code. |
| **Tool Interoperability** | **Native `@tool` + MCP Client (`mcp` optional extra)** | Connects to any external Model Context Protocol (MCP) server (`stdio`/`SSE`) and wraps remote tools with `runlatch`'s `RiskTier`, `PolicyEngine`, `SecretRedactor`, and HITL approvals. |
| **CLI Framework** | **`typer` + `rich`** | Minimal boilerplate for clean subcommands (`runlatch run`, `runlatch replay`, `runlatch approve`) with readable terminal output for tool calls and diffs. |
| **Git Operations** | **Subprocess `git` wrapper (`shell=False`) + `git worktree`** | Boring, zero-dependency, works with existing SSH/credential helpers, isolates concurrent runs via ephemeral worktrees, and avoids `GitPython` memory leaks. |

---

## 3. Production Runtime Stack (Layer 2 — `runlatch[server]`)

Here is an honest senior-engineer evaluation of each production component you asked about (**FastAPI, Celery, Redis, Notifications, PostgreSQL**):

| Concern | Choice | Do We Actually Need It? (Honest Justification) |
|---|---|---|
| **API & Webhook Control Plane** | **FastAPI + Uvicorn** | **YES.** GitHub webhooks require an HTTP endpoint that verifies HMAC-SHA256 signatures and responds in `<10s` (ideally `<50ms`). FastAPI also exposes the REST API for triggering runs, inspecting logs, handling HITL Approve/Reject callbacks, and streaming live progress via **Server-Sent Events (SSE)**. |
| **Primary Database** | **PostgreSQL 16 + SQLAlchemy 2.0 (async) + Alembic** | **YES for multi-worker production.** SQLite breaks under concurrent writes from multiple Celery workers + FastAPI API requests. PostgreSQL gives ACID transactions, JSONB columns for structured tool args/outputs, row-level locking (`SELECT ... FOR UPDATE SKIP LOCKED`) for safe approval state transitions, and schema migrations via Alembic. |
| **Task Queue & Background Workers** | **Celery 5 + Redis 7** | **YES — and here is why:** LLM multi-step workflows take 30 seconds to 10+ minutes. They CANNOT run inside FastAPI request handlers (timeouts, lost work on deploy/restart, no concurrency control). **Celery + Redis** provides: <br>• Dedicated queues (`agent.runs`, `agent.notifications`, `agent.webhooks`) so slow LLM runs never starve fast notifications.<br>• Automatic retries with exponential backoff for transient LLM 429/503 errors.<br>• Hard & soft time limits per task so rogue agents cannot hang workers forever.<br>• **Celery Beat** for scheduled developer workflows (weekly dependency audits, daily stale-PR triage).<br>• Dead-letter handling for failed tasks. |
| **Cache, Locks & Pub/Sub** | **Redis 7** | **YES (already present as Celery broker!).** Reusing Redis gives us 3 high-value features with **zero extra infrastructure**:<br>1. **Webhook Idempotency Keys** (`SETEX webhook:<delivery_id> 86400 1`) to prevent duplicate GitHub retries from running an agent twice.<br>2. **Distributed Per-Repo Locks** so two agents don't simultaneously mutate the same git checkout.<br>3. **Redis Pub/Sub $\to$ FastAPI SSE** to stream real-time step/tool events from a background Celery worker directly to a user's browser or CLI! |
| **Notification Engine** | **Built-in Pluggable Dispatcher (executed on Celery `agent.notifications` queue)** | **YES — essential for async + HITL workflows.** When an autonomous agent runs in the background or pauses waiting for approval on `git_push`, nobody is staring at a terminal. A dedicated notification subsystem with provider plugins (**Slack**, **Discord**, **GitHub PR Comment**, **Generic Webhook**, **SMTP Email**), retry tracking, and delivery logs in Postgres turns `runlatch` from a script into an operational product. |
| **Observability** | **Structlog (JSON logs) + OpenTelemetry (optional)** | **YES.** Every log line carries `run_id`, `workflow_name`, `step_index`, and `trace_id`. Optional OTLP export plugs directly into Jaeger, Grafana Tempo, or Honeycomb. |
| **Containerization** | **Docker + Docker Compose** | One-command `docker compose up` spins up `api`, `worker-agents`, `worker-notifications`, `beat`, `postgres`, and `redis`. |

---

## 4. Architecture Decision Records (ADRs) — Key Tradeoffs

### ADR-01: Why Celery + Redis Instead of FastAPI `BackgroundTasks` or `pgmq`?
- **FastAPI `BackgroundTasks`:** Runs inside the web server process. If Uvicorn restarts or OOMs, every running agent vanishes with zero retry and zero visibility. **Rejected for production.**
- **Supabase `pgmq` / Postgres-backed queues:** Great when you want to avoid Redis altogether, and a great upgrade/alternative option. However, **Celery + Redis** is the universal industry standard in Python backends, gives built-in **Celery Beat** scheduling, multi-queue routing, Flower monitoring, and Redis Pub/Sub for SSE live streaming out of the box.
- **Upgrade Path (`ponytail:` note):** Our queue interface (`TaskDispatcher` protocol) is abstracted behind a 30-line protocol so `InProcessDispatcher` (local CLI), `CeleryDispatcher` (production), or a future `ARQ`/`Temporal` dispatcher can be swapped without touching agent logic.

### ADR-02: Why Not LangGraph Under the Hood?
- Building `runlatch` *on top* of LangGraph would make it just a thin wrapper and inherit LangGraph's dependency bloat and breaking changes.
- Implementing our own clean, ~400-line Python orchestration engine (`Agent` loop + `Workflow` step runner + `CheckpointStore`) means:
  - Zero magic, 100% inspectable stack traces.
  - Native support for our Risk Tier / HITL pause-resume protocol.
  - Every interviewer question about how tool calling, state persistence, and retry loops work can be answered from first principles.

### ADR-03: Async SQLAlchemy Inside Celery `prefork` Workers (Avoiding the `asyncpg` Event-Loop Crash!)
- FastAPI and our `httpx` LLM client are natively `async`, while standard Celery workers use `prefork`.
- **Critical Production Footgun:** Calling `asyncio.run(...)` across multiple tasks in the same `prefork` worker with a module-level async connection pool crashes on task #2 with `RuntimeError: Future attached to a different loop` (because `asyncpg` connections remain bound to task #1's closed loop).
- **Mandatory Solution:** Every Celery task creates a **task-scoped async engine with `poolclass=NullPool`** and disposes it in `finally` inside `asyncio.run(...)`, giving 100% shared async code across CLI, FastAPI, and Celery with zero event-loop leaks.

---

## 5. Dependency Footprint Summary

### Core (`pip install runlatch`) — Minimal Pure-Python Runtime
```toml
dependencies = [
    "pydantic>=2.8,<3.0",
    "pydantic-settings>=2.4,<3.0",
    "httpx>=0.27,<1.0",
    "sqlalchemy[asyncio]>=2.0,<3.0",
    "aiosqlite>=0.20,<1.0",
    "typer>=0.12,<1.0",
    "rich>=13.7,<14.0",
]
```

### Optional Extras (`mcp`, `server`, `observability`, `dev`)
```toml
[project.optional-dependencies]
mcp = [
    "mcp>=1.2,<2.0",
]
server = [
    "fastapi>=0.115,<1.0",
    "uvicorn[standard]>=0.30,<1.0",
    "jinja2>=3.1,<4.0",
    "asyncpg>=0.29,<1.0",
    "alembic>=1.13,<2.0",
    "celery[redis]>=5.4,<6.0",
    "redis>=5.0,<6.0",
    "structlog>=24.1,<25.0",
]
observability = [
    "opentelemetry-api>=1.26",
    "opentelemetry-sdk>=1.26",
    "opentelemetry-exporter-otlp>=1.26",
]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "respx>=0.21",
    "ruff>=0.6",
    "mypy>=1.11",
]
```
