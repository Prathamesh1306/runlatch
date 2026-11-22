# 09 — Engineering Roadmap & Granular Task Breakdown

This is the complete, execution-ready engineering backlog to build `runlatch` from scratch to a production-grade v1.0 open-source release.

> **Note:** For the strict hexagonal module boundaries, AI-IDE coding guardrails, and exact vertical build order (without artificial day estimates), start with [`START-HERE-ROADMAP.md`](../START-HERE-ROADMAP.md). This document serves as the granular ticket checklist (`ENG-001` through `ENG-030`).

---

## Milestone Overview (Dependency-Ordered Slices)

| Slice | Focus | Deliverable |
|---|---|---|
| **Slice 1** | Bootstrap & Core Agent Loop | Hexagonal packaging, `core/` types & protocols, `@tool`, `OpenAICompatibleProvider` (with Ollama/Groq/GitHub Models presets) + `MockLLMProvider`, bounded ReAct loop |
| **Slice 2** | Safety, Unified Storage & Workflows | `SecretRedactor`, `PolicyEngine`, unified async SQLAlchemy (`sqlite+aiosqlite` / `postgresql+asyncpg`), `Workflow` + `StepExecutor` |
| **Slice 3** | Dev Toolkits, Local Sandbox & MCP | `FileToolkit` (`SEARCH/REPLACE` `edit_file`), `GitToolkit` (`worktree` + `git add -N .` patch capture), `SubprocessExecutor`, `GitHubToolkit`, `MCPToolkit` |
| **Slice 4** | Flagship Workflows & CLI (**`v0.1.0`**) | `pr-review`, `issue-to-pr`, Typer + Rich CLI with `$0` `--demo` mode, runnable examples, CI (zero-Docker release) |
| **Slice 5** | FastAPI Control Plane & HTMX UI | Alembic migrations (with `workspace_patch`), REST + SSE, GitHub HMAC webhooks, `SELECT ... FOR UPDATE` approvals, zero-build HTMX Run Inspector & safe confirmation page |
| **Slice 6** | Celery Workers, Docker Sandbox & Notifications (**`v0.2.0`**) | 3-queue Celery + Redis (`NullPool` async safety), `DockerSandboxExecutor` (`--network=none`), stateless worktree resume, Notification Engine, Docker Compose |
| **Slice 7** | Full Workflows, Extra LLMs & Launch (**`v1.0.0`**) | `ci-triage`, `dep-audit`, `doc-sync`, Anthropic/Gemini adapters, OpenTelemetry, security fuzz suite, PyPI release |

---

## Phase 0: Project Bootstrap & Core Types

- **`ENG-001` — Initialize Package & Optional Extras**
  - **Files:** `pyproject.toml`, `LICENSE` (MIT), `.gitignore`, `.env.example`, `runlatch/__init__.py`
  - **AC:** `pip install -e ".[dev]"` succeeds with only 4 core deps; `pip install -e ".[server,dev]"` installs FastAPI/Celery/SQLAlchemy/Redis cleanly.
- **`ENG-002` — Core Domain Models & Exceptions**
  - **Files:** `runlatch/types.py`, `runlatch/exceptions.py`, `runlatch/config.py`
  - **AC:** Pydantic v2 models for `Message`, `ToolCall`, `ToolResult`, `RunStatus`, `RiskTier`, `RunResult`, `RunCheckpoint`, `BudgetConfig`; typed exceptions (`BudgetExceededError`, `LoopDetectedError`, `PolicyDeniedError`, `ToolValidationError`, `ApprovalRequiredSignal`).
- **`ENG-003` — CI Pipeline Setup**
  - **Files:** `.github/workflows/ci.yml`
  - **AC:** Runs `ruff check`, `mypy runlatch`, and `pytest tests/unit` on Python 3.11 & 3.12.

---

## Phase 1: Core SDK — Tool Registry, LLM Adapters & Orchestration Loop

- **`ENG-004` — `@tool` Decorator & Pydantic JSON Schema Compiler**
  - **Files:** `runlatch/tools/registry.py`, `tests/unit/test_tools.py`
  - **AC:** Supports both sync and async functions; extracts JSON Schema from type annotations + docstrings; validates incoming LLM args via Pydantic and returns clean error strings on validation failure; enforces `timeout_s` and output truncation.
- **`ENG-005` — Unified LLM Provider Protocol & Pricing Table**
  - **Files:** `runlatch/llm/base.py`, `runlatch/llm/pricing.py`, `runlatch/llm/mock.py`
  - **AC:** Normalized `LLMResponse` with `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`; `MockLLMProvider` supports scripted multi-turn tool call sequences.
- **`ENG-006` — OpenAI-Compatible Provider Adapter (OpenAI / Groq / Azure / Ollama)**
  - **Files:** `runlatch/llm/openai_compat.py`, `tests/unit/test_llm_openai.py`
  - **AC:** Direct async `httpx` implementation supporting function calling, custom `base_url`, retry with exponential backoff on HTTP `429`/`5xx`, verified via `respx` HTTP mocks.
- **`ENG-007` — Anthropic & Gemini Provider Adapters**
  - **Files:** `runlatch/llm/anthropic.py`, `runlatch/llm/gemini.py`, `tests/unit/test_llm_providers.py`
  - **AC:** Translates `ToolSpec` and `tool_calls` cleanly into Anthropic `tool_use`/`tool_result` and Gemini `functionDeclarations`/`functionCall`.
- **`ENG-008` — Agent Tool-Calling Orchestration Loop**
  - **Files:** `runlatch/agent.py`, `tests/unit/test_agent_loop.py`
  - **AC:**
    - Runs multi-turn tool-calling loop until final answer or halt condition.
    - Executes concurrent read-only tool calls with `asyncio.gather()`.
    - Enforces `max_steps`, `max_tokens`, and `max_cost_usd` before every LLM call.
    - Detects duplicate `(tool_name, args_hash)` loops (warn on 2nd, abort with `LoopDetectedError` on 3rd).

---

## Phase 2: Workflows, Safety Guardrails, Checkpointing & Dev Toolkits

- **`ENG-009` — Secret Redaction Engine**
  - **Files:** `runlatch/security/redactor.py`, `tests/security/test_redactor.py`
  - **AC:** Masks OpenAI/Anthropic/Groq/GitHub/AWS tokens, PEM blocks, Bearer headers, and dynamic runtime secrets across strings and nested dicts.
- **`ENG-010` — Policy Engine & Risk Tier Enforcement**
  - **Files:** `runlatch/policy.py`, `tests/unit/test_policy.py`
  - **AC:** Evaluates `ALLOW`, `REQUIRE_APPROVAL`, and `DENY` based on `RiskTier`, explicit overrides, and `runlatch.toml` rules.
- **`ENG-011` — Local SQLite CheckpointStore & Pause/Resume Execution**
  - **Files:** `runlatch/storage/base.py`, `runlatch/storage/sqlite_store.py`, `tests/integration/test_hitl_resume.py`
  - **AC:** Agent pauses cleanly when `PolicyEngine` returns `REQUIRE_APPROVAL`, persists full message transcript + pending tool call to SQLite, and `agent.resume(run_id, approved=True)` completes execution seamlessly; `approved=False` feeds rejection reason back to the LLM.
- **`ENG-012` — Multi-Step `Workflow` & `Step` Orchestrator**
  - **Files:** `runlatch/workflow.py`, `runlatch/events.py`, `tests/unit/test_workflow.py`
  - **AC:** Sequential + conditional steps, shared `WorkflowContext`, global budget pooling across steps, step-level retries, checkpointing after each step, and `EventBus` lifecycle emissions.
- **`ENG-013` — Built-In Filesystem & Git Toolkits**
  - **Files:** `runlatch/tools/sandbox.py`, `runlatch/toolkits/filesystem.py`, `runlatch/toolkits/git.py`, `tests/security/test_sandbox.py`
  - **AC:** Path traversal & symlink escapes blocked; denied globs (`.env`, `.git/config`) enforced; `apply_patch` applies unified diffs safely; `git_*` tools run with `shell=False`.
- **`ENG-014` — Built-In Shell & GitHub Toolkits**
  - **Files:** `runlatch/toolkits/shell.py`, `runlatch/toolkits/github.py`, `tests/unit/test_toolkits.py`
  - **AC:** `ShellToolkit` enforces binary allowlist + timeout + stripped child env; `GitHubToolkit` fetches PR diffs, issues, workflow logs, and posts comments/PRs via async `httpx`.

---

## Phase 3: CLI & Flagship Developer Workflows

- **`ENG-015` — Built-In Workflows (`pr-review`, `ci-triage`, `issue-to-pr`, `dep-audit`)**
  - **Files:** `runlatch/workflows/pr_review.py`, `ci_triage.py`, `issue_to_pr.py`, `dep_audit.py`
  - **AC:** Each workflow is composable, uses structured outputs, enforces budget limits, and has an integration test using `MockLLMProvider`.
- **`ENG-016` — Typer + Rich CLI**
  - **Files:** `runlatch/cli/main.py`
  - **AC:** Commands `init`, `run`, `runs list`, `runs inspect`, `approvals list`, `approve`, `reject`, `replay` work locally with formatted Rich tables and diff previews.
- **`ENG-017` — Runnable Examples**
  - **Files:** `examples/01_minimal_agent.py` through `examples/04_github_pr_reviewer.py`
  - **AC:** Every example executes cleanly and documents a core framework feature.

---

## Phase 4: Production Control Plane (FastAPI + PostgreSQL)

- **`ENG-018` — SQLAlchemy 2.0 Async Models & Alembic Initial Migration**
  - **Files:** `runlatch/storage/models.py`, `runlatch/storage/postgres_store.py`, `alembic/versions/0001_initial_schema.py`
  - **AC:** Implements all 6 tables (`workflow_runs`, `run_steps`, `tool_calls`, `approval_requests`, `notification_deliveries`, `webhook_events`) with indexes and JSONB columns; `alembic upgrade head` passes cleanly on Postgres 16.
- **`ENG-019` — FastAPI App Factory, Auth & Health Probes**
  - **Files:** `runlatch/server/app.py`, `runlatch/server/auth.py`, `runlatch/server/routes/health.py`
  - **AC:** Bearer auth middleware, structured request-ID logging, `/health/live` and `/health/ready` (checking Postgres + Redis connectivity).
- **`ENG-020` — Runs & Metrics REST Endpoints + SSE Streaming**
  - **Files:** `runlatch/server/routes/runs.py`, `runlatch/server/routes/metrics.py`
  - **AC:** `POST /runs` returns `202` and enqueues Celery task; `GET /runs/{id}/stream` streams real-time events via Redis Pub/Sub; `/metrics/summary` returns token/cost/success aggregates.
- **`ENG-021` — Approval Endpoints & Atomic State Locking**
  - **Files:** `runlatch/server/routes/approvals.py`
  - **AC:** Validates signed HMAC URLs or Bearer token; uses `SELECT ... FOR UPDATE` to prevent double-resume race conditions; dispatches `resume_workflow_task`.
- **`ENG-022` — GitHub Webhook Ingress with HMAC + Redis Idempotency**
  - **Files:** `runlatch/server/routes/webhooks.py`, `tests/integration/test_webhooks.py`
  - **AC:** Verifies `X-Hub-Signature-256`; deduplicates via Redis `SET NX EX` on `X-GitHub-Delivery`; routes `pull_request`, `issues`, and `workflow_run` events to matching workflows in `<50ms`.

---

## Phase 5: Celery Workers, Redis Locks & Notification Engine

- **`ENG-023` — Celery Application & Multi-Queue Topology**
  - **Files:** `runlatch/workers/celery_app.py`, `runlatch/workers/agent_tasks.py`, `runlatch/workers/maintenance_tasks.py`
  - **AC:** Configures `agent.runs`, `agent.notifications`, `agent.maintenance` queues; sets `task_acks_late=True`, `worker_prefetch_multiplier=1`, soft/hard time limits, Redis repo lock acquisition, and Celery Beat schedules (approval sweeper every 5m + weekly `dep-audit`).
- **`ENG-024` — Pluggable Notification Engine & Router**
  - **Files:** `runlatch/notifications/base.py`, `runlatch/notifications/router.py`, `runlatch/workers/notification_tasks.py`
  - **AC:** Isolated `agent.notifications` Celery task with 5 exponential retries, HMAC approval URL generation, and full `notification_deliveries` persistence (`PENDING` $\to$ `SENT` / `DEAD_LETTER`).
- **`ENG-025` — Concrete Notification Providers**
  - **Files:** `runlatch/notifications/slack.py`, `discord.py`, `github_comment.py`, `webhook.py`, `email.py`
  - **AC:** Slack Block Kit formatting with Approve/Reject links, Discord rich embeds, GitHub PR Markdown comments, HMAC-signed outbound webhooks, and async SMTP email — all verified with `respx`/mock tests.

---

## Phase 6: Containerization, Security Verification & OSS Launch

- **`ENG-026` — Docker & Docker Compose Production Stack**
  - **Files:** `Dockerfile`, `docker-compose.yml`
  - **AC:** `docker compose up --build` brings up `postgres`, `redis`, `api`, `worker-agents`, `worker-notifications`, and `beat` with passing health checks and automatic Alembic migration on startup.
- **`ENG-027` — Security & Chaos Test Suite**
  - **Files:** `tests/security/test_injection_and_limits.py`
  - **AC:** Verifies path traversal rejection, shell metacharacter blocking, secret redaction under nested payloads, loop termination, budget cap enforcement, and concurrent approval race safety.
- **`ENG-028` — Open-Source Polish & GitHub Release (`v0.1.0`)**
  - **Files:** `README.md`, `CHANGELOG.md`, `.github/workflows/release.yml`
  - **AC:** Complete README with architecture diagram, 15-line quickstart, server deployment guide, GIF/terminal output snippet, and tagged `v0.1.0` GitHub release.

---

## Recommended Execution Strategy (Ship Fast, Then Layer Up)

> **Important Practical Advice for Your Job Search:**
> Do **NOT** wait until Phase 6 to push to GitHub!
> 1. **Ship `v0.1.0` after Phases 0–3** (Core SDK + CLI + Built-in Workflows + Tests + polished README) and push immediately to `github.com/Prathamesh1306/runlatch`. At this point your resume bullet is **already 100% live, verifiable, and impressive**.
> 2. **Ship `v0.2.0` with Phases 4–5** (FastAPI + PostgreSQL + Celery + Redis + Notification Engine + Docker Compose). This elevates the repo into a true **Senior / Production Systems showcase** that directly mirrors your backend + notification microservice experience.
