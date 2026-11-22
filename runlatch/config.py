"""Configuration loader supporting environment variables (.env) and runlatch.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from runlatch.core.types import BudgetConfig, RiskTier


class ShellPolicySettings(BaseModel):
    """Shell command execution policy configuration."""

    executor: str = "subprocess"
    allowed_binaries: list[str] = Field(default_factory=lambda: ["pytest", "ruff", "mypy", "git"])
    timeout_seconds: float = 60.0
    inherit_env_allowlist: list[str] = Field(
        default_factory=lambda: ["PATH", "HOME", "LANG", "VIRTUAL_ENV", "TMPDIR"]
    )
    docker_image: str = "python:3.12-slim"


class FilesystemPolicySettings(BaseModel):
    """Filesystem access boundary configuration."""

    denied_globs: list[str] = Field(
        default_factory=lambda: [
            ".env*",
            "**/.env*",
            "**/*.pem",
            "**/*.key",
            "**/id_rsa*",
            "**/.git/config",
        ]
    )


class PolicySettings(BaseModel):
    """Top-level policy configuration loaded from runlatch.toml or env."""

    auto_approve_max_tier: RiskTier = RiskTier.WRITE
    require_approval_tools: list[str] = Field(
        default_factory=lambda: ["git_push", "github_create_pr"]
    )
    denied_tools: list[str] = Field(default_factory=list)
    shell: ShellPolicySettings = Field(default_factory=ShellPolicySettings)
    filesystem: FilesystemPolicySettings = Field(default_factory=FilesystemPolicySettings)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)


class RunLatchSettings(BaseSettings):
    """Environment and TOML-backed application settings."""

    model_config = SettingsConfigDict(
        env_prefix="RUNLATCH_",
        env_file=".env",
        extra="ignore",
    )

    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5-coder:7b"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"

    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    groq_api_key: str = Field(default="", validation_alias="GROQ_API_KEY")
    github_token: str = Field(default="", validation_alias="GITHUB_TOKEN")
    anthropic_api_key: str = Field(default="", validation_alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")

    database_url: str = "sqlite+aiosqlite:///./.runlatch.db"
    redis_url: str = "redis://localhost:6379/0"
    api_key: str = "dev-runlatch-api-key"
    github_webhook_secret: str = "dev-github-webhook-secret"
    approval_signing_secret: str = "dev-approval-signing-secret-32bytes"
    public_base_url: str = "http://localhost:8000"
    approval_ttl_minutes: int = 60

    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@runlatch.dev"
    smtp_use_tls: bool = True
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""

    max_steps: int = 15
    max_tokens: int = 80000
    max_cost_usd: float = 0.50
    auto_approve_max_tier: RiskTier = RiskTier.WRITE

    policy: PolicySettings = Field(default_factory=PolicySettings)

    def secret_values(self) -> list[str]:
        """Return non-empty sensitive credential strings for dynamic redaction."""
        candidates = [
            self.llm_api_key if self.llm_api_key not in ("", "ollama", "mock", "demo") else "",
            self.openai_api_key,
            self.groq_api_key,
            self.github_token,
            self.anthropic_api_key,
            self.gemini_api_key,
            self.api_key if self.api_key != "dev-runlatch-api-key" else "",
            self.github_webhook_secret
            if self.github_webhook_secret != "dev-github-webhook-secret"
            else "",
            self.approval_signing_secret
            if self.approval_signing_secret != "dev-approval-signing-secret-32bytes"
            else "",
            self.smtp_password,
            self.slack_webhook_url,
            self.discord_webhook_url,
        ]
        return [s for s in candidates if len(s) >= 8]

    @classmethod
    def load(cls, toml_path: Path | str | None = None) -> RunLatchSettings:
        """Load settings from environment variables and optional runlatch.toml."""
        settings = cls()
        candidate = Path(toml_path) if toml_path else Path("runlatch.toml")
        if candidate.is_file():
            raw: dict[str, Any] = tomllib.loads(candidate.read_text(encoding="utf-8"))
            if "policy" in raw and isinstance(raw["policy"], dict):
                settings.policy = PolicySettings.model_validate(raw["policy"])
                settings.max_steps = settings.policy.budget.max_steps
                settings.max_tokens = settings.policy.budget.max_tokens
                settings.max_cost_usd = settings.policy.budget.max_cost_usd
                settings.auto_approve_max_tier = settings.policy.auto_approve_max_tier
        else:
            settings.policy.auto_approve_max_tier = settings.auto_approve_max_tier
            settings.policy.budget = BudgetConfig(
                max_steps=settings.max_steps,
                max_tokens=settings.max_tokens,
                max_cost_usd=settings.max_cost_usd,
            )
        return settings
