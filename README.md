# `runlatch` — Durable, HITL-First Execution Runtime for Autonomous Developer Agents

[![PyPI version](https://img.shields.io/pypi/v/runlatch.svg)](https://pypi.org/project/runlatch/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/Prathamesh1306/runlatch/blob/main/LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://github.com/Prathamesh1306/runlatch/blob/main/pyproject.toml)
[![Architecture: Hexagonal](https://img.shields.io/badge/Architecture-Hexagonal-10b981.svg)](https://github.com/Prathamesh1306/runlatch/blob/main/AGENTS.md)
[![MCP: Governed](https://img.shields.io/badge/MCP-Fail--Closed_Governance-8b5cf6.svg)](https://github.com/Prathamesh1306/runlatch/blob/main/runlatch/toolkits/mcp.py)

> *The MCP-native, event-driven Python runtime for autonomous developer workflows — zero-infra locally on SQLite, production-hardened with stateless pause/resume approvals on FastAPI, PostgreSQL, Redis, and Celery.*
>
> **[Complete User & Python API Usage Guide (`docs/USAGE.md`)](https://github.com/Prathamesh1306/runlatch/blob/main/docs/USAGE.md)**

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

```text
+------------------------------------------------------------------------------+
|                            TRIGGERS & CLIENTS                                |
|   CLI (runlatch)   |   GitHub Webhooks (PR/Issue/CI/Push)   |   Celery Beat  |
+--------------------+----------------------------------------+----------------+
                                       |
                                       v
+------------------------------------------------------------------------------+
|                  FASTAPI CONTROL PLANE + HTMX UI (Layer 2)                   |
|  - REST + SSE Streaming API (/api/v1/runs, /approvals, /metrics/summary)     |
|  - Webhook Ingress (HMAC-SHA256 + Redis SET NX EX 86400 Idempotency)         |
|  - Live Run Inspector (/ui) & Read-Only Approval Page (/ui/approvals/{id})   |
+--------------------------------------+---------------------------------------+
                                       |
                                       v
+------------------------------------------------------------------------------+
|                   REDIS + CELERY 3-QUEUE BACKBONE                            |
|   [agent.runs] (NullPool Async)   |   [agent.notifications] (5x Backoff)     |
|   [agent.maintenance] (5m Stale Approval Sweeper + Weekly dep-audit)         |
+--------------------------------------+---------------------------------------+
                                       |
                                       v
+------------------------------------------------------------------------------+
|               HEXAGONAL CORE & TOOLKITS (Layer 1 — Pure Python)              |
|  - Workflow & StepExecutor Protocol (pr-review, issue-to-pr, ci-triage, ...) |
|  - Bounded ReAct Agent + Duplicate-Call Loop Detector + Token/USD Caps       |
|  - PolicyEngine (READ / WRITE / EXEC / EXTERNAL) + SecretRedactor            |
|  - Toolkits: FileToolkit (SEARCH/REPLACE), GitToolkit (worktree),            |
|              ShellToolkit (Subprocess / Docker), GitHubToolkit, MCPToolkit   |
|  - LLM Adapters: OpenAI / Ollama / Groq / GitHub Models / Anthropic / Gemini |
+------------------+----------------------------------------+------------------+
                   |                                        |
                   v                                        v
+-----------------------------------+    +-------------------------------------+
|   UNIFIED ASYNC STORAGE           |    |   NOTIFICATION ENGINE               |
|   SQLite (aiosqlite) locally      |    |   Slack · Discord · GitHub PR       |
|   PostgreSQL (asyncpg) in prod    |    |   HMAC Webhooks · SMTP Email        |
+-----------------------------------+    +-------------------------------------+
```

---

## Quickstart (60 Seconds, `$0.00` API Spend)

### 1. Install & Run Built-in Flagship Workflows in `--demo` Mode

```bash
# Core SDK & CLI (zero Docker/Redis required)
pip install runlatch

# Optional: add MCP server governance and/or full FastAPI + Celery + HTMX server extras
pip install "runlatch[mcp,server]"

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

See a full recorded terminal trace in [`examples/traces/pr_review_demo.md`](https://github.com/Prathamesh1306/runlatch/blob/main/examples/traces/pr_review_demo.md), or read the **[Complete Usage Guide (`docs/USAGE.md`)](https://github.com/Prathamesh1306/runlatch/blob/main/docs/USAGE.md)** for custom `@tool` functions, multi-step `Workflow` pipelines, and server setup.

### 2. Minimal `.env` Configuration (BYOK or Zero-Key Local Ollama)

```env
# Local Free LLM (Ollama — zero API keys)
RUNLATCH_LLM_PROVIDER=ollama
RUNLATCH_LLM_MODEL=qwen2.5-coder:7b
RUNLATCH_LLM_BASE_URL=http://localhost:11434/v1
RUNLATCH_LLM_API_KEY=ollama

# Or switch to Groq / GitHub Models / OpenAI / Anthropic / Gemini:
# GROQ_API_KEY=gsk_...
# GITHUB_TOKEN=github_pat_...
# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
# GEMINI_API_KEY=...
```

### 3. Minimal 15-Line Python Agent (Local Ollama / Groq / GitHub Models / OpenAI / Anthropic / Gemini)

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

### 4. Fail-Closed MCP Server Governance (`MCPToolkit`)

Wrap any third-party MCP server (`stdio` or `streamable_http`) with deterministic risk tiers, glob overrides, secret redaction, and HITL pause/resume — without trusting remote `readOnlyHint` annotations:

```python
from runlatch import MCPServerConfig, MCPToolkit, PolicyEngine, RiskTier

config = MCPServerConfig(
    server_name="ops",
    transport="stdio",
    command="npx",
    args=["-y", "@modelcontextprotocol/server-postgres"],
)

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

Clone the repository for the Docker Compose stack and `.env.example`:

```bash
git clone https://github.com/Prathamesh1306/runlatch.git && cd runlatch
cp .env.example .env
docker compose up --build
```

- **Live Run Inspector UI (Jinja2 + HTMX):** `http://localhost:8000/ui`
- **Read-Only HITL Approval Page:** `http://localhost:8000/ui/approvals/{approval_id}?sig=...&exp=...`
- **Health & Readiness Probes:** `http://localhost:8000/health/live` and `http://localhost:8000/health/ready`
- **GitHub Webhook Endpoint:** `POST http://localhost:8000/api/v1/webhooks/github`

Or start components individually from the CLI (`pip install "runlatch[server]"`):

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
| **Two-Tier Command Execution** | **Trusted Local Repos (`SubprocessExecutor`):** `shell=False`, environment stripped to explicit allowlist, shell metacharacters blocked, and raw interpreters (`python`, `bash`, `sh`, `node`, `curl`) strictly forbidden. **Untrusted External PRs (`DockerSandboxExecutor`):** Runs inside ephemeral containers with `docker run --rm --network=none --cap-drop=ALL --read-only --memory=512m --cpus=1 --security-opt=no-new-privileges`. |
| **Secret Redaction** | `SecretRedactor` scrubs OpenAI, Anthropic, Groq, GitHub, Slack, AWS, Bearer, PEM private keys, and dynamic runtime secrets (including `SMTP_PASSWORD` and Slack/Discord webhook URLs) from all tool outputs, audit logs, and notifications before persistence or dispatch. |
| **Safe HITL Approval Links** | Notification links point to a read-only `GET /ui/approvals/{id}` page; state changes happen strictly on `POST /api/v1/approvals/{id}/decision` inside an atomic `SELECT ... FOR UPDATE` transaction. |

---

## Documentation

- **[`docs/USAGE.md`](https://github.com/Prathamesh1306/runlatch/blob/main/docs/USAGE.md)** — Complete Usage & Python API Guide
- **[`AGENTS.md`](https://github.com/Prathamesh1306/runlatch/blob/main/AGENTS.md)** — Hexagonal Architecture & Contributor Guardrails
- **[`CHANGELOG.md`](https://github.com/Prathamesh1306/runlatch/blob/main/CHANGELOG.md)** — Release History

---

## License

Released under the [MIT License](https://github.com/Prathamesh1306/runlatch/blob/main/LICENSE).
