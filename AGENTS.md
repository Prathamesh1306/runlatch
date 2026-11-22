# `AGENTS.md` — Engineering & Architecture Guardrails for `runlatch`

Read `START-HERE-ROADMAP.md` and `README.md` before writing code. Every change in this repository MUST obey these non-negotiable rules.

## 1. Strict Hexagonal Import Boundaries

Dependencies ONLY point inward — never outward:

- **`runlatch/core/`** — PURE DOMAIN. Allowed imports: Python stdlib, `pydantic`, `runlatch.core.*`.
  - **STRICTLY FORBIDDEN:** `fastapi`, `celery`, `redis`, `sqlalchemy`, `httpx`, `mcp`, `typer`, `jinja2`, `runlatch.adapters.*`, `runlatch.runtime.*`.
- **`runlatch/tools/`** — Allowed: stdlib, `pydantic`, `runlatch.core.*`.
- **`runlatch/adapters/*`** — Allowed: stdlib, `runlatch.core.*`, plus **only its own external library** (e.g., `sqlalchemy` only inside `adapters/storage/`). Never import another adapter or `runlatch.runtime.*`.
- **`runlatch/toolkits/*`** — Allowed: `runlatch.core.*`, `runlatch.tools.*`, `httpx` / lazy `mcp`. Never import `fastapi`, `celery`, `sqlalchemy`, or `runlatch.runtime.*`.
- **`runlatch/workflows/*`** — Pure composition of `runlatch.core.*` and `runlatch.toolkits.*`. Never import `fastapi`, `celery`, or `sqlalchemy`.
- **`runlatch/runtime/*`** — Wires `core` + `adapters` + `toolkits` + `workflows`. Never embed agent loops, business logic, or raw SQL inside FastAPI routes or Celery task wrappers.

## 2. Protocol-First Boundaries

Every external dependency MUST implement a `typing.Protocol` defined in `runlatch/core/protocols.py`:
- `LLMProvider`
- `StepExecutor` (allows built-in `Agent` or BYO-Agent adapters)
- `CheckpointStore`
- `CommandExecutor`
- `TaskDispatcher`
- `NotificationProvider`

## 3. Mandatory Engineering Invariants

1. **Unified Async Storage:** Use a single async SQLAlchemy 2.0 `SQLAlchemyCheckpointStore` supporting both `sqlite+aiosqlite` (Layer 1 local) and `postgresql+asyncpg` (Layer 2 prod) via dialect-aware `JSONType` / `UUIDType` shims.
2. **Stateless HITL Checkpoints (No Untracked-File Loss):** Before pausing a run that modifies a repo, ALWAYS run `git add -N .` (intent-to-add) before `git diff HEAD`, enforce a `512 KB` max patch size, exclude binaries, and persist `workspace_patch` + `workspace_meta` (`repo_url`, `base_commit_sha`, `branch_name`) in `workflow_runs`. On resume, reconstruct into a fresh `git worktree` via `git apply --3way`.
3. **Reliable File Editing:** `FileToolkit.edit_file` MUST use deterministic `SEARCH/REPLACE` blocks (exact match + whitespace-normalized fallback + atomic write) — never LLM-generated unified diffs.
4. **Honest Shell Security:** `SubprocessExecutor` uses `shell=False`, strips env vars to an explicit allowlist, and NEVER allows raw interpreters (`python`, `bash`, `sh`, `node`) in `allowed_binaries`. Untrusted PR execution uses `DockerSandboxExecutor` (`--rm --network=none --cap-drop=ALL --read-only`) in Slice 6.
5. **Fail-Closed MCP Governance:** `MCPToolkit` (`runlatch/toolkits/mcp.py`, lazy-importing optional `mcp`) MUST default all discovered tools to `RiskTier.EXTERNAL`, never blindly trust remote `readOnlyHint`, namespace tools as `{server}__{tool}`, accept serializable `MCPServerConfig` (`stdio` / `streamable_http`), and route every invocation through `PolicyEngine` and `SecretRedactor`.
6. **Zero-Cost LLM Presets & Demo Mode:** `OpenAICompatibleProvider` MUST expose 1-line presets `.ollama()`, `.groq()`, and `.github_models()`. CLI `runlatch run` MUST support `--demo` backed by realistic `MockLLMProvider` traces.
7. **Safe Approval Links (`GET` Never Mutates):** Notification links point to read-only `GET /ui/approvals/{id}?sig=...&exp=...`. State transitions happen ONLY on `POST /api/v1/approvals/{id}/decision` inside a `SELECT ... FOR UPDATE` transaction.
8. **Celery `prefork` Async Safety:** Every Celery task calling `asyncio.run(...)` MUST create a task-scoped SQLAlchemy async engine with `poolclass=NullPool` and call `await engine.dispose()` in `finally`.

## 4. Slice-Gated Verification

- Zero `TODO`, `pass`, `NotImplementedError`, or placeholder stubs.
- Never start Slice `N+1` until `ruff check .`, `mypy runlatch`, and `pytest` are 100% green for Slice `N`.
