"""Security tests for SecretRedactor static patterns and dynamic runtime secrets."""

from __future__ import annotations

from runlatch.core.redactor import SecretRedactor


def test_secret_redactor_scrubs_known_token_patterns() -> None:
    redactor = SecretRedactor(dynamic_secrets=["my-custom-runtime-secret-999"])
    raw = (
        "OpenAI: sk-proj-1234567890abcdefghijklmno\n"
        "Anthropic: sk-ant-api03-1234567890abcdef\n"
        "Groq: gsk_1234567890abcdefghijklmnop\n"
        "GitHub: ghp_1234567890abcdefghijklmnop\n"
        "GitHub PAT: github_pat_11ABCDEF0123456789_abcdefghijklmnop\n"
        "AWS: AKIAIOSFODNN7EXAMPLE\n"
        "Header: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret\n"
        "Custom: my-custom-runtime-secret-999\n"
        "PEM: -----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBA...\n-----END RSA PRIVATE KEY-----"
    )
    cleaned = redactor.redact_text(raw)

    assert "sk-proj-" not in cleaned
    assert "sk-ant-" not in cleaned
    assert "gsk_123" not in cleaned
    assert "ghp_123" not in cleaned
    assert "github_pat_" not in cleaned
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
    assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
    assert "my-custom-runtime-secret-999" not in cleaned
    assert "MIIBOgIBAAJBA" not in cleaned
    assert "[REDACTED_OPENAI_KEY]" in cleaned
    assert "[REDACTED_ANTHROPIC_KEY]" in cleaned
    assert "[REDACTED_GROQ_KEY]" in cleaned
    assert "[REDACTED_GITHUB_TOKEN]" in cleaned
    assert "[REDACTED_AWS_KEY]" in cleaned
    assert "[REDACTED_BEARER_TOKEN]" in cleaned
    assert "[REDACTED_PRIVATE_KEY]" in cleaned


def test_secret_redactor_scrubs_nested_dicts_and_lists() -> None:
    redactor = SecretRedactor()
    payload = {
        "user": "alice",
        "api_key": "plain_key_value",
        "nested": {
            "Authorization": "Bearer secret_jwt_token_123",
            "items": ["safe_item", "token: ghp_1234567890abcdefghijklmnop"],
        },
    }
    scrubbed = redactor.redact_obj(payload)
    assert scrubbed["user"] == "alice"
    assert scrubbed["api_key"] == "[REDACTED]"
    assert scrubbed["nested"]["Authorization"] == "[REDACTED]"
    assert "[REDACTED_GITHUB_TOKEN]" in scrubbed["nested"]["items"][1]
