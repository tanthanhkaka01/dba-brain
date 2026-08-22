"""`common/restore/` must stay shippable on its own: no config, no data, no app.

The goal this protects is packaging. `db_ops/common/` is to be obfuscated and dropped into any
directory, imported by callers that are not this repo, with no `data/` beside it and no
`config.json` anywhere. Every rule below is a thing that breaks that, and every one of them breaks
it *silently at the customer's site* rather than here - a module that reads `db_instances.json`
imports fine and only fails when someone asks it a question, on a machine where the file was never
going to exist.

A promise in a doc does not survive a year of edits. This is the same promise, executable.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[1] / "db_ops" / "common" / "restore"

#: Modules whose whole purpose is to reach data or config. Importing any of them from the restore
#: API means the API can no longer answer from its input alone.
DATA_REACHING = frozenset({
    "db_ops.config", "db_ops.common.data_sources",
    "db_ops.lib.secret_text", "db_ops.db",
})

APPS = frozenset({
    "backup_restore", "control", "jobs", "metrics", "reports",
    "sla", "sql_tasks", "sre", "telegram", "webhost",
})


def _modules() -> list[Path]:
    """Recursive: a subpackage is exactly where a data read would hide from a top-level glob."""
    return sorted(PACKAGE.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_package_has_modules_to_check():
    """A glob that silently matched nothing would make every test below vacuously true."""
    assert _modules(), f"no modules found under {PACKAGE}"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(PACKAGE)))
def test_no_module_reaches_config_or_the_data_folder(path: Path):
    """The API answers from its input. A lookup here would make the same request mean different
    things on two machines, with nothing in the request to say why."""
    offenders = sorted(
        name for name in _imports(path)
        if any(name == blocked or name.startswith(blocked + ".") for blocked in DATA_REACHING)
    )
    assert not offenders, (
        f"common/restore/{path.relative_to(PACKAGE)} imports {offenders}, which read config or data. "
        "This layer takes resolved values as parameters; the caller does the lookup "
        "(see db_ops/backup_restore/spec_builder.py)."
    )


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(PACKAGE)))
def test_no_module_imports_an_app(path: Path):
    """Every app depends on this layer. Reaching back up inverts it and makes the package
    unshippable without dragging an app along."""
    offenders = sorted(
        name for name in _imports(path)
        if name.startswith("db_ops.") and name.split(".")[1] in APPS
    )
    assert not offenders, f"common/restore/{path.relative_to(PACKAGE)} imports an app: {offenders}."


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(PACKAGE)))
def test_no_module_opens_a_file(path: Path):
    """Belt and braces for the rule above: a hand-rolled `open()`/`read_text()` would slip past an
    import check while doing exactly the thing the import check exists to stop."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    reads = {"open", "read_text", "read_bytes"}
    offenders = sorted({
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id in reads)
             or (isinstance(node.func, ast.Attribute) and node.func.attr in reads))
    })
    assert not offenders, (
        f"common/restore/{path.relative_to(PACKAGE)} reads from disk ({offenders}). The spec carries the values; "
        "nothing here should need a file."
    )


def test_the_package_imports_with_no_config_present(tmp_path, monkeypatch):
    """The end goal, at its bluntest: import it from a directory that holds nothing at all.

    Run from `tmp_path` so a stray relative path to `data/` or `config.json` would have to resolve
    against an empty folder - which is what a packaged copy on a customer's machine looks like.
    """
    monkeypatch.chdir(tmp_path)
    import importlib

    module = importlib.import_module("db_ops.common.restore")
    importlib.reload(module)

    assert module.parse_restore_spec is not None
