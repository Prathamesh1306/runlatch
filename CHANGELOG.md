# Changelog

All notable changes to `runlatch` are documented in this file.

## [1.1.1] — 2026-11-23

### Changed
- **Live Run Inspector & HITL Approval Gate UI Redesign (`GET /ui`, `GET /ui/approvals/{id}`):**
  - Redesigned `index.html` and `approval_confirm.html` with a Linear/Vercel dark obsidian theme (`#09090b`), sticky top navbar, live status indicator, shortened 8-character monospace UUID pills with 1-click Copy-to-Clipboard, and color-coded status/risk badges.
  - Added an in-page collapsible **Live SSE Event Terminal** drawer (`EventSource('/api/v1/runs/{id}/stream')`), an optional 3-second HTMX auto-refresh toggle (`hx-select="#dashboard-body"`), and prominent **Action Required** cards with pre-signed `Review & Decide ->` links on `/ui`.
  - Added a GitHub-style unified diff viewer (`parse_unified_diff` in `runlatch/runtime/server/routes/ui.py`) with line numbers and `+`/`-`/`@@` syntax highlighting, copyable `sanitized_args` JSON card, and in-place `fetch()` decision confirmation banners on `/ui/approvals/{id}` while keeping `GET /ui/approvals/{id}` strictly read-only.

## [1.1.0] — 2026-11-23

### Added
- **Default Webhook Notification Channels (`RUNLATCH_DEFAULT_NOTIFY_CHANNELS`):** Added `default_notify_channels` to `RunLatchSettings` with `parsed_notify_channels()`; GitHub webhook runs in `runlatch/runtime/server/routes/webhooks.py` now automatically attach configured channels to `input_payload["notify_channels"]`.
- **Docker Compose `.env` Support:** Added `env_file` (with `required: false`) to `api`, `worker-agents`, `worker-notifications`, and `beat` in `docker-compose.yml`.
- **Server Scaffolding via `runlatch init --server`:** Added `--server` option to `runlatch init` to generate `.env.example` and a PyPI-ready `docker-compose.yml` installing `runlatch[mcp,server]`.

### Fixed
- **Zero-Commit Git Repo Fallback:** `GitWorkspacePatcher.capture_patch()` and `GitToolkit.git_diff()` now gracefully diff against Git's empty tree object when a repository has zero commits yet (unborn `HEAD`).

## [1.0.1] — 2026-11-22

### Changed
- **PyPI Packaging & Documentation Polish:**
  - Replaced Mermaid architecture diagram in `README.md` with a clean ASCII diagram compatible with PyPI rendering.
  - Updated quickstart install commands to `pip install runlatch` and `pip install "runlatch[mcp,server]"`, added a `.env` configuration snippet, and linked prominently to `docs/USAGE.md`.
  - Converted all relative documentation/license/trace links in `README.md` to absolute GitHub URLs.
  - Added `[project.urls]` (`Homepage`, `Repository`, `Documentation`, `Issues`, `Changelog`), `keywords`, and PyPI `classifiers` to `pyproject.toml`, and bumped version to `1.0.1`.

## [1.0.0] — 2026-11-22

### Added
- **5 Flagship Autonomous Developer Workflows:** Added `ci-triage` (`runlatch/workflows/ci_triage.py`), `dep-audit` (`runlatch/workflows/dep_audit.py`), and `doc-sync` (`runlatch/workflows/doc_sync.py`) alongside `pr-review` and `issue-to-pr`, all supporting zero-cost `--demo` execution.
- **Native Anthropic & Gemini Providers:** Added `AnthropicProvider` (`tool_use`/`tool_result`) and `GeminiProvider` (`functionDeclarations`/`functionCall`) with exponential backoff on HTTP 429/5xx/529.
- **Optional OpenTelemetry Observability:** Added `OpenTelemetrySubscriber` (`runlatch/adapters/observability/otel.py`) subscribing to `EventBus` to emit structured spans (`workflow.run`, `workflow.step`, `tool.execute`, `approval.gate`) with graceful no-op fallback when `opentelemetry` is not installed.
- **Security & Chaos Test Suite:** Added parametrized adversarial tests in `tests/security/test_injection_and_limits.py`.
- **Automated PyPI Release Workflow:** Added `.github/workflows/release.yml` for tag-triggered PyPI Trusted Publishing.

## [0.2.0] — 2026-11-22

### Added
- **FastAPI Control Plane & Safe HTMX UI (`runlatch[server]`):** Added REST API (`/api/v1/runs`, `/api/v1/approvals`, `/api/v1/webhooks/github`, `/api/v1/metrics/summary`), SSE live streaming, Live Run Inspector (`GET /ui`), and strictly read-only HITL confirmation page (`GET /ui/approvals/{id}?sig=...&exp=...`) immune to link-unfurler auto-approvals.
- **Celery + Redis 3-Queue Distributed Workers:** Added `agent.runs`, `agent.notifications`, and `agent.maintenance` queues, Beat scheduler, `NullPool` task-scoped async SQLAlchemy runner, and stateless remote worker `git clone` + `git worktree` + `git apply --3way` resume.
- **`DockerSandboxExecutor`:** Added ephemeral container execution (`--rm --network=none --cap-drop=ALL --read-only --memory=512m --cpus=1`) for untrusted PR/issue commands.
- **Pluggable Notification Engine:** Added `NotificationRouter` with Slack (Block Kit), Discord, GitHub PR comments, HMAC-signed Webhooks, and SMTP Email providers with 5x exponential backoff and `notification_deliveries` DB audit trail.
- **Alembic & Docker Compose:** Added `alembic/versions/0001_initial_schema.py`, `Dockerfile`, and 6-service `docker-compose.yml`.

## [0.1.0] — 2026-11-22

### Added
- **Hexagonal Core Runtime:** Pure domain `Agent` ReAct loop, multi-step `Workflow` engine (`StepExecutor` BYO-agent protocol), `PolicyEngine` (`RiskTier.READ/WRITE/EXEC/EXTERNAL`), `SecretRedactor`, duplicate tool-call loop detection, and pooled token/USD budget caps.
- **Tool System & Developer Toolkits:** `@tool` Pydantic v2 JSON Schema compiler with self-correction validation feedback, `FileToolkit` (deterministic `SEARCH/REPLACE` editor), `GitToolkit` (`git add -N .` + `git diff HEAD` patch capture), `ShellToolkit` (`SubprocessExecutor` blocking raw interpreters), `GitHubToolkit`, and fail-closed `MCPToolkit` (`AsyncExitStack` session reuse + glob risk overrides).
- **Unified Async Storage & CLI:** Single `SQLAlchemyCheckpointStore` supporting `sqlite+aiosqlite` and `postgresql+asyncpg`, plus Typer + Rich CLI (`runlatch init/run/runs/approvals/approve/reject/replay`).
