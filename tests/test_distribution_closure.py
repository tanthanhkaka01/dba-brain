"""Nothing the public distribution ships may import anything it leaves behind.

The distribution ships thirteen of fourteen packages, and the cut only works because whatever is
excluded sits on the far side of a line nothing crosses. That was measured on 2026-08-22, when
seven packages were withheld: 88 of 246 modules, and not one edge from the kept set into the
excluded one. Only `control` is withheld now, and the property is the same one.

**A property that holds today and is checked by nothing will stop holding.** One `from
db_ops.reports import ...` inside `db_ops/metrics/` would make the wheel unimportable, and it would
not fail here, in this repository, where every package is present — it would fail for a stranger,
after `pip install`, on a machine nobody can look at. That is the failure this file exists to move
forward in time.

The walk is static. Importing the packages to find out would need every database driver installed
and would defeat the point, which is that the public half stands alone.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from db_ops.lib.distribution import (
    PRIVATE_PACKAGES,
    PUBLIC_PACKAGES,
    is_public,
    public_package_globs,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "db_ops"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    """Every ``db_ops.*`` module this file names, without importing anything."""
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.startswith("db_ops"):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("db_ops"))
    return found


def _public_files() -> list[Path]:
    return [
        path
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts and is_public(_module_name(path))
    ]


def test_no_public_module_imports_a_private_one() -> None:
    crossings: list[str] = []
    for path in _public_files():
        me = _module_name(path)
        for imported in sorted(_imports(path)):
            if not is_public(imported):
                crossings.append(f"{me} -> {imported}")

    assert not crossings, (
        "These imports cross out of the public distribution into a package it does not ship. The "
        "wheel would install and fail on import, for a stranger, after `pip install`:\n  "
        + "\n  ".join(crossings)
    )


def test_the_walk_actually_covers_the_public_packages() -> None:
    """A filter that silently matched nothing would make the check above pass by finding nothing."""
    files = _public_files()
    assert len(files) > 80, f"only {len(files)} public modules found; the filter is broken"

    seen = {
        _module_name(path).split(".")[1]
        for path in files
        if len(_module_name(path).split(".")) > 1
    }
    missing = sorted(set(PUBLIC_PACKAGES) - seen)
    assert not missing, f"these public packages contributed no files to the walk: {missing}"


@pytest.mark.parametrize("name", sorted(PRIVATE_PACKAGES))
def test_every_excluded_package_still_exists(name: str) -> None:
    """An exclusion naming a package that is gone is folklore, and hides that the list is stale.

    Skipped where the package is absent, which is exactly the exported tree: an exclusion is
    *supposed* to be missing there, so asserting its presence would fail by design. This one
    assertion is why the whole file used to be withheld — and withholding it took the other seven
    with it, including the one that guards the public half against importing the private one,
    which is the check that matters most in the tree where it cannot be re-run.
    """
    if not (PACKAGE_ROOT / name).is_dir():
        pytest.skip(f"{name} is excluded and absent - this is the exported tree, not the source")
    assert (PACKAGE_ROOT / name).is_dir()


def test_the_two_lists_do_not_overlap() -> None:
    both = sorted(set(PUBLIC_PACKAGES) & set(PRIVATE_PACKAGES))
    assert not both, f"these are listed as both shipped and withheld: {both}"


def test_every_package_in_the_tree_has_a_verdict() -> None:
    """A new app must be a decision, not a default.

    Whichever way it defaulted would be wrong: defaulting to public ships something nobody reviewed,
    and defaulting to private makes the public tree quietly lose a capability. So it fails here
    until somebody writes the package down in `db_ops/lib/distribution.py`.
    """
    on_disk = {
        path.name
        for path in PACKAGE_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").exists() and path.name != "__pycache__"
    }
    undecided = sorted(on_disk - set(PUBLIC_PACKAGES) - set(PRIVATE_PACKAGES))
    assert not undecided, (
        "These packages are neither shipped nor withheld. Add each to PUBLIC_PACKAGES or to "
        f"PRIVATE_PACKAGES with its reason: {undecided}"
    )


def test_every_exclusion_states_why() -> None:
    unexplained = sorted(name for name, reason in PRIVATE_PACKAGES.items() if not reason.strip())
    assert not unexplained, f"excluded with no reason given: {unexplained}"


def test_the_packaging_globs_match_the_declared_set() -> None:
    """The globs this module derives are the packages it declares."""
    globs = public_package_globs()

    assert globs[0] == "db_ops", "the root package must ship, or nothing imports"
    assert set(globs[1:]) == {f"db_ops.{name}*" for name in PUBLIC_PACKAGES}
    for name in PRIVATE_PACKAGES:
        assert f"db_ops.{name}*" not in globs


def test_pyproject_ships_what_this_module_declares() -> None:
    """`pyproject.toml` and this module must not drift into shipping different software.

    The test above says that once, and used to carry this docstring while comparing the module to
    itself — `public_package_globs()` against `PUBLIC_PACKAGES`, both defined a few lines apart. It
    passed through a release in which `pyproject.toml`'s `include` still listed the seven packages
    of the previous one, which is exactly the drift it claimed to prevent. **A guard that never
    opens the file it is about is not guarding it.**
    """
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        declared = tomllib.load(handle)["tool"]["setuptools"]["packages"]["find"]["include"]

    assert set(declared) == set(public_package_globs()), (
        "pyproject.toml's include list and distribution.PUBLIC_PACKAGES disagree; the wheel "
        "would ship a different set of packages from the one the export copies."
    )
