"""The root package holds nothing of its own - `db-ops` only dispatches (R41).

The operator, 2026-09-25: *`db_ops` is not an app, and it should not be run as a CLI like that.*
`db_ops/cli.py` is the `db-ops` / `dbabrain` command a person types, and it dispatched `db-ops
<component> ...` - but it also answered six commands of its own and imported two apps, `common`,
`db` and `logging_ops`, and its docstring said it sat outside the import rules *by construction*.
A module outside every rule is where the rules stop being true. So the root keeps the entry point
and nothing else: a command it answers itself is an operation (`common`) or a rule (`lib`).

The baselines below are what was there on 2026-09-25. They may only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path


DB_OPS = Path(__file__).resolve().parents[1] / "db_ops"
ROOT_CLI = DB_OPS / "cli.py"

#: What the root may hold: the package marker (re-exporting the version), the entry point, and
#: the `db_ops.config` alias of `lib.config`.
ROOT_MODULES = frozenset({"__init__.py", "cli.py", "config.py"})
ROOT_MODULES_LEFT: frozenset[str] = frozenset()

#: `argv[0]` values the dispatcher answers without dispatching - asking for help or the version.
NOT_COMMANDS = frozenset({"-h", "--help", "help", "-V", "--version"})
#: Empty since 0.24.0: `check-credentials` was the last, and it is a `common.cli` command (its two
#: resolvers moved to `lib.data_sources`), reached through ALIASES like `init`.
ROOT_COMMANDS_LEFT: frozenset[str] = frozenset()

#: Empty with it: the three were check-credentials's.
ROOT_IMPORTS_LEFT: frozenset[str] = frozenset()


def _tree() -> ast.AST:
    return ast.parse(ROOT_CLI.read_text(encoding="utf-8"), filename=str(ROOT_CLI))


def _own_commands() -> set[str]:
    """Every string ``argv[0]`` is compared with - the commands the root answers itself."""
    found: set[str] = set()
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Subscript) and getattr(left.value, "id", "") == "argv"
                and isinstance(left.slice, ast.Constant) and left.slice.value == 0):
            continue
        for comparator in node.comparators:
            for item in ast.walk(comparator):
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    found.add(item.value)
    return found - NOT_COMMANDS


def _outside_imports() -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_tree()):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for name in names:
            parts = name.split(".")
            if parts[0] == "db_ops" and (len(parts) < 2 or parts[1] != "lib"):
                found.add(".".join(parts[:2]))
    return found


def test_the_root_holds_only_its_entry_point():
    extra = {p.name for p in DB_OPS.glob("*.py")} - ROOT_MODULES - ROOT_MODULES_LEFT
    assert not extra, f"db_ops/{sorted(extra)}: the root holds nothing of its own - lib or common"


def test_the_root_cli_answers_no_command_of_its_own():
    new = sorted(_own_commands() - ROOT_COMMANDS_LEFT)
    assert not new, (f"db_ops/cli.py answers {new} itself. It dispatches `db-ops <component>`; a "
                     "command belongs to common (an operation) or lib (a rule).")


def test_the_root_cli_imports_only_lib():
    new = sorted(_outside_imports() - ROOT_IMPORTS_LEFT)
    assert not new, f"db_ops/cli.py imports {new}; the dispatcher imports lib and names components as strings"


def test_the_baselines_only_shrink():
    stale = sorted(ROOT_COMMANDS_LEFT - _own_commands()) + sorted(ROOT_IMPORTS_LEFT - _outside_imports()) \
        + sorted(name for name in ROOT_MODULES_LEFT if not (DB_OPS / name).exists())
    assert not stale, f"gone - delete from the baselines: {stale}"
