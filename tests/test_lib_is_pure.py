"""``db_ops/lib/`` imports nothing from ``db_ops``, and that is the whole reason it exists.

The rule that created this package on 2026-08-15 is "an app does not import ``common``; it calls
the ``common`` CLI". That rule is right for anything that *does* something — reaching a host,
running SQL, moving a file — because those are operations, and an operation can be a process.

It cannot apply to a value. ``metrics`` builds ``MetricResult`` objects to hand to the store, and
a class does not come back from a subprocess. ``policy_engine`` classifies every metric row —
about 29,000 for one database's index inventory — and ``time_window`` is consulted on every daemon
tick; a process per call there is not a slower design, it is a broken one.

So the split is by what a thing **is**, not by who calls it: an operation goes through the CLI, a
value or a rule about values is imported. This package is the second half, and it is only
defensible while it stays a leaf. The failure mode is not dramatic — someone needs one helper,
adds one import, and the package that every component may import in-process quietly starts pulling
config, a store connection, or an app behind it. Then "apps do not import ``common``" has been
routed around rather than kept.

The single exception is ``notify``, and it is named here rather than left to be discovered: it
reads the configured notify-level vocabulary from ``db_ops.config``, lazily and failing open,
because that vocabulary is data an operator adds by registering a Telegram group. ``db_ops.config``
is a root module — config parsing, imported by everything, owning nothing — so this does not point
the layer at anything above it. Any *second* exception should be argued as hard as this one was.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


LIB_ROOT = Path(__file__).resolve().parents[1] / "db_ops" / "lib"

#: module -> the one thing it may import from db_ops, and why.
ALLOWED_DB_OPS_IMPORTS: dict[str, str] = {
    "notify.py": "db_ops.config",
    # Same shape as notify's, and allowed for the same reason: a lazy, last-resort fallback to a
    # root module. When the Telegram app's CLI cannot be reached, the level -> chat map is read
    # straight from config rather than dropping the message. `db_ops.config` owns nothing and is
    # imported by everything, so this does not point the layer at anything above it.
    "telegram_route.py": "db_ops.config",
}


def _module_files() -> list[Path]:
    return sorted(p for p in LIB_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path) -> str:
    return path.relative_to(LIB_ROOT).as_posix()


def _db_ops_imports(path: Path) -> set[str]:
    """Every ``db_ops.*`` outside this package that the file imports.

    Sibling imports (``db_ops.lib.x``, and the relative form of the same thing) are **not**
    offences: the invariant is that ``lib`` pulls nothing from *outside* ``lib``, and a helper
    calling a helper next to it drags nothing new in. ``delimited_import`` sharing a parser with
    ``xlsx_import`` is the shape this package is supposed to have. What must never appear is a
    component: config, a store, an app.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith("db_ops")}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith("db_ops"):
                found.add(node.module)
    return {name for name in found if not name.startswith("db_ops.lib")}


@pytest.mark.parametrize("path", _module_files(), ids=_relative)
def test_a_lib_module_imports_nothing_from_db_ops(path: Path) -> None:
    relative = _relative(path)
    allowed = ALLOWED_DB_OPS_IMPORTS.get(relative)
    offenders = sorted(
        name for name in _db_ops_imports(path)
        if not (allowed and (name == allowed or name.startswith(allowed + ".")))
    )
    assert not offenders, (
        f"lib/{relative} imports {offenders}. This package is imported in-process by every "
        "component precisely because it depends on nothing; an import here is inherited by all of "
        "them. Take the fact as a parameter and let the caller look it up, or the module belongs "
        "in `common` behind the CLI instead."
    )


def test_every_allowance_still_names_a_real_module() -> None:
    """An allowance for a file that no longer exists is a rule nobody is checking."""
    missing = [name for name in ALLOWED_DB_OPS_IMPORTS if not (LIB_ROOT / name).exists()]
    assert not missing, f"ALLOWED_DB_OPS_IMPORTS names files that are gone: {missing}"


def test_every_allowance_is_still_used() -> None:
    """And an allowance the module stopped needing should be deleted, not left as decoration."""
    unused = [
        name for name, allowed in ALLOWED_DB_OPS_IMPORTS.items()
        if (LIB_ROOT / name).exists()
        and not any(
            imported == allowed or imported.startswith(allowed + ".")
            for imported in _db_ops_imports(LIB_ROOT / name)
        )
    ]
    assert not unused, (
        f"These modules no longer need their exception and should lose it: {unused}")


#: The mirror rule, stated by the operator on 2026-08-17 and previously unwritten:
#:
#:     `common` may not be imported — it is only ever run as a CLI.
#:     `lib` may not run a CLI — it is only ever imported.
#:
#: The first half is `test_app_common_imports.py`. This is the second, and it is the half that had
#: no guard, which is how the two modules below arrived without anyone arguing for them.
#:
#: Both are *transport clients*: their whole job is to spawn another component's CLI and read the
#: JSON back. That makes them the opposite of what this package is for — they do something rather
#: than decide something — and it is the same two modules that hold the two import allowances
#: above, from both directions.
#:
#: They are recorded, not deleted, because the resolution is structural rather than a move:
#: `common_cli` is the transport every app uses to reach `common`, so wherever it lives, something
#: has to spawn a process. Naming the layer that owns the transport is a design decision and is
#: open in `audits/CONFORMANCE_ARCHITECTURE_RULES.md` (V7).
KNOWN_CLI_LAUNCHERS: dict[str, str] = {
    "common_cli.py": "the one client for `db_ops.common.cli` (and `db.cli` via `module=`)",
    "telegram_route.py": "falls back to `db_ops.telegram.cli` for the level -> chat map",
}


def _launches_a_process(path: Path) -> bool:
    """Does this module actually spawn something, as opposed to mentioning it in prose?

    Checked on the AST, not with a text search: every second module here has the word
    ``subprocess`` in its docstring explaining why it is *not* one, and a grep counts those.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "subprocess" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            return True
    return False


@pytest.mark.parametrize("path", _module_files(), ids=_relative)
def test_a_lib_module_does_not_run_a_cli(path: Path) -> None:
    """`lib` is imported, never spawned — and it does not spawn anything either."""
    relative = _relative(path)
    if not _launches_a_process(path):
        return

    assert relative in KNOWN_CLI_LAUNCHERS, (
        f"lib/{relative} launches a process. `lib` is the imported layer: it holds values and "
        "rules that are pure functions of their arguments. Something that spawns a CLI is an "
        "operation, and an operation belongs in `common` behind its own command."
    )


def test_no_new_module_starts_launching_a_cli() -> None:
    """The ratchet: this set may shrink, never grow."""
    actual = {_relative(p) for p in _module_files() if _launches_a_process(p)}
    assert actual == set(KNOWN_CLI_LAUNCHERS), (
        f"expected exactly {sorted(KNOWN_CLI_LAUNCHERS)} to launch a CLI, found {sorted(actual)}. "
        "If one of them stopped, delete its entry in the same commit."
    )
