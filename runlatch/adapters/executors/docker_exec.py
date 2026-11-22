"""Ephemeral DockerSandboxExecutor for untrusted PR/issue execution.

Runs commands inside an isolated container with:
`docker run --rm --network=none --cap-drop=ALL --read-only --memory=512m --cpus=1`
Enforces the same strict binary allowlist (no raw python/bash/sh) and metacharacter
blocking as SubprocessExecutor.
Imports ONLY Python stdlib and runlatch.core.*.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

from runlatch.adapters.executors.subprocess_exec import FORBIDDEN_INTERPRETERS
from runlatch.core.exceptions import SandboxViolationError
from runlatch.core.types import CommandResult

_SHELL_METACHAR_RE = re.compile(r"[;&|`$<>\\\n\r\x00]")

ProcessRunner = Callable[
    [Sequence[str], Path, float],
    Coroutine[Any, Any, tuple[int, str, str, bool]],
]


def _is_forbidden_binary(binary_name: str) -> bool:
    lower = Path(binary_name).name.lower()
    if lower in FORBIDDEN_INTERPRETERS:
        return True
    if lower.startswith("python3.") or lower.startswith("python2."):
        return True
    return False


async def _default_docker_runner(
    argv: Sequence[str], cwd: Path, timeout_s: float
) -> tuple[int, str, str, bool]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        proc.kill()
        out_b, err_b = await proc.communicate()

    code = -1 if timed_out else (proc.returncode if proc.returncode is not None else 1)
    return (
        code,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
        timed_out,
    )


class DockerSandboxExecutor:
    """Ephemeral container command executor implementing CommandExecutor."""

    def __init__(
        self,
        *,
        image: str = "python:3.12-slim",
        allowed_binaries: Sequence[str] = ("pytest", "ruff", "mypy", "git"),
        memory_limit: str = "512m",
        cpus: str = "1",
        docker_binary: str = "docker",
        default_timeout_s: float = 60.0,
        runner: ProcessRunner | None = None,
    ) -> None:
        normalized_allowlist: set[str] = set()
        for binary in allowed_binaries:
            base = Path(binary).name
            if _is_forbidden_binary(base):
                raise SandboxViolationError(
                    f"Raw interpreter '{binary}' is forbidden in DockerSandboxExecutor allowlist."
                )
            normalized_allowlist.add(base)

        self.image = image
        self.allowed_binaries = frozenset(normalized_allowlist)
        self.memory_limit = memory_limit
        self.cpus = cpus
        self.docker_binary = docker_binary
        self.default_timeout_s = default_timeout_s
        self._runner: ProcessRunner = runner or _default_docker_runner

    def _validate_inner_command(self, command: Sequence[str]) -> list[str]:
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

    def build_docker_argv(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env_overrides: dict[str, str] | None = None,
    ) -> list[str]:
        """Construct the hardened `docker run` argument vector."""
        inner_argv = self._validate_inner_command(command)
        resolved_cwd = Path(cwd).resolve()

        env_flags: list[str] = []
        if env_overrides:
            for k, v in sorted(env_overrides.items()):
                if _SHELL_METACHAR_RE.search(k) or _SHELL_METACHAR_RE.search(v):
                    raise SandboxViolationError("Invalid characters in env_overrides.")
                env_flags.extend(["-e", f"{k}={v}"])

        return [
            self.docker_binary,
            "run",
            "--rm",
            "--network=none",
            "--cap-drop=ALL",
            "--read-only",
            f"--memory={self.memory_limit}",
            f"--cpus={self.cpus}",
            "--security-opt=no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "-v",
            f"{resolved_cwd}:/workspace:rw",
            "-w",
            "/workspace",
            *env_flags,
            self.image,
            *inner_argv,
        ]

    async def run_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float = 60.0,
        env_overrides: dict[str, str] | None = None,
    ) -> CommandResult:
        docker_argv = self.build_docker_argv(
            command, cwd=cwd, env_overrides=env_overrides
        )
        effective_timeout = timeout_s if timeout_s > 0 else self.default_timeout_s
        start = time.perf_counter()

        exit_code, stdout, stderr, timed_out = await self._runner(
            docker_argv, Path(cwd).resolve(), effective_timeout
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        return CommandResult(
            command=docker_argv,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )
