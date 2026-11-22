"""Automated AST check enforcing AGENTS.md §1 hexagonal import boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "runlatch"

FORBIDDEN_IN_CORE = {
    "fastapi",
    "celery",
    "redis",
    "sqlalchemy",
    "httpx",
    "mcp",
    "typer",
    "jinja2",
    "runlatch.adapters",
    "runlatch.runtime",
    "runlatch.tools",
    "runlatch.toolkits",
    "runlatch.workflows",
}

FORBIDDEN_IN_TOOLS = {
    "fastapi",
    "celery",
    "redis",
    "sqlalchemy",
    "httpx",
    "mcp",
    "typer",
    "jinja2",
    "runlatch.adapters",
    "runlatch.runtime",
    "runlatch.toolkits",
    "runlatch.workflows",
}

FORBIDDEN_IN_TOOLKITS = {
    "fastapi",
    "celery",
    "redis",
    "sqlalchemy",
    "typer",
    "jinja2",
    "runlatch.adapters",
    "runlatch.runtime",
    "runlatch.workflows",
}

FORBIDDEN_IN_WORKFLOWS = {
    "fastapi",
    "celery",
    "redis",
    "sqlalchemy",
    "typer",
    "jinja2",
    "runlatch.adapters",
    "runlatch.runtime",
}


def _imported_modules(py_file: Path) -> list[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def _matches_prefix(mod: str, forbidden_set: set[str]) -> str | None:
    for bad in forbidden_set:
        if mod == bad or mod.startswith(f"{bad}."):
            return bad
    return None


def test_hexagonal_import_boundaries() -> None:
    for py_file in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel = py_file.relative_to(PACKAGE_ROOT).as_posix()
        imports = _imported_modules(py_file)

        if rel.startswith("core/"):
            for mod in imports:
                hit = _matches_prefix(mod, FORBIDDEN_IN_CORE)
                assert hit is None, f"Hexagonal violation in runlatch/{rel}: imported '{mod}'"

        elif rel.startswith("tools/"):
            for mod in imports:
                hit = _matches_prefix(mod, FORBIDDEN_IN_TOOLS)
                assert hit is None, f"Hexagonal violation in runlatch/{rel}: imported '{mod}'"

        elif rel.startswith("toolkits/"):
            for mod in imports:
                hit = _matches_prefix(mod, FORBIDDEN_IN_TOOLKITS)
                assert hit is None, f"Hexagonal violation in runlatch/{rel}: imported '{mod}'"

        elif rel.startswith("workflows/"):
            for mod in imports:
                hit = _matches_prefix(mod, FORBIDDEN_IN_WORKFLOWS)
                assert hit is None, f"Hexagonal violation in runlatch/{rel}: imported '{mod}'"

        elif rel.startswith("adapters/"):
            parts = Path(rel).parts
            adapter_group = parts[1] if len(parts) > 2 else ""
            for mod in imports:
                assert not mod.startswith("runlatch.runtime"), (
                    f"Adapter runlatch/{rel} must not import runtime '{mod}'"
                )
                if mod.startswith("runlatch.adapters."):
                    target_group = mod.split(".")[2]
                    assert target_group == adapter_group, (
                        f"Cross-adapter import in runlatch/{rel}: '{mod}'"
                    )
