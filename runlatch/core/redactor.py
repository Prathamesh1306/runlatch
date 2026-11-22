"""Secret redaction engine scrubbing credentials before persistence or external dispatch.

Pure domain module: imports ONLY Python stdlib and runlatch.core.*.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

REDACTED_PLACEHOLDER = "[REDACTED]"

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Private key PEM blocks
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Anthropic keys (must precede generic sk- pattern)
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_ANTHROPIC_KEY]"),
    # OpenAI keys
    (re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b"), "[REDACTED_OPENAI_KEY]"),
    # Groq keys
    (re.compile(r"\bgsk_[A-Za-z0-9_-]{16,}\b"), "[REDACTED_GROQ_KEY]"),
    # GitHub tokens (ghp_, gho_, ghs_, ghu_, ghr_, github_pat_)
    (
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    # Slack tokens & webhooks
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    # AWS Access Key IDs
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # Bearer tokens in headers
    (
        re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s\"'`,;]+"),
        r"\1[REDACTED_BEARER_TOKEN]",
    ),
    # Key-value secret assignments (api_key=..., password=..., secret=..., token=...)
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|password|client[_-]?secret)"
            r"(\s*[:=]\s*[\"']?)([^\"'\s,;}{]{6,})([\"']?)"
        ),
        r"\1\2[REDACTED]\4",
    ),
]

_SENSITIVE_DICT_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "secret_key",
    "password",
    "passwd",
    "access_token",
    "refresh_token",
    "github_token",
    "authorization",
    "private_key",
    "client_secret",
}

_DEFAULT_ENV_SECRET_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "GITHUB_TOKEN",
    "GEMINI_API_KEY",
    "RUNLATCH_LLM_API_KEY",
)


class SecretRedactor:
    """Scrubs known token formats and dynamic runtime secrets from strings and nested payloads."""

    def __init__(
        self,
        dynamic_secrets: Sequence[str] = (),
        *,
        include_env_secrets: bool = True,
    ) -> None:
        raw_secrets = list(dynamic_secrets)
        if include_env_secrets:
            for env_name in _DEFAULT_ENV_SECRET_NAMES:
                val = os.environ.get(env_name, "").strip()
                if val and val not in ("ollama", "mock", "demo"):
                    raw_secrets.append(val)
        # Only track dynamic secrets of length >= 8 to avoid accidental false positives
        self._dynamic_secrets = sorted(
            {s for s in raw_secrets if isinstance(s, str) and len(s) >= 8},
            key=len,
            reverse=True,
        )

    def redact_text(self, text: str) -> str:
        """Scrub all static regex patterns and dynamic runtime secrets from a string."""
        if not text:
            return text
        result = text
        for secret in self._dynamic_secrets:
            if secret in result:
                result = result.replace(secret, REDACTED_PLACEHOLDER)
        for pattern, replacement in _SECRET_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    def redact_obj(self, obj: Any) -> Any:
        """Recursively scrub secrets from dicts, lists, tuples, and strings."""
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, dict):
            cleaned: dict[Any, Any] = {}
            for k, v in obj.items():
                key_lower = str(k).lower().strip()
                if key_lower in _SENSITIVE_DICT_KEYS and isinstance(v, str) and v:
                    cleaned[k] = REDACTED_PLACEHOLDER
                else:
                    cleaned[k] = self.redact_obj(v)
            return cleaned
        if isinstance(obj, list):
            return [self.redact_obj(item) for item in obj]
        if isinstance(obj, tuple):
            return tuple(self.redact_obj(item) for item in obj)
        return obj
