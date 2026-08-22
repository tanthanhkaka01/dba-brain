"""Where the tool believes its configuration lives, and why that cannot be its own file path.

``paths.py`` used to answer the question with one line::

    TOOL_ROOT = Path(__file__).resolve().parents[2]

That is correct in a checkout and correct in the container, because in both the package sits
beside ``data/`` and ``config.json``. It is wrong the moment the package is *installed*: on
2026-08-21 a wheel built from this tree and installed into a clean virtualenv reported its data
directory as ``site-packages/data``, a path that does not exist and never will. The tool imported
fine and could not have found a single config file.

That is the difference between a repository that is run from its checkout and a toolkit someone
else installs, so the resolution has to stop being a property of where the code sits on disk.
The order is: what the operator said, then where the operator is standing, and only then the
package's own location as a last resort — because that last one is a guess that happens to be
right in exactly two layouts.

These tests take the environment, the working directory and the package location as arguments
rather than touching the real ones, so they describe the rule instead of the machine they run on.
"""

from __future__ import annotations

from pathlib import Path

from db_ops.lib.paths import ROOT_MARKERS, resolve_data_dir, resolve_tool_root


def _make_root(tmp_path: Path, name: str) -> Path:
    """A directory that looks like a tool root: it carries the config the tool reads."""
    root = tmp_path / name
    (root / "data").mkdir(parents=True)
    (root / "config.json").write_text("{}", encoding="utf-8")
    return root


def test_an_explicit_home_wins_over_everything_else(tmp_path: Path) -> None:
    stated = _make_root(tmp_path, "stated")
    standing_in = _make_root(tmp_path, "standing_in")
    package = _make_root(tmp_path, "package")

    resolved = resolve_tool_root(home=str(stated), cwd=standing_in, package_root=package)

    assert resolved == stated


def test_a_working_directory_that_carries_the_config_is_the_root(tmp_path: Path) -> None:
    standing_in = _make_root(tmp_path, "standing_in")
    package = tmp_path / "site-packages"
    package.mkdir()

    resolved = resolve_tool_root(home=None, cwd=standing_in, package_root=package)

    assert resolved == standing_in


def test_a_working_directory_without_the_config_is_ignored(tmp_path: Path) -> None:
    """Being in an unrelated directory must not silently redefine where the config is.

    A user who runs the tool from their home directory has said nothing about configuration;
    treating the home directory as a tool root would invent an answer rather than fail honestly.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    package = _make_root(tmp_path, "package")

    resolved = resolve_tool_root(home=None, cwd=elsewhere, package_root=package)

    assert resolved == package


def test_the_package_location_is_the_last_resort_not_the_first(tmp_path: Path) -> None:
    standing_in = _make_root(tmp_path, "standing_in")
    package = _make_root(tmp_path, "package")

    resolved = resolve_tool_root(home=None, cwd=standing_in, package_root=package)

    assert resolved == standing_in, "the package's own location must lose to where the operator is"


def test_an_explicit_home_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    """A pointer to nowhere is a typo, and a typo must not fall through to a working default.

    Falling back would hide the mistake and then read a *different* estate's configuration, which
    is the one failure mode worth being loud about.
    """
    package = _make_root(tmp_path, "package")

    try:
        resolve_tool_root(home=str(tmp_path / "no-such-directory"), cwd=package, package_root=package)
    except FileNotFoundError as exc:
        assert "no-such-directory" in str(exc)
    else:
        raise AssertionError("a home that does not exist must raise, not fall back")


def test_the_data_directory_can_be_pointed_somewhere_else_on_its_own(tmp_path: Path) -> None:
    """Config beside the code is a deployment layout, not a law.

    An installed toolkit keeps its configuration wherever the operator keeps configuration, which
    is rarely next to the package.
    """
    root = _make_root(tmp_path, "root")
    elsewhere = tmp_path / "etc" / "dbops"
    elsewhere.mkdir(parents=True)

    assert resolve_data_dir(data_dir=str(elsewhere), tool_root=root) == elsewhere
    assert resolve_data_dir(data_dir=None, tool_root=root) == root / "data"


def test_an_installed_package_does_not_conclude_its_config_lives_in_site_packages(tmp_path: Path) -> None:
    """The regression this whole module exists for.

    With the package installed and the operator standing in their own configuration directory,
    the answer must be the operator's directory — never the install location.
    """
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    operator_dir = _make_root(tmp_path, "operator")

    resolved = resolve_tool_root(home=None, cwd=operator_dir, package_root=site_packages)

    assert resolved == operator_dir
    assert resolve_data_dir(data_dir=None, tool_root=resolved) == operator_dir / "data"


def test_the_markers_are_the_files_the_tool_actually_reads() -> None:
    """The marker list is the definition of "this is a tool root", so it is stated, not implied."""
    assert "data" in ROOT_MARKERS
    assert "config.json" in ROOT_MARKERS
