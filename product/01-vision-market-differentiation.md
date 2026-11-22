# 01 — Product Vision & Market Differentiation

## 1. The Honest Market Reality

The Python agent ecosystem is crowded. Before writing a single line of code, we must answer one ruthless question:

> **"Why should `runlatch` exist when LangGraph, CrewAI, AutoGen, PydanticAI, and OpenHands already exist?"**

If `runlatch` is just "another wrapper around OpenAI function calling," it has zero differentiation and feels like a toy tutorial project. To stand out as a serious open-source framework (and an impressive engineering portfolio piece), `runlatch` must occupy a **specific, under-served architectural niche**.

---

## 2. Competitor Landscape & Gap Analysis

| Framework | Primary Strength | Where It Fails for Autonomous Developer Workflows |
|---|---|---|
| **LangChain / LangGraph** | General-purpose stateful graph orchestration; huge ecosystem. | Heavy dependency footprint, frequent breaking abstractions, steep boilerplate to wire simple dev tasks, and zero built-in opinionated developer tooling (Git, GitHub, shell policy, PR approvals). |
| **CrewAI / AutoGen** | Multi-agent persona/chat delegation ("Manager talks to Researcher"). | Conversational loops are **non-deterministic and expensive** for engineering tasks. No native webhook-to-worker architecture, no strict tool risk tiers, and poor crash/resume guarantees. |
| **PydanticAI / OpenAI Agents SDK** | Clean, typed, lightweight Python tool-calling primitives. | Purely **in-process libraries**. They stop at `await agent.run()`. They do NOT provide durable background execution, GitHub event ingestion, HITL pause/resume across worker restarts, or notification delivery. |
| **Aider / Claude Code / Cline** | Best-in-class interactive coding copilots in a local terminal/IDE. | Built for a **human sitting at a keyboard**. Not programmable Python frameworks for headless CI/CD pipelines, scheduled repo audits, or webhook-driven team automation. |
| **OpenHands / SWE-Agent** | Autonomous benchmark-solving coding agents inside heavy containers. | Heavyweight monolithic systems built to solve SWE-bench tasks — not modular, composable Python libraries you can extend with custom internal tools and notification pipelines. |

---

## 3. Our Unique Differentiator (The 2026 `runlatch` Wedge)

Nobody in 2026 is waking up wishing for *yet another* `@tool` decorator or hand-rolled LLM wrapper. PydanticAI, OpenAI/Claude Agent SDKs, LangGraph, and MCP already own basic tool calling.

`runlatch` wins by owning the **missing production runtime layer for software engineering agents**: **durable background execution + MCP governance + stateless HITL approvals + GitHub ops + live Run Inspector UI**.

### Five Pillars That Make `runlatch` Different

#### Pillar 1: MCP-Native Governance & Bring-Your-Own-Agent Extensibility
- **Plug in any MCP server (`MCPToolkit`):** Instead of forcing developers to rewrite integrations from scratch, `runlatch` connects to existing MCP (Model Context Protocol) servers (GitHub, Sentry, Postgres, Linear, Filesystem) and automatically wraps every remote tool with **Risk Tiers, Policy Engine checks, Secret Redaction, Budget Caps, and HITL Approval Gates**.
- **Bring Your Own Agent (`StepExecutor` protocol):** Ships a clean, zero-bloat built-in ReAct `Agent` out of the box, while letting teams wrap external agent runners (e.g., PydanticAI or custom Python functions) inside `runlatch`'s durable workflow + Celery + HITL runtime.

#### Pillar 2: Two-Gear Architecture (Zero-Infra Local $\to$ Distributed Production)
- **Gear 1 (Local Runtime):** `pip install runlatch` requires **zero Docker, zero Redis, zero Postgres**. A developer writes a 15-line Python script, runs it in their terminal, and state checkpoints automatically to a local SQLite file via unified async SQLAlchemy.
- **Gear 2 (Production Worker Plane):** `pip install "runlatch[server]"` + `docker compose up` takes the **exact same workflow code** and runs it behind FastAPI + HTMX Run Inspector + PostgreSQL + Redis + Celery workers, triggered by GitHub webhooks or cron schedules.

#### Pillar 3: Stateless Pause/Resume Human-in-the-Loop (HITL) via Risk Tiers
- Every tool (native `@tool` or imported via MCP) declares a deterministic risk tier: `READ`, `WRITE`, `EXEC`, or `EXTERNAL` (e.g., `git_push`, `create_pull_request`).
- When an agent attempts a tool above the configured auto-approve threshold:
  1. The worker **checkpoints full conversation state, workflow context, AND the unified git diff patch** to Postgres (so no state is tied to ephemeral worker disks).
  2. Status transitions to `WAITING_APPROVAL` and the worker **frees its process slot immediately**.
  3. The **Notification Engine** sends an alert (Slack, Discord, GitHub PR comment, Email, or Webhook) linking to a **read-only HTMX Confirmation & Diff Review page** (immune to Slack/email link-unfurler `GET` auto-approvals).
  4. When a developer clicks Approve hours later, a `POST` request acquires a Postgres `SELECT ... FOR UPDATE` lock, enqueues a Celery resume task, reconstructs the repo + patch in a fresh `git worktree` on *any* worker, and completes the action.

#### Pillar 4: Engineering-First Safety, Cost Controls & Live Visibility
- **Hard Budget Caps & Loop Detection:** Every run enforces `max_steps`, `max_tokens`, `max_cost_usd`, and `(tool_name, args_hash)` loop detection.
- **Two-Tier Command Sandboxing:** Honest separation between local trusted subprocess execution (`shell=False`, strict allowlist — no raw `python`) and ephemeral Docker container execution (`--network=none --cap-drop=ALL`) for untrusted PRs.
- **Zero-Build Live Run Inspector (`/ui`):** Lightweight FastAPI + HTMX dashboard consuming the SSE stream so teams watch tool calls, token burn, diffs, and approve/reject runs in a browser with zero Node/React build step.

#### Pillar 5: Native Developer Primitives (Built for Real Repos)
- **Filesystem Toolkit:** Path-jailed `read_file`, `search_code`, `list_files`, and **`edit_file` via deterministic `SEARCH/REPLACE` blocks** (far higher LLM success rate than brittle line-numbered unified diffs).
- **Git & GitHub Toolkits:** `git_status`, `git_diff`, `git_log`, worktree-isolated branching/commits, and async GitHub REST operations — plus full MCP server compatibility.

---

## 4. Target Personas

| Persona | Pain Point | How They Use `runlatch` |
|---|---|---|
| **Backend / AI Engineer** | Needs to automate repetitive repo chores (PR summaries, migration checks, flaky test triage) in Python without adopting LangChain's complexity. | Installs `runlatch` locally, registers 2 custom internal tools with `@tool`, wires a 3-step `Workflow`, and runs it in CI. |
| **Startup Engineering Team (5–50 devs)** | Wants automated PR first-pass reviews, CI failure root-cause analysis, and issue triage triggered by GitHub webhooks — with Slack alerts and human approval before any code is pushed. | Deploys `runlatch[server]` via Docker Compose, connects GitHub webhooks + Slack webhook, and monitors runs via the FastAPI API. |
| **OSS Maintainer** | Drowning in low-quality PRs and repetitive bug reports. | Runs `runlatch` workflows via GitHub Actions to auto-triage issues, check PR compliance, and draft release notes within a strict `$0.10/PR` budget cap. |

---

## 5. Explicit Non-Goals (Keeping Scope Sharp)

To stay focused and avoid building a bloated monolith, `runlatch` will **explicitly NOT** try to be:
- A visual drag-and-drop workflow builder UI (like n8n or Dify) — `runlatch` is **code-first Python** for developers.
- A full conversational chatbot / customer-support framework — it is optimized for **bounded, task-oriented software workflows**.
- A container orchestration platform (Kubernetes replacement) — it uses standard Docker/process sandboxing and delegates heavy infra scaling to standard Celery workers.
- A proprietary hosted SaaS — 100% open-source (MIT License), self-hostable, bring-your-own-LLM-key.
