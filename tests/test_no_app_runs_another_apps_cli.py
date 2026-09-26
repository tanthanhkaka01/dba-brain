"""An app never runs another app's CLI from its code (R42).

The operator, 2026-09-25: *a `db_ops` CLI is not something to run casually - what components share
goes into `lib` or `common`.* An app driving another app's CLI is an import rule (R01) routed around
through a process: the two are coupled exactly as much, and the coupling is invisible to every
import check.

Two exceptions, named because running configured command lines is their job: the daemon (`jobs`
runs `app_commands.json`) and the bot (`telegram` runs its command templates). Those lines come from
configuration, not from code, and this guard reads code - an argv list written into a module.

A component re-launching **its own** CLI is not another app, and is not a hit.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


DB_OPS = Path(__file__).resolve().parents[1] / "db_ops"
APPS = frozenset({"backup_restore", "control", "jobs", "metrics", "reports", "sla", "sql_tasks",
                  "sre", "telegram", "webhost"})

#: (file, the app whose CLI it starts) - measured 2026-09-25, when there were four; empty since
#: 0.24.0, and it stays empty. Gone: `telegram`'s alias of a `reports` command, the bot's SQL task
#: listing, `reports` collecting metrics before a report (it reads the stored results), and
#: `control worker-create-db-docker` running `sre` in the worker (it builds through `common`).
CALLS_LEFT: frozenset[tuple[str, str]] = frozenset()


def _app_files() -> list[Path]:
    return sorted(p for p in DB_OPS.rglob("*.py") if "__pycache__" not in p.parts
                  and p.relative_to(DB_OPS).parts[0] in APPS and len(p.relative_to(DB_OPS).parts) > 1)


def _relative(path: Path) -> str:
    return path.relative_to(DB_OPS).as_posix()


def _apps_started(path: Path) -> set[str]:
    """The other apps whose module an argv list in this file names right after ``-m``."""
    own = path.relative_to(DB_OPS).parts[0]
    started: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        values = [item.value if isinstance(item, ast.Constant) else None for item in node.elts]
        for flag, module in zip(values, values[1:]):
            if flag == "-m" and isinstance(module, str) and module.startswith("db_ops."):
                app = module.split(".")[1]
                if app in APPS and app != own:
                    started.add(app)
    return started


@pytest.mark.parametrize("path", _app_files(), ids=_relative)
def test_an_app_does_not_start_another_apps_cli(path):
    new = sorted(app for app in _apps_started(path) if (_relative(path), app) not in CALLS_LEFT)
    assert not new, (
        f"{_relative(path)} starts {new}'s CLI. What two apps share is a rule (lib) or an operation "
        "(a common.cli command, reached through transport) - not one app driving another's CLI.")


def test_the_calls_left_only_shrink():
    """A call that was moved leaves the list in the same change."""
    gone = sorted(f"{file} -> {app}" for file, app in CALLS_LEFT
                  if not (DB_OPS / file).exists() or app not in _apps_started(DB_OPS / file))
    assert not gone, f"no longer started - delete from CALLS_LEFT: {gone}"
