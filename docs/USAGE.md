# `runlatch` — Complete User & Python API Usage Guide

This guide walks you through everything you need to use `runlatch` as a CLI tool, a Python library in your own projects, or a self-hosted production runtime.

---

## 1. Installation

```bash
# Core SDK + CLI (local SQLite, zero Docker)
pip install runlatch

# Add Model Context Protocol (MCP) server governance
pip install "runlatch[mcp]"

# Add Production Server (FastAPI, HTMX UI, PostgreSQL, Redis, Celery)
pip install "runlatch[mcp,server]"

# Add OpenTelemetry tracing
pip install "runlatch[observability]"
```

---

## 2. Environment Configuration (`.env` & `runlatch.toml`)

`RunLatchSettings.load()` automatically loads `.env` from your current working directory plus optional policy rules from `runlatch.toml`.

### Quick `.env` Presets

**A. Local Free LLM (Ollama — Zero API Keys)**
```env
RUNLATCH_LLM_PROVIDER=ollama
RUNLATCH_LLM_MODEL=qwen2.5-coder:7b
RUNLATCH_LLM_BASE_URL=http://localhost:11434/v1
RUNLATCH_LLM_API_KEY=ollama
```

**B. Cloud Free Tier (Groq or GitHub Models)**
```env
RUNLATCH_LLM_PROVIDER=groq
RUNLATCH_LLM_MODEL=llama-3.3-70b-versatile
GROQ_API_KEY=gsk_your_key_here
GITHUB_TOKEN=github_pat_your_token_here
```

**C. Cloud Paid Providers (OpenAI / Anthropic / Gemini)**
```env
# Set RUNLATCH_LLM_PROVIDER to openai, anthropic, or gemini
RUNLATCH_LLM_PROVIDER=anthropic
RUNLATCH_LLM_MODEL=claude-3-5-sonnet-latest
ANTHROPIC_API_KEY=sk-ant-...
# Or: OPENAI_API_KEY=sk-... / GEMINI_API_KEY=...
```

**D. Production Server + Notifications (Optional Layer 2)**
```env
RUNLATCH_DATABASE_URL=postgresql+asyncpg://runlatch:runlatch@localhost:5432/runlatch
RUNLATCH_REDIS_URL=redis://localhost:6379/0

# Generate with: openssl rand -hex 32
RUNLATCH_API_KEY=your_api_key
RUNLATCH_GITHUB_WEBHOOK_SECRET=your_webhook_secret
RUNLATCH_APPROVAL_SIGNING_SECRET=your_signing_secret
RUNLATCH_PUBLIC_BASE_URL=https://your-domain-or-ngrok-url

# Notifications (all optional)
RUNLATCH_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
RUNLATCH_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
RUNLATCH_SMTP_HOST=smtp.gmail.com
RUNLATCH_SMTP_PORT=587
RUNLATCH_SMTP_USERNAME=you@gmail.com
RUNLATCH_SMTP_PASSWORD=your_app_password
RUNLATCH_SMTP_FROM=you@gmail.com
```

### Policy File (`runlatch.toml`)
Run `runlatch init` in your repo to generate `runlatch.toml`:
```toml
[policy]
auto_approve_max_tier = "WRITE"
require_approval_tools = ["git_push", "github_create_pr"]
denied_tools = []

[policy.shell]
executor = "subprocess"
allowed_binaries = ["pytest", "ruff", "mypy", "git"]
timeout_seconds = 60

[policy.budget]
max_steps = 15
max_tokens = 80000
max_cost_usd = 0.50
max_duplicate_tool_calls = 2
```

---

## 3. CLI Reference

```bash
# 1. Scaffold runlatch.toml policy config in your repo
runlatch init

# 2. Run any built-in workflow in $0 demo mode (no API keys required)
runlatch run pr-review --demo
runlatch run ci-triage --demo
runlatch run dep-audit --demo
runlatch run issue-to-pr --demo
runlatch run doc-sync --demo

# 3. Run against a live LLM (uses .env or explicit --provider / --model)
runlatch run pr-review --provider ollama --model qwen2.5-coder:7b --max-cost 0.10
runlatch run pr-review --provider groq --max-cost 0.05

# 4. Inspect past runs, token usage, and USD spend
runlatch runs list
runlatch runs inspect <RUN_ID>

# 5. Handle Human-in-the-Loop (HITL) approvals from your terminal
runlatch approvals list
runlatch approve <APPROVAL_ID> --reason "Diff and tests look good"
runlatch reject <APPROVAL_ID> --reason "Keep auth middleware untouched"

# 6. Deterministically replay a previous run
runlatch replay <RUN_ID>

# 7. Start Production Server & Workers (requires `pip install "runlatch[server]"`)
runlatch server --host 0.0.0.0 --port 8000
runlatch worker --queues agent.runs,agent.maintenance --concurrency 4
runlatch worker --queues agent.notifications --concurrency 8
runlatch beat
```

---

## 4. Python Library API — Step-by-Step Examples

### 4.1 Define Custom Typed Tools with `@tool`
Any sync or async Python function with type hints becomes a Pydantic-validated LLM tool with an explicit `RiskTier`:

```python
from runlatch import RiskTier, tool

@tool(risk=RiskTier.READ, timeout_s=15)
async def fetch_service_health(service_name: str) -> str:
    """Check health status of an internal microservice."""
    return f"{service_name}: healthy (p99=42ms)"

@tool(risk=RiskTier.EXTERNAL, requires_approval=True)
async def rollback_deployment(service_name: str, version: str) -> str:
    """Roll back a production service (pauses for human approval!)."""
    return f"Rolled back {service_name} to {version}"
```

### 4.2 Run a Guarded `Agent` with `.env` Settings & SQLite Checkpointing

```python
import asyncio
from runlatch import Agent, FileToolkit, GitToolkit, PolicyEngine, RiskTier
from runlatch.adapters.llm.openai_compat import OpenAICompatibleProvider
from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings

async def main() -> None:
    settings = RunLatchSettings.load()  # Automatically loads .env + runlatch.toml
    store = SQLAlchemyCheckpointStore(settings.database_url)

    provider = OpenAICompatibleProvider.ollama(model=settings.llm_model)
    # Other providers:
    # provider = OpenAICompatibleProvider.groq(api_key=settings.groq_api_key)
    # provider = OpenAICompatibleProvider.github_models(token=settings.github_token)
    # from runlatch.adapters.llm.anthropic import AnthropicProvider
    # from runlatch.adapters.llm.gemini import GeminiProvider

    agent = Agent(
        name="repo-helper",
        provider=provider,
        tools=[*GitToolkit(".").tools(), *FileToolkit(".").tools()],
        policy=PolicyEngine(auto_approve_max_tier=RiskTier.READ),
        store=store,
        max_cost_usd=0.10,
    )

    result = await agent.run("Inspect git diff and summarize any risky changes.")
    print(result.status, result.output)
    print(f"Tokens: {result.total_tokens} | Cost: ${result.total_cost_usd:.4f}")
    await store.dispose()

asyncio.run(main())
```

### 4.3 Multi-Step `Workflow` with HITL Pause & Resume

```python
import asyncio
from runlatch import Agent, PolicyEngine, RiskTier, RunStatus, Step, Workflow
from runlatch.adapters.llm.openai_compat import OpenAICompatibleProvider
from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore

async def main() -> None:
    store = SQLAlchemyCheckpointStore("sqlite+aiosqlite:///./.runlatch.db")
    provider = OpenAICompatibleProvider.ollama("qwen2.5-coder:7b")
    policy = PolicyEngine(auto_approve_max_tier=RiskTier.WRITE)

    planner = Agent(name="planner", provider=provider, policy=policy, store=store)
    deployer = Agent(
        name="deployer",
        provider=provider,
        tools=[rollback_deployment],
        policy=policy,
        store=store,
    )

    workflow = (
        Workflow(name="incident-rollback", max_cost_usd=0.25, store=store)
        .add_step(Step(name="diagnose", agent=planner, output_key="plan"))
        .add_step(
            Step(
                name="rollback",
                agent=deployer,
                prompt="Execute rollback based on plan:\n{plan}",
                condition=lambda ctx: "rollback" in str(ctx.get("plan", "")).lower(),
            )
        )
    )

    result = await workflow.run({"prompt": "API latency spiked after v2.4.1. Rollback to v2.4.0."})

    if result.status == RunStatus.WAITING_APPROVAL:
        print(f"Paused! Approval ID: {result.approval_id}")
        # Resume later (even in a new process!) after human approval:
        final = await workflow.resume(
            result.run_id,
            approved=True,
            decided_by="prathamesh",
            reason="Confirmed in Grafana",
            store=store,
        )
        print("Resumed result:", final.status, final.output)

    await store.dispose()

asyncio.run(main())
```

### 4.4 Govern Any External MCP Server (`MCPToolkit`)

```python
import asyncio
from runlatch import Agent, MCPServerConfig, MCPToolkit, PolicyEngine, RiskTier
from runlatch.adapters.llm.openai_compat import OpenAICompatibleProvider

async def main() -> None:
    config = MCPServerConfig(
        server_name="github",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "github_pat_..."},
    )

    # All discovered tools default to RiskTier.EXTERNAL (fail-closed).
    # Only explicit local overrides can downgrade read-only tools.
    async with MCPToolkit(
        config,
        tool_risk_overrides={
            "github__get_*": RiskTier.READ,
            "github__list_*": RiskTier.READ,
        },
        policy=PolicyEngine(auto_approve_max_tier=RiskTier.READ),
    ) as mcp:
        tools = await mcp.discover_tools()
        agent = Agent(
            name="mcp-ops",
            provider=OpenAICompatibleProvider.ollama("qwen2.5-coder:7b"),
            tools=tools,
            policy=PolicyEngine(auto_approve_max_tier=RiskTier.READ),
        )
        res = await agent.run("List open issues and open a PR if needed.")
        print(res.status, res.approval_id)

asyncio.run(main())
```

### 4.5 Connect GitHub Webhooks, Slack, Discord & Email

1. **GitHub Webhook:** In your GitHub repo $\to$ *Settings $\to$ Webhooks $\to$ Add webhook*:
   - **Payload URL:** `https://<your-domain>/api/v1/webhooks/github` *(for local testing, run `ngrok http 8000`)*
   - **Content type:** `application/json`
   - **Secret:** Same value as `RUNLATCH_GITHUB_WEBHOOK_SECRET` in `.env`
   - **Events:** `Pull requests`, `Issues`, `Workflow runs`
2. **Slack / Discord / Email:** Set `RUNLATCH_SLACK_WEBHOOK_URL`, `RUNLATCH_DISCORD_WEBHOOK_URL`, or `RUNLATCH_SMTP_*` in `.env`, then pass `"notify_channels": ["slack:default", "discord:default", "email:you@example.com"]` when triggering `POST /api/v1/runs`.
3. **Live Run Inspector & Safe Approvals:** Open `http://localhost:8000/ui` in your browser to watch live SSE runs and approve/reject pending actions via the read-only confirmation page.
