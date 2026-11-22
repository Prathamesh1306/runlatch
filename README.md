# `runlatch` — Durable, HITL-First Execution Runtime for Autonomous Developer Agents

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](pyproject.toml)
[![Architecture: Hexagonal](https://img.shields.io/badge/Architecture-Hexagonal-10b981.svg)](AGENTS.md)
[![MCP: Governed](https://img.shields.io/badge/MCP-Fail--Closed_Governance-8b5cf6.svg)](runlatch/toolkits/mcp.py)

> *The MCP-native, event-driven Python runtime for autonomous developer workflows — zero-infra locally on SQLite, production-hardened with stateless pause/resume approvals on FastAPI, PostgreSQL, Redis, and Celery.*

---

## Why `runlatch`?

Existing Python agent tools force an awkward choice:
- **In-process SDKs** (PydanticAI, OpenAI Agents SDK) give clean tool calling, *but* stop at `await agent.run()` — no durable background workers, no pause/resume human approvals across process restarts, no webhook idempotency, and no multi-channel notifications.
- **Conversational frameworks** (CrewAI, AutoGen, LangGraph) bring heavy dependency trees and non-deterministic chat loops without native git worktree isolation or fail-closed tool risk tiers.

**`runlatch` provides the missing execution & governance runtime in a two-gear architecture:**

1. **Gear 1 — Zero-Infra Local SDK & CLI (`pip install runlatch`)**:
   - Define typed tools with `@tool` or wrap any **Model Context Protocol (MCP)** server with `MCPToolkit`.
   - Enforce deterministic `RiskTier` policies (`READ`, `WRITE`, `EXEC`, `EXTERNAL`), automatic secret redaction, duplicate tool-call loop detection, and hard token/USD budget caps.
   - Persist durable checkpoints locally via async SQLAlchemy 2.0 + `aiosqlite` — zero Docker or Redis required.
2. **Gear 2 — Distributed Production Runtime (`pip install "runlatch[server]"`)**:
   - Run the **exact same Python workflows** behind **FastAPI**, **PostgreSQL**, **Redis + Celery** (3 isolated queues + Beat scheduler), a **Zero-Build Jinja2/HTMX Live Run Inspector (`/ui`)**, and a **5-Channel Notification Engine** (Slack Block Kit, Discord, GitHub PR comments, HMAC-signed Webhooks, SMTP Email).
3. **Stateless Human-in-the-Loop (HITL) Git Worktree Pause & Resume**:
   - When an agent calls an `EXTERNAL` tool (`git_push`, `github_create_pr`), `runlatch` captures both modified and brand-new untracked files (`git add -N .` + `git diff HEAD`, excluding binaries, capped at `512 KB`), saves `workspace_patch` + `workspace_meta` to the DB, frees the worker slot immediately, and dispatches a signed link to a **read-only confirmation page** (`GET /ui/approvals/{id}?sig=...&exp=...` — immune to Slack/email link-unfurler `GET` prefetches).
   - Once a human clicks **Approve** (`POST /api/v1/approvals/{id}/decision` guarded by `SELECT ... FOR UPDATE`), *any* healthy worker clones/fetches `base_commit_sha` into a fresh `git worktree`, applies the patch via `git apply --3way`, and completes the run.

---

## System Architecture

```mermaid
flowchart TB
    subgraph Triggers["Triggers & Clients"]
        CLI["CLI (`runlatch`) / Python SDK"]
        GH["GitHub Webhooks (PR, Issue, CI, Push)"]
        CRON["Celery Beat (5m Approval Sweeper + Weekly Audit)"]
    end

    subgraph ControlPlane["FastAPI Control Plane + HTMX UI (Layer 2)"]
        API["REST + SSE API & Bearer Auth"]
        UI["HTMX Run Inspector (`/ui`) & Read-Only Approval Page (`/ui/approvals/{id}`)"]
        WH["Webhook Verifier (HMAC-SHA256 + Redis `SET NX EX 86400`)"]
    end

    subgraph Queue["Redis + Celery 3-Queue Backbone"]
        Q_RUNS["Queue: `agent.runs` (`NullPool` Async Runner)"]
        Q_NOTIFY["Queue: `agent.notifications` (5x Exponential Backoff)"]
        Q_MAINT["Queue: `agent.maintenance`"]
    end

    subgraph Core["Hexagonal Core & Toolkits (Layer 1 — Zero External Framework Imports)"]
        WF["Multi-Step `Workflow` & `StepExecutor` Protocol"]
        AGENT["Bounded ReAct `Agent` + Loop Detector + Budget Caps"]
        POLICY["`PolicyEngine` (`READ` / `WRITE` / `EXEC` / `EXTERNAL`) + `SecretRedactor`"]
        TK["`FileToolkit` (`SEARCH/REPLACE`) · `GitToolkit` (`worktree`) · `ShellToolkit` · `GitHubToolkit` · `MCPToolkit`"]
        LLM["LLM Adapters (OpenAI/Ollama/Groq/GitHub Models · Anthropic · Gemini · Mock)"]
    end

    subgraph Storage["Unified Async Storage & Notifications"]
        DB[("SQLite (`aiosqlite`) Locally\nPostgreSQL (`asyncpg`) in Prod")]
        NOTIF["NotificationRouter (Slack · Discord · GitHub · Webhook · SMTP Email)"]
    end

    CLI --> WF
    GH --> WH --> Q_RUNS
    CRON --> Q_MAINT
    API --> Q_RUNS
    UI -- "POST /api/v1/approvals/{id}/decision" --> API

    Q_RUNS --> WF --> AGENT
    AGENT <--> POLICY
    AGENT <--> LLM
    AGENT <--> TK
    AGENT <--> DB
    Q_RUNS -- "Approval Requested / Run Completed" --> Q_NOTIFY --> NOTIF --> DB
```

---

## Quickstart (60 Seconds, `$0.00` API Spend)

### 1. Install & Run Built-in Flagship Workflows in `--demo` Mode

```bash
pip install -e ".[dev]"

# Initialize runlatch.toml policy config
runlatch init

# Run autonomous PR review at $0.00 using deterministic multi-turn traces + real local git/file tools
runlatch run pr-review --demo

# Run all 5 flagship workflows in --demo mode
runlatch run ci-triage --demo
runlatch run dep-audit --demo
runlatch run issue-to-pr --demo   # Pauses at HITL gate -> approve with `runlatch approve <id>`
runlatch run doc-sync --demo      # Pauses at HITL gate -> approve with `runlatch approve <id>`
```

See a full recorded terminal trace in [`examples/traces/pr_review_demo.md`](examples/traces/pr_review_demo.md).

### 2. Minimal 15-Line Python Agent (Local Ollama / Groq / GitHub Models / OpenAI / Anthropic / Gemini)

```python
import asyncio
from runlatch import Agent, FileToolkit, GitToolkit, OpenAICompatibleProvider

async def main() -> None:
    agent = Agent(
        name="git-summarizer",
        # Zero-cost presets: .ollama(), .groq(), .github_models() — or AnthropicProvider / GeminiProvider
        provider=OpenAICompatibleProvider.ollama(model="qwen2.5-coder:7b"),
        tools=[*GitToolkit(".").tools(), *FileToolkit(".").tools()],
        max_cost_usd=0.05,
    )
    result = await agent.run("Inspect my git status and summarize the repository files.")
    print(result.output)
    print(f"Tokens: {result.total_tokens} | Cost: ${result.total_cost_usd:.4f}")

asyncio.run(main())
```

### 3. Fail-Closed MCP Server Governance (`MCPToolkit`)

Wrap any third-party MCP server (`stdio` or `streamable_http`) with deterministic risk tiers, glob overrides, secret redaction, and HITL pause/resume — without trusting remote `readOnlyHint` annotations:

```python
from runlatch import MCPServerConfig, MCPToolkit, PolicyEngine, RiskTier

config = MCPServerConfig(server_name="ops", transport="stdio", command="npx", args=["-y", "@modelcontextprotocol/server-postgres"])

async with MCPToolkit(
    config,
    # Fail-closed: all discovered tools default to RiskTier.EXTERNAL (requires approval).
    # Exact match wins over fnmatch glob; remote readOnlyHint is never trusted blindly.
    tool_risk_overrides={
        "ops__get_*": RiskTier.READ,
        "ops__get_secret_dump": RiskTier.EXTERNAL,
    },
    policy=PolicyEngine(auto_approve_max_tier=RiskTier.READ),
) as mcp_toolkit:
    tools = await mcp_toolkit.discover_tools()
```

---

## Running the Full Production Stack (`FastAPI + HTMX UI + Postgres + Redis + Celery`)

```bash
cp .env.example .env
docker compose up --build
```

- **Live Run Inspector UI (Jinja2 + HTMX):** `http://localhost:8000/ui`
- **Read-Only HITL Approval Page:** `http://localhost:8000/ui/approvals/{approval_id}?sig=...&exp=...`
- **Health & Readiness Probes:** `http://localhost:8000/health/live` and `http://localhost:8000/health/ready`
- **GitHub Webhook Endpoint:** `POST http://localhost:8000/api/v1/webhooks/github`

Or start components individually from the CLI:

```bash
runlatch server --host 0.0.0.0 --port 8000
runlatch worker --queues agent.runs,agent.maintenance --concurrency 4
runlatch worker --queues agent.notifications --concurrency 8
runlatch beat
```

---

## Security & Honest Execution Boundaries

| Boundary | Deterministic Control in `runlatch` |
|---|---|
| **Filesystem Sandbox** | `resolve_safe_path()` enforces `Path.resolve().is_relative_to(workspace_root)`, blocking `..` traversal, symlink escapes, null bytes, and denied globs (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `.git/config`). |
| **Two-Tier Command Execution** | • **Trusted Local Repos (`SubprocessExecutor`):** `shell=False`, environment stripped to explicit allowlist, shell metacharacters blocked, and raw interpreters (`python`, `bash`, `sh`, `node`, `curl`) strictly forbidden.<br>• **Untrusted External PRs (`DockerSandboxExecutor`):** Runs inside ephemeral containers with `docker run --rm --network=none --cap-drop=ALL --read-only --memory=512m --cpus=1 --security-opt=no-new-privileges`. |
| **Secret Redaction** | `SecretRedactor` scrubs OpenAI, Anthropic, Groq, GitHub, Slack, AWS, Bearer, PEM private keys, and dynamic runtime secrets (including `SMTP_PASSWORD` and Slack/Discord webhook URLs) from all tool outputs, audit logs, and notifications before persistence or dispatch. |
| **Safe HITL Approval Links** | Notification links point to a read-only `GET /ui/approvals/{id}` page; state changes happen strictly on `POST /api/v1/approvals/{id}/decision` inside an atomic `SELECT ... FOR UPDATE` transaction. |

---

## Architecture & Engineering Documentation

- **[`AGENTS.md`](AGENTS.md)** — Strict hexagonal import boundaries (`tests/unit/test_hexagonal_boundaries.py`) & mandatory engineering invariants.
- **[`START-HERE-ROADMAP.md`](START-HERE-ROADMAP.md)** — Concentric layer map and vertical slice build guide.
- **[`product/01-vision-market-differentiation.md`](product/01-vision-market-differentiation.md)** & **[`product/02-use-cases-and-workflows.md`](product/02-use-cases-and-workflows.md)**
- **[`architecture/03-tech-stack-and-tradeoffs.md`](architecture/03-tech-stack-and-tradeoffs.md)** through **[`architecture/07-security-guardrails-and-observability.md`](architecture/07-security-guardrails-and-observability.md)**
- **[`engineering/08-repo-structure-and-dx.md`](engineering/08-repo-structure-and-dx.md)** through **[`engineering/10-testing-deployment-and-interview-playbook.md`](engineering/10-testing-deployment-and-interview-playbook.md)**

---

## License

Released under the [MIT License](LICENSE).
