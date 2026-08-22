"""`assets/` is two different things, and the lookup has to know which is which.

`assets/metrics/`, `backup/`, `restore/` and `host/` implement shipped capabilities: the same SQL
and the same scripts for everyone, and an install that cannot find them is not an install.
`assets/tasks/` and `assets/sql_telegram_commands/` are the opposite — written per operator and
per server, and `control/worker_data.py` mirrors the worker's copy back to the master, because
operators create task SQL through the bot.

One folder name for both meant one answer for both: `TOOL_ROOT / "assets" / ...`, which is
correct for a checkout and empty for anything pip installed.

So the lookup asks twice, in a stated order: **the operator's directory, then the package's
built-ins.** That order is what lets someone fix a query for their own environment without
forking the project, and it is why moving the built-in half into the package later is a no-op
flip rather than a switch-over — the fallback is already being consulted, it is simply empty.

When neither exists, the *operator's* path is what gets reported, so "not found" says where the
file was expected rather than pointing into site-packages at something nobody can edit.
"""

from __future__ import annotations

from pathlib import Path

from db_ops.lib.paths import BUILTIN_ASSET_ROOTS, PACKAGE_DIR, asset_candidates, asset_dir


def test_the_operator_is_asked_before_the_package() -> None:
    operator, builtin = asset_candidates("metrics", tool_root=Path("/estate"))

    assert operator == Path("/estate") / "assets" / "metrics"
    assert builtin == BUILTIN_ASSET_ROOTS["metrics"]


def test_a_kind_the_package_does_not_ship_offers_only_the_operator_path() -> None:
    """`tasks` is the operator's outright. Offering a package path that can never exist would
    only make the "not found" error harder to read."""
    assert asset_candidates("tasks", tool_root=Path("/estate")) == (
        Path("/estate") / "assets" / "tasks",
    )


def test_each_shipped_tree_lives_with_the_component_that_owns_it() -> None:
    """Not in one directory called `assets`, and not beside the package either.

    Two directories both named `assets` — one inside the package, one at the tool root — cost
    three defects in two days: a stale copy shadowing the shipped one on a worker, an operator
    unable to add a single metric without hiding all 189, and a documentation page describing a
    fallback that did not exist. Every shipped tree now sits with its owner, and nothing in the
    tree is called `assets` except the operator's own directory.
    """
    assert BUILTIN_ASSET_ROOTS["metrics"] == PACKAGE_DIR / "metrics" / "collectors"
    assert BUILTIN_ASSET_ROOTS["backup"] == PACKAGE_DIR / "common" / "backup_scripts"
    assert BUILTIN_ASSET_ROOTS["restore"] == PACKAGE_DIR / "common" / "restore_scripts"
    assert BUILTIN_ASSET_ROOTS["host"] == PACKAGE_DIR / "sre" / "host_config"

    assert not (PACKAGE_DIR / "assets").exists(), (
        "db_ops/assets is back. It is the name collision this layout exists to remove."
    )


def test_the_operator_copy_wins_when_both_exist(tmp_path: Path, monkeypatch) -> None:
    from db_ops.lib import paths

    operator = tmp_path / "estate" / "assets" / "tasks"
    operator.mkdir(parents=True)
    builtin = tmp_path / "package" / "collectors"
    builtin.mkdir(parents=True)
    monkeypatch.setitem(paths.BUILTIN_ASSET_ROOTS, "tasks", builtin)

    assert paths.asset_dir("tasks", tool_root=tmp_path / "estate") == operator


def test_the_built_in_is_used_when_the_operator_has_none(tmp_path: Path, monkeypatch) -> None:
    """The case every `pip install` is in on its first run: shipped SQL, no estate folder yet."""
    from db_ops.lib import paths

    builtin = tmp_path / "package" / "collectors"
    builtin.mkdir(parents=True)
    monkeypatch.setitem(paths.BUILTIN_ASSET_ROOTS, "metrics", builtin)

    assert paths.asset_dir("metrics", tool_root=tmp_path / "empty") == builtin


def test_neither_existing_reports_the_operator_path(tmp_path: Path, monkeypatch) -> None:
    """So the error names a directory the reader can create, not one inside the installation."""
    from db_ops.lib import paths

    monkeypatch.setitem(paths.BUILTIN_ASSET_ROOTS, "metrics", tmp_path / "package" / "collectors")
    missing = paths.asset_dir("metrics", tool_root=tmp_path / "estate")

    assert missing == tmp_path / "estate" / "assets" / "metrics"


def test_one_operator_file_does_not_hide_the_rest(tmp_path: Path, monkeypatch) -> None:
    """Adding a single query of your own must not hide the 189 that ship.

    It used to. The metric catalogue resolved ONE root — the operator's directory if it existed at
    all — and joined every variant onto it, so creating `assets/metrics/` to hold one file made the
    collector refuse to start with "variant file not found" for every shipped metric. Resolution is
    per file now, and this is the case that proves it.
    """
    from db_ops.lib import paths

    builtin = tmp_path / "package" / "collectors"
    (builtin / "postgresql").mkdir(parents=True)
    (builtin / "postgresql" / "001_shipped.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setitem(paths.BUILTIN_ASSET_ROOTS, "metrics", builtin)

    estate = tmp_path / "estate"
    mine = estate / "assets" / "metrics" / "postgresql"
    mine.mkdir(parents=True)
    (mine / "900_mine.sql").write_text("SELECT 2;", encoding="utf-8")

    assert paths.resolve_tool_path(
        "assets/metrics/postgresql/900_mine.sql", tool_root=estate) == mine / "900_mine.sql"
    assert paths.resolve_tool_path(
        "assets/metrics/postgresql/001_shipped.sql", tool_root=estate
    ) == builtin / "postgresql" / "001_shipped.sql"


def test_every_asset_reader_goes_through_the_lookup() -> None:
    """The guard: four call sites resolved `assets/` for themselves, and a fifth would too.

    Adding one is fine — adding one that spells the path out again is what puts an installed copy
    back to looking in a folder that is not there.
    """
    import re

    package = Path(__file__).resolve().parents[1] / "db_ops"
    offenders = [
        f"{path.relative_to(package).as_posix()}:{number}"
        for path in package.rglob("*.py")
        if path.relative_to(package).as_posix() != "lib/paths.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if re.search(r'(TOOL_ROOT|REPO_ROOT|PACKAGE_ROOT)\s*/\s*"assets"', line)
    ]

    assert not offenders, (
        "These spell out an assets path instead of asking db_ops.lib.paths.asset_dir /"
        f" asset_candidates, so they see only the operator's copy: {offenders}"
    )
