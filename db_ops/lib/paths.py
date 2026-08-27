"""Where the tool is on disk, and — separately — where its configuration is.

``TOOL_ROOT = Path(__file__).resolve().parents[2]`` was written out in twenty files,
``REPO_ROOT`` in four more and ``DEFAULT_DATA_DIR`` in eight. Each was correct, and each was
correct only for the depth of the file it sat in — which is exactly how a module that moves one
level deeper starts resolving ``data/`` to a folder that does not exist. That happened in this
session, to ``common/data_sources`` when it became a package: the constant still said
``parents[2]`` and the whole reports suite failed on ``db_ops/data/reports_config.json``.

Computed here and imported everywhere, the depth is stated once, in the one file whose own
location it depends on. ``tests/test_no_duplicate_definitions.py`` keeps the copies from growing
back.

**The package's location stopped being the answer on 2026-08-21.** A wheel built from this tree
and installed into a clean virtualenv resolved its data directory to ``site-packages/data`` — a
path that does not exist and never will. Deriving the configuration location from ``__file__`` is
right in exactly two layouts, a dev checkout and the container, because in both the package sits
beside ``data/``. It is wrong for every installed copy, which is what a public toolkit mostly is.

So the answer now has an order, and the package's own location is the *last* entry in it:

1. what the operator stated — ``DB_OPS_HOME`` (and ``DB_OPS_DATA_DIR`` for the data folder alone),
2. where the operator is standing, if that directory carries the config the tool reads,
3. the package's own location, as the fallback that keeps the checkout and the container working.

Step 2 requires a marker rather than accepting any working directory: a user who runs the tool
from their home directory has said nothing about configuration, and treating that directory as a
tool root would invent an answer instead of falling through to one. See
``tests/test_tool_root_resolution.py``.

The resolvers take the environment, the working directory and the package location as arguments,
defaulting to the real ones. That is what lets the rule be tested without a virtualenv and a
built wheel — the failure above was found by building one, and it should not take building one
to find the next.
"""

from __future__ import annotations

import os
from pathlib import Path


#: Environment variable naming the tool root outright. Set it and the search stops.
HOME_ENV_VAR = "DB_OPS_HOME"

#: Environment variable naming the data folder alone, for an installed copy whose configuration
#: lives where the operator keeps configuration rather than beside the code.
DATA_DIR_ENV_VAR = "DB_OPS_DATA_DIR"

#: What makes a directory a tool root: it holds the files the tool reads. ``data/`` is the folder
#: every app loads its configuration from; ``config.json`` is the runtime paths file. Either one
#: is enough — a deployment that keeps only one of them is still unambiguous.
ROOT_MARKERS: tuple[str, ...] = ("data", "config.json")

#: The file inside ``data/`` that says which of its neighbours are configuration at all. Two
#: components need the name and neither owns it: ``db_ops.db.config_sync`` reads it to decide what
#: may enter the runtime store, and ``db_ops.lib.config_bundle`` reads it to decide what crosses
#: to another machine. Spelled here so those two can never come to disagree about which file they
#: mean — ``tests/test_no_duplicate_definitions.py`` is what noticed they had started to.
CONFIG_CATALOG_FILENAME = "config_catalog.json"

#: The package's own location: the directory holding ``db_ops/``. Correct for a dev checkout and
#: for the container (``/app/tools/db_ops``); meaningless for an installed copy, which is why it
#: is the last resort and not the definition.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


#: The package directory itself — ``db_ops/``. Derived from this file's own location on purpose,
#: and legitimately: this is the one module allowed to know how deep the package sits (see
#: ``tests/test_no_self_derived_project_root.py``). It asks "where is my code", which is what a
#: shipped file follows — unlike configuration, which follows the operator.
PACKAGE_DIR = Path(__file__).resolve().parents[1]

#: Where each **shipped** asset tree physically lives, keyed by the name configuration calls it.
#:
#: The two halves used to be two directories both called ``assets`` — one inside the package, one
#: at the tool root — and that collision cost three defects in two days: a stale copy on a worker
#: silently shadowing the shipped one, an operator unable to add a single metric without hiding
#: all 189, and a documentation page confidently describing a fallback that did not exist. The
#: shipped files now live with the component that owns them, and nothing in the tree is called
#: ``assets`` except the operator's own directory.
#:
#: Configuration still says ``assets/backup/...``. That is deliberate: it is the operator's
#: vocabulary, it is written into every estate's ``restore_config.json``, and it names *what it
#: wants* rather than where the installer put it. This map is the one place the two meet, so they
#: cannot drift.
BUILTIN_ASSET_ROOTS: dict[str, Path] = {
    "metrics": PACKAGE_DIR / "metrics" / "collectors",
    "backup": PACKAGE_DIR / "common" / "backup_scripts",
    "restore": PACKAGE_DIR / "common" / "restore_scripts",
    "host": PACKAGE_DIR / "sre" / "host_config",
}

#: Asset trees the operator owns outright. They ship with no built-in copy, so a miss is a miss.
OPERATOR_ASSET_KINDS: frozenset[str] = frozenset({"tasks", "sql_telegram_commands"})


def builtin_asset_root(kind: str) -> Path | None:
    """Where the package keeps *kind*, or ``None`` when the operator owns it outright."""
    return BUILTIN_ASSET_ROOTS.get(kind)


def asset_candidates(*parts: str, tool_root: Path | None = None) -> tuple[Path, ...]:
    """Where to look for one asset: the operator's copy first, the package's second.

    The operator's copy wins so that fixing one query for your own environment does not mean
    forking the project. When the package ships nothing of that kind — ``tasks``,
    ``sql_telegram_commands`` — only the operator's path comes back, because there is nothing to
    fall back to and offering a path that can never exist only makes the error harder to read.
    """
    root = tool_root if tool_root is not None else TOOL_ROOT
    operator = root.joinpath("assets", *parts)
    builtin_root = builtin_asset_root(parts[0]) if parts else None
    if builtin_root is None:
        return (operator,)
    return (operator, builtin_root.joinpath(*parts[1:]))


def asset_dir(*parts: str, tool_root: Path | None = None) -> Path:
    """The first of :func:`asset_candidates` that exists as a directory.

    Neither existing returns the *operator's* path, so a "not found" says where the operator was
    expected to put it rather than pointing into site-packages.

    **Directory granularity is the wrong unit for anything that names individual files** — an
    operator who creates ``assets/metrics/`` to add one query would hide every shipped one. Use
    :func:`resolve_tool_path` per file for those; this is for callers that genuinely want a
    directory.
    """
    candidates = asset_candidates(*parts, tool_root=tool_root)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def resolve_tool_path(path: str | Path, *, tool_root: Path | None = None) -> Path:
    """A relative path a config gave, made real — falling back to the shipped copy **per file**.

    Configuration names a shipped script the way an operator sees the tree:
    ``"assets/backup/sqlserver/mssql_backup_database.sh"``. The file itself lives with the
    component that owns it, and the config should not have to know that.

    Per file, not per directory, and that distinction is the whole point: the operator's tree wins
    for the files it actually contains, and everything else still comes from the package. That is
    what lets someone override one query without copying the other 188.
    """
    path = Path(path)
    if path.is_absolute():
        return path
    root = tool_root if tool_root is not None else TOOL_ROOT
    operator = root / path
    if operator.exists():
        return operator
    parts = path.parts
    if len(parts) > 1 and parts[0] == "assets":
        builtin_root = builtin_asset_root(parts[1])
        if builtin_root is not None:
            builtin = builtin_root.joinpath(*parts[2:])
            if builtin.exists():
                return builtin
    return operator


def looks_like_tool_root(candidate: Path) -> bool:
    """True when *candidate* carries at least one of :data:`ROOT_MARKERS`."""
    return any((candidate / marker).exists() for marker in ROOT_MARKERS)


def resolve_tool_root(
    *,
    home: str | None = None,
    cwd: Path | None = None,
    package_root: Path | None = None,
) -> Path:
    """The project root, by the order documented at the top of this module.

    ``home`` defaults to ``DB_OPS_HOME``, ``cwd`` to the process's working directory and
    ``package_root`` to :data:`PACKAGE_ROOT`; each is an argument so the rule can be tested
    without standing in a particular directory.

    A ``home`` that does not exist raises rather than falling through. Falling through would
    swallow a typo and then read a *different* estate's configuration — the one failure here
    worth being loud about.
    """
    stated = home if home is not None else os.environ.get(HOME_ENV_VAR, "").strip()
    if stated:
        root = Path(stated).expanduser()
        if not root.is_dir():
            raise FileNotFoundError(
                f"{HOME_ENV_VAR} points at a directory that does not exist: {root}"
            )
        return root.resolve()

    standing_in = Path(cwd) if cwd is not None else Path.cwd()
    if looks_like_tool_root(standing_in):
        return standing_in.resolve()

    return (package_root if package_root is not None else PACKAGE_ROOT).resolve()


def resolve_data_dir(*, data_dir: str | None = None, tool_root: Path | None = None) -> Path:
    """Where ``data/*.json`` lives.

    Separate from the tool root because the two move independently once the tool is installed:
    the code goes wherever pip puts it, while the configuration stays where the operator keeps
    configuration.
    """
    stated = data_dir if data_dir is not None else os.environ.get(DATA_DIR_ENV_VAR, "").strip()
    if stated:
        return Path(stated).expanduser().resolve()
    return (tool_root if tool_root is not None else resolve_tool_root()) / "data"


#: The project root — the directory holding ``db_ops/``, ``data/``, ``assets/`` and
#: ``config.json``. On the worker this is ``/app/tools/db_ops``; in a dev checkout it is the
#: repository root; for an installed copy it is whatever the operator pointed at.
TOOL_ROOT = resolve_tool_root()

#: Historical spelling of the same directory. Kept because four modules use this name for it and
#: renaming them would be churn with no reader; it is an alias, not a second value.
REPO_ROOT = TOOL_ROOT

#: Where ``data/*.json`` lives. Callers that take a ``data_dir`` argument should keep taking one —
#: this is the default for the ones that do not, not a licence to stop passing it.
DEFAULT_DATA_DIR = resolve_data_dir(tool_root=TOOL_ROOT)
