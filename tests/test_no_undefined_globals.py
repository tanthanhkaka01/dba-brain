"""No function reaches for a name its module never binds.

Written because one shipped. On 2026-08-15 a refactor moved ``secret_text`` out of ``common``, and
in the rewrite ``db/cli.py`` lost the module-level ``data_sources`` import while
``_active_group_levels`` — a module-level function — still used it. Every test passed: nothing
exercised that function, because reaching it needs a live store. It was deployed, and
``ops-status`` answered ``{"ok": false, "error": "name 'data_sources' is not defined"}`` — the app
whose one job is to notice the others failing, failing silently itself.

A linter would have caught it in a second, and this repo has none installed. Rather than add a
dependency, this is the one rule that mattered: a name loaded inside a function must be bound
somewhere the function can see it — a parameter, a local, a module-level definition or import, a
builtin. Anything else is a ``NameError`` waiting for the one code path nobody covers.

Deliberately narrow. It does not check types, unused imports, shadowing, or anything else a real
linter would; those are opinions, and this is a crash.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest


DB_OPS_ROOT = Path(__file__).resolve().parents[1] / "db_ops"
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__"}


def _module_files() -> list[Path]:
    return sorted(p for p in DB_OPS_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path) -> str:
    return path.relative_to(DB_OPS_ROOT).as_posix()


def _bound_names(node: ast.AST) -> set[str]:
    """Every name this node binds: imports, assignments, defs, args, with/except/for targets."""
    bound: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in child.names}
        elif isinstance(child, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in child.names}
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            bound.add(child.id)
        elif isinstance(child, ast.arg):
            bound.add(child.arg)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            bound |= set(child.names)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            bound.add(child.name)
        elif isinstance(child, ast.MatchAs) and child.name:
            bound.add(child.name)
    return bound


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        child.id for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


@pytest.mark.parametrize("path", _module_files(), ids=_relative)
def test_every_name_a_function_loads_is_bound_somewhere(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # Module scope: top-level imports, assignments, defs, and anything bound inside a top-level
    # `try`/`if` (the conditional-import pattern this tree uses for optional drivers).
    module_bound: set[str] = set()
    for stmt in tree.body:
        module_bound |= _bound_names(stmt) if not isinstance(
            stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) else {stmt.name}

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Everything visible from inside: its own bindings (args, locals, nested imports) plus
        # the module and builtins. Enclosing-function scopes are covered because `_bound_names`
        # walks the whole subtree of every enclosing function we also visit — a name bound in an
        # outer function is bound in that outer function's own check, not this one, so only names
        # bound *nowhere* in the file are reported.
        visible = _bound_names(node) | module_bound | BUILTINS
        for name in sorted(_loaded_names(node) - visible):
            # A name bound anywhere in the file at all is somebody's local; only names the file
            # never binds are a certain NameError.
            if name not in _bound_names(tree):
                offenders.append(f"{node.name}() uses undefined name {name!r}")

    assert not offenders, (
        f"{_relative(path)}: {offenders}. The name is not a parameter, not a local, not imported "
        "at module level and not a builtin — this is a NameError on whichever path reaches it. "
        "A function-local import in a *different* function does not count: that is exactly how "
        "`db/cli.py` shipped a broken `ops-status` on 2026-08-15."
    )
