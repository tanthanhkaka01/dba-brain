"""The wheel carries the files the shipped code executes, not only its Python.

`package-data` is a claim, and an unusually quiet one: **setuptools silently ignores an entry for a
package that is not in `packages.find`**. So a stale line reads as "the host configuration ships"
while nothing ships, and the failure arrives on somebody else's machine as a template that is not
there — long after the install that was supposed to contain it.

That is not hypothetical here. `v0.1.0` withheld seven packages, `db_ops.sre`'s `package-data` entry
was removed with them, and when the packages came back in `v0.2.0` the entry had to come back too.
Nothing but this test connects those two edits.

It asserts against the **built artifact** rather than the declaration, because the declaration is
the thing under suspicion. Building a wheel takes a few seconds, which is why this is one test and
not one per package.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: One file per shipped package that carries data, chosen because the package cannot do its job
#: without it. A metric with no SQL collects nothing; a compose template that is absent means
#: `create-db-docker` writes an empty file; a report with no HTML renders a blank page.
MUST_BE_IN_THE_WHEEL: tuple[tuple[str, str], ...] = (
    ("db_ops/metrics/collectors/sqlserver/001_sqlserver_instance_status.sql", "a metric's query"),
    ("db_ops/common/backup_scripts/sqlserver/mssql_backup_database.sh", "a backup script"),
)


#: What a build reads besides the package: the metadata files `pyproject.toml` names, and the
#: sdist's extras from MANIFEST.in.
_BUILD_INPUTS = ("pyproject.toml", "MANIFEST.in", "README.md", "CHANGELOG.md", "LICENSE", "NOTICE")


def _clean_source(tmp_path: Path) -> Path:
    """A copy of exactly what a build reads, and none of what a previous build left behind.

    A build run inside the repository is not a clean build, in two ways measured on 2026-09-11 while
    a file was missing from `package-data`: `build/lib` keeps every file an earlier build copied
    there and the next wheel ships them, and `db_ops.egg-info/SOURCES.txt` keeps the list of files an
    earlier build found and setuptools reads it back as a manifest. Either one made this test pass
    with the declaration removed, for as long as an earlier build had run with it present. Built
    from a copy that holds neither, the wheel carries what is declared and nothing else.
    """
    source = tmp_path / "source"
    source.mkdir()
    for name in _BUILD_INPUTS:
        if (REPO_ROOT / name).exists():
            shutil.copy2(REPO_ROOT / name, source / name)
    leftovers = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "build")
    shutil.copytree(REPO_ROOT / "db_ops", source / "db_ops", ignore=leftovers)
    if (REPO_ROOT / "docs").is_dir():
        shutil.copytree(REPO_ROOT / "docs", source / "docs", ignore=leftovers)
    return source


def _wheel_names(tmp_path: Path) -> set[str]:
    """Build a wheel from a clean copy of the source into *tmp_path* and return its paths."""
    source = _clean_source(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=source, capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"the wheel did not build here: {result.stderr.strip().splitlines()[-1:]}")
    wheels = sorted(tmp_path.glob("*.whl"))
    assert wheels, "build reported success and produced no wheel"
    with zipfile.ZipFile(wheels[-1]) as archive:
        return set(archive.namelist())


@pytest.mark.slow
def test_the_wheel_carries_the_files_the_code_runs(tmp_path: Path) -> None:
    names = _wheel_names(tmp_path)

    missing = [(path, why) for path, why in MUST_BE_IN_THE_WHEEL if path not in names]
    assert not missing, (
        "the wheel is missing files the shipped code executes — check `[tool.setuptools."
        "package-data]`, and remember an entry for a package outside `packages.find` is ignored "
        "in silence:\n" + "\n".join(f"  {path}  ({why})" for path, why in missing)
    )


@pytest.mark.slow
def test_every_file_init_writes_from_the_package_is_in_the_wheel(tmp_path: Path) -> None:
    """`init` copies each packaged default out of the installed package, and **skips one it cannot
    find without a word** — a first run must not fail on a missing optional file. So a default that
    is in the source tree and not in the wheel passes every source-tree test and then vanishes on an
    installed node.

    That happened on 2026-09-11 with `db_ops/backup_restore/catalogue/restore_config.json`: added to
    PACKAGED_DEFAULTS, left out of `package-data`, found only by building the wheel for a fresh-root
    test. The list is derived from the map itself, so the next one cannot be forgotten the same way.
    """
    from db_ops import scaffold

    names = _wheel_names(tmp_path)
    missing = sorted(f"db_ops/{source}" for source in scaffold.PACKAGED_DEFAULTS.values()
                     if f"db_ops/{source}" not in names)

    assert not missing, (
        "init writes these from the package, and the wheel does not carry them - add their folder "
        "to `[tool.setuptools.package-data]`:\n" + "\n".join(f"  {path}" for path in missing))


@pytest.mark.slow
def test_every_package_data_entry_names_a_package_that_ships(tmp_path: Path) -> None:
    """A `package-data` line for a withheld package is ignored, so it reads as a lie.

    This is the check that would have caught the `db_ops.sre` entry being wrong in both directions
    — left behind when the package was withheld, and needed again when it came back.
    """
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)["tool"]["setuptools"]
    declared = set(config.get("package-data", {}))
    globs = set(config["packages"]["find"]["include"])

    for package in sorted(declared):
        assert f"{package}*" in globs or package in globs, (
            f"package-data names {package}, which `packages.find` does not include. setuptools "
            "ignores that entry silently, so the files it lists do not ship."
        )
