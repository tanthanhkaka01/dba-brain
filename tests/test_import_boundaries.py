"""The dependency direction is a rule the tree has to keep, so it is checked, not just written down.

Two rules, and the second is the one that kept breaking silently:

1. **No app imports another app.** Apps talk through ``common``, or across a
   process boundary (the module CLIs). This one has held.
2. **No shared layer imports an app.** ``common``, ``db`` and ``logging_ops``
   sit *below* every app, so an import pointing back up inverts the layering. Every app
   already depends on ``common``; when ``common`` depends back on one, the "shared" module
   is only shared until that app changes.

Rule 2 is checked here because it is invisible at runtime — Python resolves the cycle
happily, tests pass, and the layering is gone. The 2026-08-06 audit found seven such edges,
each one added by someone who needed one function and reached for the nearest copy. Two
resolutions were available and both are in the tree: move the shared thing down
(``metrics/storage.py`` → ``common/metric_store.py``, ``telegram/severity.py`` →
``common/telegram_severity.py``, each leaving a re-export shim), or declare the module a
composition root and list it below.

``ALLOWED_UPWARD_IMPORTS`` is deliberately awkward to extend: adding an entry means writing
down why an app-independent operation could not be expressed without an app, which is the
argument that should have to be made out loud. The default answer is to move the code down.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


DB_OPS_ROOT = Path(__file__).resolve().parents[1] / "db_ops"

APPS = frozenset({
    "backup_restore", "control", "jobs", "metrics", "reports",
    "sla", "sql_tasks", "sre", "telegram", "webhost",
})
SHARED_LAYERS = frozenset({"common", "db", "logging_ops", "lib"})

#: Composition roots: CLI entry points whose whole job is to drive several apps at once.
#: Each maps to the apps it is allowed to reach, so a new one is a visible diff.
ALLOWED_UPWARD_IMPORTS: dict[str, frozenset[str]] = {
    # Deliberately empty since 2026-08-15, and that is the point: the shared layers now import no
    # app at all, so there is nothing here to keep honest.
    #
    # Two entries lived here and both were closed by moving code rather than by writing the
    # exception down again:
    #
    # `db/cli.py` — `init`/`check` compose the store's schema from the classes that own its
    # tables, and two of them lived inside `sla` and `backup_restore`. Removed 2026-08-11 by
    # taking the resolution the header recommends: move the shared thing down.
    #
    # `common/cli.py` — `check-credentials` needs the metrics target loader and the Telegram
    # SQL-command resolver, because it has to ask the question the apps ask at runtime rather
    # than a config-only imitation of it. That argument was sound; what was wrong was the
    # *location*. A command that spans two apps is not shared-layer work, so it moved to
    # `db_ops/cli.py`, a root module that is outside both rules by construction. Removed
    # 2026-08-15.
    #
    # Before adding an entry, check whether the code is in the wrong place instead. Twice now
    # the answer has been yes.
}


def _module_files() -> list[Path]:
    return sorted(
        path for path in DB_OPS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _files_under(packages: frozenset[str]) -> list[Path]:
    """Only the modules a rule actually applies to.

    Parametrizing over the whole tree and skipping the rest would report ~160 skips, which
    buries the handful of genuine skips the suite has (a missing driver, a platform-specific
    restore path) in noise. A rule that does not apply to a file is not a skipped check.
    """
    return [path for path in _module_files() if _owner_of(path) in packages]


def _owner_of(path: Path) -> str:
    return _top_package(f"db_ops.{_relative(path)}".replace("/", "."))


def _imported_db_ops_modules(path: Path) -> set[str]:
    """Every ``db_ops.<x>`` a file imports, at module level or inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has no module name; relative imports stay inside one
            # package and cannot cross a layer, so they are not interesting here.
            if node.level == 0 and node.module:
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {name for name in found if name.startswith("db_ops.")}


def _top_package(dotted: str) -> str:
    parts = dotted.split(".")
    return parts[1] if len(parts) > 1 else ""


def _relative(path: Path) -> str:
    return path.relative_to(DB_OPS_ROOT).as_posix()


@pytest.mark.parametrize("path", _files_under(APPS), ids=_relative)
def test_an_app_never_imports_another_app(path: Path) -> None:
    owner = _owner_of(path)
    offenders = sorted(
        name for name in _imported_db_ops_modules(path)
        if _top_package(name) in APPS and _top_package(name) != owner
    )
    assert not offenders, (
        f"{_relative(path)} imports another app: {offenders}. "
        "Move the shared part into db_ops/common (a data shape goes there too, as its own "
        "module: db_ops/contracts was folded in on 2026-08-15), "
        "or call the other app across its CLI."
    )


@pytest.mark.parametrize("path", _files_under(SHARED_LAYERS), ids=_relative)
def test_a_shared_layer_never_imports_an_app(path: Path) -> None:
    relative = _relative(path)
    allowed = ALLOWED_UPWARD_IMPORTS.get(relative, frozenset())
    offenders = sorted(
        name for name in _imported_db_ops_modules(path)
        if _top_package(name) in APPS and _top_package(name) not in allowed
    )
    assert not offenders, (
        f"{relative} is a shared layer but imports an app: {offenders}. "
        "Every app already depends on this layer, so this inverts the layering. "
        "Move the shared code down (leaving a re-export shim if callers exist), or — if the "
        "module really is a composition root — add it to ALLOWED_UPWARD_IMPORTS with the reason."
    )


def test_every_allowlisted_composition_root_still_exists() -> None:
    """An allowlist entry that no longer matches a file is a rule nobody is checking."""
    missing = [name for name in ALLOWED_UPWARD_IMPORTS if not (DB_OPS_ROOT / name).exists()]
    assert not missing, f"ALLOWED_UPWARD_IMPORTS names files that no longer exist: {missing}"


def test_every_allowlisted_exception_is_still_used() -> None:
    """Drop the exception once the import is gone, so the list keeps meaning something."""
    unused: list[str] = []
    for relative, allowed in ALLOWED_UPWARD_IMPORTS.items():
        imported = {_top_package(name) for name in _imported_db_ops_modules(DB_OPS_ROOT / relative)}
        stale = sorted(allowed - imported)
        if stale:
            unused.append(f"{relative} no longer imports {stale}")
    assert not unused, (
        "These allowlist entries are obsolete and should be removed: " + "; ".join(unused)
    )

def test_common_never_launches_a_db_ops_cli():
    """``common`` is the layer that performs work; it must not shell out to an app to get its job
    done.

    A shared library that spawns a CLI has taken a dependency on whatever that CLI imports -
    invisible to the import checks above and just as binding. ``common/notify_route.py`` did
    exactly that: it ran ``db_ops.common.cli telegram-route`` to read the *Telegram app's*
    settings, so the bottom layer depended on an app through a process boundary instead of an
    import. Running a command on a *target host* (``hostcmd``, ``remote_exec``) is a different
    thing entirely - that is the work, not a detour through another app.

    A module name appearing as an argparse ``prog=`` is **not** a launch, and is excluded: it is
    the label a command prints in its own ``--help``, and naming yourself is the opposite of
    shelling out. Without that exclusion ``config_admin`` could not tell its users what to type.
    """
    labels = set()
    offenders = []
    for path in sorted((DB_OPS_ROOT / "common").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                labels |= {
                    id(kw.value) for kw in node.keywords
                    if kw.arg == "prog" and isinstance(kw.value, ast.Constant)
                }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in labels:
                continue
            if node.value.startswith("db_ops.") and node.value.endswith(".cli"):
                offenders.append(f"{path.relative_to(DB_OPS_ROOT)}:{node.lineno} {node.value}")
    assert offenders == [], (
        "common must not launch a db_ops CLI: " + "; ".join(offenders))



#: Direction inside the shared tier. `common` is the bottom: it answers from what it is given, so
#: it may not reach the store. `db` owns the runtime store and may use `common` as the library it
#: is — secrets, the severity vocabulary, the shared JSON-request parser.
#: `lib` is below both: it imports nothing at all (tests/test_lib_is_pure.py).
SHARED_LAYER_ORDER = ("common", "db")


def test_the_shared_layers_do_not_import_each_other_both_ways() -> None:
    """`common` and `db` may not be mutually dependent — one direction only.

    They were, until 2026-08-15: `common/metric_store.py`, `sla_store.py` and
    `backup_restore_history.py` imported `db.backend`/`db.store` while `db/cli.py` imported
    `common.secret_text`. Nothing crashed — both ``__init__`` files are thin, so Python resolved it
    happily — and no test said a word. A cycle that runs is still a cycle: neither package can be
    reasoned about, packaged, or imported first without the other coming along.

    It was closed by moving the four store modules into `db`, which is where ORD 01 owns them
    anyway. What is left is one arrow, and this test is what keeps it one.
    """
    lower, upper = SHARED_LAYER_ORDER
    offenders = sorted(
        f"{_relative(path)} -> {name}"
        for path in _files_under(frozenset({lower}))
        for name in _imported_db_ops_modules(path)
        if _top_package(name) == upper
    )
    assert not offenders, (
        f"`{lower}` imports `{upper}`: {offenders}. The shared tier is a stack, not a pair: "
        f"`{upper}` may import `{lower}`, never the reverse. If `{lower}` needs something from "
        f"`{upper}`, it needs it passed in as a value — that is the whole point of the layer."
    )


def test_the_allowed_direction_is_actually_used() -> None:
    """The rule above is only meaningful while the arrow it permits exists.

    If `db` ever stopped importing `common`, the two would be independent and the ordering above
    would be a rule about nothing — worth deleting rather than leaving as decoration.
    """
    lower, upper = SHARED_LAYER_ORDER
    used = any(
        _top_package(name) == lower
        for path in _files_under(frozenset({upper}))
        for name in _imported_db_ops_modules(path)
    )
    assert used, (
        f"`{upper}` no longer imports `{lower}`; the two layers are independent, so "
        "SHARED_LAYER_ORDER no longer describes anything. Drop it or restate it."
    )


#: The row shapes the runtime store persists. They live in `db` beside the store that writes them
#: (moved from `common` on 2026-08-15, and from `db_ops/contracts/` before that).
SHAPE_MODULES = ("job_runs.py", "metric_results.py", "metric_definitions.py", "sla_results.py")


@pytest.mark.parametrize("name", SHAPE_MODULES)
def test_a_shape_module_imports_nothing_at_all(name: str) -> None:
    """A row shape is a leaf: it may not import anything from ``db_ops``, sibling included.

    Stricter than the rule for ``lib``, and deliberately so. ``db/store.py`` imports ``JobRun``,
    and ``common`` imports ``db``; the moment a shape reaches sideways for a helper, opening the
    store starts pulling that helper — and whatever it pulls — behind it. Today
    ``import db_ops.db.store`` costs seven modules and no library code, which is the property that
    lets the lowest layer stay the lowest layer.

    The guard used to live in ``tests/test_common_layers.py`` because the shapes used to live in
    ``common``. It moved with them: a rule left behind at the old address is a rule that stops
    being checked without anyone deciding to stop checking it.
    """
    path = DB_OPS_ROOT / "db" / name
    assert path.exists(), f"shape module missing: db/{name}"
    offenders: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            offenders |= {a.name for a in node.names if a.name.startswith("db_ops")}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                offenders.add("." * node.level + (node.module or ""))
            elif node.module and node.module.startswith("db_ops"):
                offenders.add(node.module)
    assert not offenders, (
        f"db/{name} is a row shape but imports {sorted(offenders)}. Shapes are leaves — the store "
        "and its writer both depend on them, so anything they pull in is inherited by both. Move "
        "the behaviour to the store or to the writer and leave the shape a shape."
    )


def test_the_shape_module_list_has_not_silently_shrunk() -> None:
    """Deleting a shape and its entry together would leave this rule guarding nothing."""
    missing = [name for name in SHAPE_MODULES if not (DB_OPS_ROOT / "db" / name).exists()]
    assert not missing, f"SHAPE_MODULES names files that no longer exist: {missing}"
