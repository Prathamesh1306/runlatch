"""Local SubprocessExecutor for trusted repositories.

Enforces shell=False, strict binary allowlist, forbidden raw interpreters
(never allows python/bash/sh/node), metacharacter blocking, and environment stripping.
Imports ONLY Python stdlib and runlatch.core.*.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Sequence
from pathlib import Path

from runlatch.core.exceptions import SandboxViolationError
from runlatch.core.types import CommandResult

FORBIDDEN_INTERPRETERS: frozenset[str] = frozenset(
    {
        "python",
        "python2",
        "python3",
        "bash",
        "sh",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "node",
        "nodejs",
        "perl",
        "ruby",
        "php",
        "lua",
        "pwsh",
        "powershell",
        "cmd",
        "curl",
        "wget",
        "nc",
        "netcat",
    }
)

_SHELL_METACHAR_RE = re.compile(r"[;&|`$<>\\\n\r\x00]")


def _is_forbidden_binary(binary_name: str) -> bool:
    lower = Path(binary_name).name.lower()
    if lower in FORBIDDEN_INTERPRETERS:
        return True
    if lower.startswith("python3.") or lower.startswith("python2."):
        return True
    return False


class SubprocessExecutor:
    """Trusted-repo subprocess command executor implementing CommandExecutor."""

    def __init__(
        self,
        *,
        allowed_binaries: Sequence[str] = ("pytest", "ruff", "mypy", "git"),
        inherit_env_allowlist: Sequence[str] = (
            "PATH",
            "HOME",
            "LANG",
            "VIRTUAL_ENV",
            "TMPDIR",
        ),
        default_timeout_s: float = 60.0,
    ) -> None:
        normalized_allowlist: set[str] = set()
        for binary in allowed_binaries:
            base = Path(binary).name
            if _is_forbidden_binary(base):
                raise SandboxViolationError(
                    f"Raw interpreter or network binary '{binary}' is forbidden in "
                    "SubprocessExecutor allowlist."
                )
            normalized_allowlist.add(base)

        self.allowed_binaries = frozenset(normalized_allowlist)
        self.inherit_env_allowlist = tuple(inherit_env_allowlist)
        self.default_timeout_s = default_timeout_s

    def _build_env(self, overrides: dict[str, str] | None) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in self.inherit_env_allowlist:
            if key in os.environ:
                env[key] = os.environ[key]
        if overrides:
            for k, v in overrides.items():
                env[str(k)] = str(v)
        return env

    def _validate_command(self, command: Sequence[str]) -> list[str]:
        if not command:
            raise SandboxViolationError("Command sequence must not be empty.")
        argv = [str(part) for part in command]
        binary_base = Path(argv[0]).name

        if _is_forbidden_binary(binary_base):
            raise SandboxViolationError(
                f"Execution of raw interpreter '{binary_base}' is strictly forbidden."
            )
        if binary_base not in self.allowed_binaries:
            allowed_list = sorted(self.allowed_binaries)
            raise SandboxViolationError(
                f"Binary '{binary_base}' is not in allowed_binaries {allowed_list}."
            )

        for token in argv:
            if _SHELL_METACHAR_RE.search(token):
                raise SandboxViolationError(
                    f"Shell metacharacter or injection token blocked in argument: '{token}'."
                )
        return argv

    async def run_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float = 60.0,
        env_overrides: dict[str, str] | None = None,
    ) -> CommandResult:
        argv = self._validate_command(command)
        effective_timeout = timeout_s if timeout_s > 0 else self.default_timeout_s
        clean_env = self._build_env(env_overrides)
        start = time.perf_counter()

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(Path(cwd).resolve()),
            env=clean_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
        except TimeoutError:
            timed_out = True
            proc.kill()
            out_b, err_b = await proc.communicate()

        duration_ms = int((time.perf_counter() - start) * 1000)
        exit_code = -1 if timed_out else (proc.returncode if proc.returncode is not None else 1)
        return CommandResult(
            command=argv,
            exit_code=exit_code,
            stdout=out_b.decode("utf-8", errors="replace"),
            stderr=err_b.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            duration_ms=duration_ms,
        )
