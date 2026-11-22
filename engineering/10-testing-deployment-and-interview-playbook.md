# 10 — Testing Strategy, Production Operations & Interview Playbook

---

## 1. Testing Strategy (Zero-Flake, Zero-Spend CI)

LLM frameworks often suffer from flaky or unrunnable test suites because tests call live paid APIs. `runlatch` enforces a strict **3-tier deterministic testing pyramid**:

### Tier 1: Fast Unit Tests (`tests/unit/`, < 2 seconds, `$0` cost)
- Uses built-in `MockLLMProvider` to script exact multi-turn LLM responses (including malformed tool args, parallel tool calls, duplicate loop triggers, and budget overruns).
- Uses `respx` to mock HTTP responses from OpenAI, Anthropic, Gemini, GitHub, Slack, and Discord APIs — testing retry/backoff on `429`/`503` deterministically.

### Tier 2: Security & Guardrail Tests (`tests/security/`)
- Parametrized attack vectors:
  - Path escapes (`../../etc/passwd`, symlinks pointing outside workspace, `.env.local` reads).
  - Shell injection attempts (`pytest; rm -rf /`, `$(whoami)`, disallowed binaries like `curl`/`bash`).
  - Secret redaction against 15+ token formats inside deeply nested JSON args and tracebacks.
  - Invalid/expired GitHub HMAC signatures and tampered approval tokens.

### Tier 3: Integration & Worker Tests (`tests/integration/`)
- Full end-to-end HITL lifecycle against SQLite & Postgres:
  `run` $\to$ `WAITING_APPROVAL` $\to$ persist checkpoint $\to$ simulate process restart (new `Agent` instance) $\to$ `resume(approved=True)` $\to$ `COMPLETED`.
- FastAPI `AsyncClient` route tests + Celery `task_always_eager=True` mode testing webhook ingress $\to$ queue dispatch $\to$ notification delivery logging.
- Concurrency test verifying two simultaneous `POST /approvals/{id}/decision` calls result in one `200 OK` and one `409 Conflict`.

---

## 2. Production Deployment Checklist

Before running `runlatch[server]` against real private repositories:

- [ ] **Dedicated Bot GitHub Token / GitHub App:** Never use a personal admin PAT. Scope token strictly to target repos (`contents: write`, `pull_requests: write`, `issues: write`).
- [ ] **Branch Protection Enabled:** Enforce GitHub branch protection on `main` so even an approved agent can only open PRs, never force-push to `main`.
- [ ] **Webhook Secret Configured:** `GITHUB_WEBHOOK_SECRET` set to >= 32 random bytes; unsigned requests rejected with `401`.
- [ ] **Policy Ceiling Locked:** `auto_approve_max_tier = "READ"` or `"WRITE"`, keeping `EXEC` and `EXTERNAL` behind HITL approval until workflow prompts are battle-tested.
- [ ] **Budget Caps Active:** Global per-run `max_cost_usd` (`$0.25–$0.50`) + provider-level monthly spend limits on OpenAI/Anthropic dashboards.
- [ ] **Worker Isolation:** Run `worker-agents` inside an unprivileged non-root Docker container with `--cap-drop=ALL`, read-only rootfs (writable `/tmp/workspaces` only), and memory/CPU limits.
- [ ] **Database Backups & Retention:** Enable automated Postgres backups and periodic pruning of `messages_checkpoint` payloads older than 90 days.

---

## 3. Known Scaling Ceilings & Upgrade Paths (`ponytail:` Registry)

Being transparent about architectural ceilings is a hallmark of senior engineering:

| Deliberate Simplification (v1.0) | Known Ceiling | Clean Upgrade Path (v2.0) |
|---|---|---|
| **Process-level shell sandboxing (`shell=False` + allowlist)** | Safe for trusted/internal repos, not hardened against malicious untrusted code execution (e.g., running arbitrary `pytest` from an untrusted external fork). | Swap `ShellToolkit` executor to ephemeral Docker containers or Firecracker/E2B microVMs per run. |
| **Celery + Redis (`asyncio.run` per task)** | Great up to ~100–500 concurrent agent runs; process-per-worker memory overhead grows at massive scale. | Swap `TaskDispatcher` implementation to **ARQ** (pure async Redis workers) or **Temporal** for ultra-long-lived durable workflows. |
| **Redis Pub/Sub for SSE live streaming** | Events published before a client connects are not replayed over Pub/Sub (client fetches past steps from Postgres first, then subscribes). | Upgrade channel to **Redis Streams (`XADD`/`XREAD`)** for cursor-based lossless SSE reconnection. |
| **Static Python pricing table (`pricing.py`)** | Model prices change occasionally over time. | Allow user overrides in `runlatch.toml` or optional sync from LiteLLM's open pricing JSON. |

---

## 4. Technical Interview Playbook (How to Pitch `runlatch`)

When an interviewer points to `runlatch` on your resume and asks *"Tell me about this framework you built"*, use this crisp 60-second structure:

### 1. The Problem & Differentiator (15 sec)
> *"Existing Python agent tools are either in-process SDKs or conversational role-play frameworks — none of them give you a durable, event-driven production runtime for developer workflows. I built `runlatch` as an MCP-native, two-layer Python runtime: zero-infra locally on SQLite, and FastAPI + HTMX Run Inspector + PostgreSQL + Celery/Redis in production with stateless pause/resume human approvals."*

### 2. Deep Technical Talking Points (Pick 2–3 Based on Interviewer)
- **MCP Governance & Self-Correcting Tool Calls:** *"Both native `@tool` functions and external MCP server tools pass through Pydantic schema validation, `PolicyEngine` risk tiers, and `SecretRedactor`. When an LLM hallucinates invalid arguments, validation returns a structured error back into the loop so the model self-corrects."*
- **Stateless HITL Pause/Resume Without Blocking Workers:** *"Before an `EXTERNAL` action like `github_create_pr`, the worker checkpoints conversation state AND the `git diff` patch to Postgres, frees its Celery slot immediately, and sends a signed link to a read-only HTMX confirmation page (immune to link-unfurler `GET` auto-approvals). When a human submits `POST`, a Postgres `SELECT ... FOR UPDATE` lock prevents double-resume, and any healthy worker reconstructs the patch in a fresh `git worktree` and pushes."*
- **Production Reliability & Honest Security Boundaries:** *"GitHub webhooks are HMAC-verified and deduplicated via Redis `NX` keys; Celery `prefork` tasks use `NullPool` async SQLAlchemy engines to avoid `asyncpg` event-loop leaks; and command execution separates trusted local `SubprocessExecutor` (`shell=False`, no raw `python`) from an ephemeral `--network=none` `DockerSandboxExecutor` for untrusted external PRs."*

### 3. Likely Follow-Up Questions & Crisp Answers
- **Q: Why Celery + Redis instead of FastAPI `BackgroundTasks`?**
  - *A: `BackgroundTasks` dies on server restart and shares event-loop/memory fate with HTTP traffic. Celery gives isolated queues (`agent.runs` vs `agent.notifications`), `acks_late` crash redelivery, time limits, exponential retries, and Beat scheduling — while Redis doubles for webhook idempotency, per-repo locks, and SSE Pub/Sub.*
- **Q: How do you test an agent framework without burning LLM credits?**
  - *A: All adapters sit behind an `LLMProvider` protocol. I ship a deterministic `MockLLMProvider` and use `respx` HTTP mocks, so the entire loop, policy engine, loop detector, and pause/resume flow run in CI in under 2 seconds at `$0` cost.*
- **Q: What happens if a prompt injection is hidden in a GitHub issue?**
  - *A: Untrusted GitHub content is delimited in prompts, but we never rely on prompt-level security alone. Deterministic code enforces path containment, `shell=False` command allowlists, secret redaction, and mandatory human approval before any `EXTERNAL` action.*
