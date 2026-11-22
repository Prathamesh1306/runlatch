"""ShellToolkit delegating command execution to a CommandExecutor protocol.

Imports ONLY Python stdlib, runlatch.core.*, and runlatch.tools.*.
"""

from __future__ import annotations

from pathlib import Path

from runlatch.core.protocols import CommandExecutor, RegisteredTool
from runlatch.core.types import RiskTier
from runlatch.tools.registry import FunctionTool
from runlatch.tools.sandbox import resolve_safe_path


class ShellToolkit:
    """Allowlisted command execution toolkit backed by CommandExecutor."""

    def __init__(
        self,
        executor: CommandExecutor,
        workspace_root: Path | str,
        *,
        timeout_s: float = 60.0,
    ) -> None:
        self.executor = executor
        self.workspace_root = Path(workspace_root).resolve()
        self.timeout_s = timeout_s

    async def run_command(self, command: list[str], working_subdir: str = ".") -> str:
        """Run an allowlisted command (e.g. ['pytest', '-q']) inside the workspace."""
        target_cwd = resolve_safe_path(self.workspace_root, working_subdir)
        res = await self.executor.run_command(
            command, cwd=target_cwd, timeout_s=self.timeout_s
        )
        status = "TIMED_OUT" if res.timed_out else f"EXIT_{res.exit_code}"
        parts = [f"[{status} in {res.duration_ms}ms]"]
        if res.stdout.strip():
            parts.append(f"STDOUT:\n{res.stdout.strip()}")
        if res.stderr.strip():
            parts.append(f"STDERR:\n{res.stderr.strip()}")
        return "\n".join(parts)

    def tools(self) -> list[RegisteredTool]:
        return [
            FunctionTool(
                self.run_command,
                name="run_command",
                risk=RiskTier.EXEC,
                timeout_s=self.timeout_s + 5.0,
            )
        ]
