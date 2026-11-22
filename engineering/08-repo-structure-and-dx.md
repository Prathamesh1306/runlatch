# 08 — Repository Structure, CLI & Developer Experience (DX)

This document defines the exact repository layout, CLI interface, local Docker Compose topology, and CI/CD pipeline for `github.com/Prathamesh1306/runlatch`.

---

## 1. Repository Layout

Clean `src/`-style layout keeping Layer 1 (Core SDK) completely decoupled from Layer 2 (Server/Workers):

```text
runlatch/
├── pyproject.toml                   # Package metadata, optional extras [mcp, server, observability, dev]
├── README.md                        # Quickstart, architecture diagram, badges, examples
├── START-HERE-ROADMAP.md            # Hexagonal module boundaries, AI-IDE rules & vertical build order
├── LICENSE                          # MIT License
├── CHANGELOG.md
├── .env.example                     # Sample environment variables (LLM keys, Postgres, Redis, Slack)
├── docker-compose.yml               # Full local prod stack: api (+HTMX UI), worker-agents, worker-notify, beat, pg, redis
├── Dockerfile                       # Multi-stage slim Python 3.12 image
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 0001_initial_schema.py
├── runlatch/                       # Main Python package (Strict Hexagonal Boundaries)
│   ├── __init__.py                  # Public exports: Agent, Workflow, Step, tool, RiskTier, MCPToolkit
│   ├── config.py                    # Pydantic BaseSettings + runlatch.toml loader
│   ├── core/                        # PURE DOMAIN — imports ONLY stdlib + pydantic (zero FastAPI/Celery/SQLAlchemy!)
│   │   ├── types.py                 # Message, ToolCall, RunResult, RunStatus, RunCheckpoint, WorkspaceMeta
│   │   ├── protocols.py             # LLMProvider, StepExecutor, CheckpointStore, CommandExecutor, TaskDispatcher, NotificationProvider
│   │   ├── exceptions.py            # BudgetExceededError, LoopDetectedError, PolicyDeniedError, ApprovalRequiredSignal
│   │   ├── events.py                # In-process EventBus (runtime bridges subscribe to this)
│   │   ├── redactor.py              # SecretRedactor regex + dynamic Settings scrubbing
│   │   ├── policy.py                # RiskTier policy engine & budget enforcement
│   │   ├── agent.py                 # Bounded tool-calling orchestration loop (run & resume)
│   │   └── workflow.py              # Multi-step Workflow & Step engine (supports Agent or custom StepExecutor)
│   ├── tools/                       # Tool compilation & safety primitives
│   │   ├── registry.py              # @tool decorator, Pydantic JSON Schema compiler, ToolRegistry
│   │   └── sandbox.py               # Path containment, denied globs & output truncation
│   ├── toolkits/                    # Built-in dev toolkits + MCP bridge
│   │   ├── filesystem.py            # list_files, read_file, search_code, edit_file (SEARCH/REPLACE blocks)
│   │   ├── git.py                   # git_status, git_diff, git_log, worktree create/patch-apply/commit/push
│   │   ├── shell.py                 # ShellToolkit delegating to CommandExecutor protocol
│   │   ├── github.py                # Async httpx GitHub REST API toolkit
│   │   └── mcp.py                   # MCPToolkit (connects stdio/SSE MCP servers -> PolicyEngine + HITL)
│   ├── adapters/                    # Concrete plug-in implementations of core/protocols.py
│   │   ├── llm/
│   │   │   ├── openai_compat.py     # OpenAI / Groq / Azure OpenAI / OpenRouter / Ollama adapter
│   │   │   ├── anthropic.py         # Anthropic Claude adapter
│   │   │   ├── gemini.py            # Google Gemini adapter
│   │   │   ├── pricing.py           # Token cost calculator table
│   │   │   └── mock.py              # Deterministic MockLLMProvider for $0 tests
│   │   ├── storage/
│   │   │   ├── models.py            # Unified SQLAlchemy 2.0 async ORM models (JSONType/UUIDType shim)
│   │   │   └── sqlalchemy_store.py  # Single CheckpointStore for both sqlite+aiosqlite AND postgresql+asyncpg
│   │   ├── executors/
│   │   │   ├── subprocess_exec.py   # Trusted local repos only (shell=False, strict allowlist, no raw python)
│   │   │   └── docker_exec.py       # Ephemeral Docker sandbox (--network=none --cap-drop=ALL) for untrusted PRs
│   │   └── notifications/
│   │       ├── router.py            # Event-to-channel routing + signed /ui/approvals/{id} URL builder
│   │       ├── slack.py             # Slack Block Kit webhook provider
│   │       ├── discord.py           # Discord embed webhook provider
│   │       ├── github_comment.py    # GitHub PR/Issue comment provider
│   │       ├── webhook.py           # Generic HMAC-signed JSON webhook provider
│   │       └── email.py             # Async SMTP email digest provider
│   ├── workflows/                   # Built-in flagship workflows (pure composition)
│   │   ├── pr_review.py
│   │   ├── issue_to_pr.py
│   │   ├── ci_triage.py
│   │   ├── dep_audit.py
│   │   └── doc_sync.py
│   └── runtime/                     # Delivery layers (CLI + optional Layer 2 Server/Workers)
│       ├── cli/
│       │   └── main.py              # Typer + Rich CLI (`runlatch`)
│       ├── server/                  # Layer 2: FastAPI Control Plane + HTMX UI
│       │   ├── app.py               # FastAPI app factory, lifespan, middleware
│       │   ├── auth.py              # Bearer auth + GitHub HMAC + signed approval URL verification
│       │   ├── templates/           # Zero-build Jinja2 + HTMX templates (index.html, approval_confirm.html)
│       │   └── routes/
│       │       ├── health.py
│       │       ├── runs.py          # Trigger, list, detail, SSE stream, cancel, replay
│       │       ├── approvals.py     # List & POST decision (SELECT ... FOR UPDATE)
│       │       ├── ui.py            # GET /ui (Live Run Inspector) & GET /ui/approvals/{id} (read-only confirm)
│       │       ├── webhooks.py      # GitHub webhook ingress + Redis NX idempotency
│       │       └── metrics.py       # Cost & execution analytics
│       └── workers/                 # Layer 2: Celery + Redis Workers
│           ├── celery_app.py        # Celery config, 3 queues, Beat schedule, NullPool async runner helper
│           ├── agent_tasks.py       # run_workflow_task, resume_workflow_task (stateless worktree apply)
│           ├── notification_tasks.py # dispatch_notification_task (isolated queue + 5x backoff)
│           └── maintenance_tasks.py # expire_stale_approvals_task, scheduled dep_audit
├── examples/
│   ├── 01_minimal_agent.py
│   ├── 02_mcp_governed_agent.py     # Wrapping an MCP server with PolicyEngine + HITL
│   ├── 03_hitl_approval_local.py
│   └── 04_github_pr_reviewer.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── security/
└── .github/
    └── workflows/
        ├── ci.yml                   # Ruff + Mypy + Pytest matrix (Python 3.11, 3.12)
        └── release.yml              # Tag-triggered PyPI build & publish
```

---

## 2. CLI Design (`runlatch`)

```bash
# 1. Initialize project config (runlatch.toml)
runlatch init

# 2. Run a built-in or custom workflow locally (uses SQLite, zero Docker required)
runlatch run pr-review --repo owner/repo --pr 42 --max-cost 0.15

# 3. Inspect past runs, tool calls, tokens, and USD costs
runlatch runs list
runlatch runs inspect <run_id>

# 4. Handle Human-in-the-Loop approvals directly from terminal
runlatch approvals list
runlatch approve <approval_id> --reason "Diff and tests look clean"
runlatch reject <approval_id> --reason "Don't modify auth middleware"

# 5. Replay a previous run deterministically from a checkpoint
runlatch replay <run_id> --from-step 1

# 6. Start Production Control Plane & Workers (Layer 2)
runlatch server --host 0.0.0.0 --port 8000
runlatch worker --queues agent.runs --concurrency 4
runlatch worker --queues agent.notifications --concurrency 16
runlatch beat
```

---

## 3. Production Docker Compose Topology (`docker-compose.yml`)

A single `docker compose up --build` starts the complete self-hosted system:

| Service | Command | Role |
|---|---|---|
| `postgres` | `postgres:16-alpine` | Primary state store & audit ledger |
| `redis` | `redis:7-alpine` | Celery broker, webhook idempotency, repo locks, SSE pub/sub |
| `api` | `uvicorn runlatch.server.app:create_app --factory` | FastAPI webhooks, REST API, SSE streaming |
| `worker-agents` | `celery -A runlatch.workers.celery_app worker -Q agent.runs -c 4` | Long-running LLM & tool-calling workflows |
| `worker-notifications` | `celery -A runlatch.workers.celery_app worker -Q agent.notifications -c 8` | Fast isolated Slack/Discord/GitHub/Email dispatcher |
| `beat` | `celery -A runlatch.workers.celery_app beat` | Scheduled workflows & approval expiration sweeper |

---

## 4. Minimal 15-Line Developer Experience (Quickstart Goal)

For open-source adoption, a developer must get value in **under 60 seconds**:

```python
import asyncio
from runlatch import Agent, OpenAICompatibleProvider, FileToolkit, GitToolkit

async def main():
    agent = Agent(
        name="git-summarizer",
        provider=OpenAICompatibleProvider(model="gpt-4o-mini"),
        tools=[*GitToolkit(".").tools(), *FileToolkit(".").tools()],
        max_cost_usd=0.05,
    )
    result = await agent.run("Inspect my uncommitted git diff and write a crisp PR description.")
    print(result.output)
    print(f"Tokens: {result.total_tokens} | Cost: ${result.total_cost_usd:.4f}")

asyncio.run(main())
```
