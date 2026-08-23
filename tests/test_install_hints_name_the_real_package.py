"""A "you are missing a driver" message must name a package that can actually be installed.

The repository installs as `db_ops`; the published distribution is `dbabrain`. Every optional
driver reported its absence with a hard-coded `pip install 'db_ops[...]'`, which is right in a
developer checkout and **wrong for every reader of the published package** — pip finds a different
project or none, and a missing extra reads as a broken toolkit.

Found by installing the wheel into an empty virtualenv and collecting OS metrics: ten metrics
warned at once, each naming a package that is not on PyPI. Seven such messages existed, one per
driver, so this is about all of them rather than the one that happened to fire.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from db_ops.lib.packaging import FALLBACK_DISTRIBUTION, distribution_name, install_hint

SOURCE = Path(__file__).resolve().parent.parent / "db_ops"


def test_the_hint_names_the_distribution_that_provides_this_package():
    assert install_hint("ssh") == f"pip install '{distribution_name()}[ssh]'"


def test_an_uninstalled_checkout_still_gets_a_usable_name(monkeypatch):
    """The one case metadata cannot answer, and the fallback a reader can act on."""
    distribution_name.cache_clear()
    monkeypatch.setattr("importlib.metadata.packages_distributions", lambda: {})
    try:
        assert distribution_name() == FALLBACK_DISTRIBUTION
    finally:
        distribution_name.cache_clear()


def test_broken_metadata_does_not_break_the_error_it_is_explaining(monkeypatch):
    distribution_name.cache_clear()

    def explode():
        raise RuntimeError("unreadable dist-info")

    monkeypatch.setattr("importlib.metadata.packages_distributions", explode)
    try:
        assert distribution_name() == FALLBACK_DISTRIBUTION
    finally:
        distribution_name.cache_clear()


@pytest.mark.parametrize("path", sorted(SOURCE.rglob("*.py")))
def test_no_module_hard_codes_a_distribution_name_in_an_install_hint(path: Path):
    """The literal is the defect; catching it here is cheaper than another clean-room install."""
    if path.name == "packaging.py":
        return  # where the name is resolved, and where it is explained
    text = path.read_text(encoding="utf-8")
    hard_coded = re.findall(r"pip install '[a-z_]+\[", text)
    assert not hard_coded, (
        f"{path.name} hard-codes a distribution name in an install hint ({hard_coded}); "
        f"use db_ops.lib.packaging.install_hint(extra) so it is right under both names")


DOCS = [
    path
    for path in [Path(__file__).resolve().parent.parent / "README.md",
                 *(Path(__file__).resolve().parent.parent / "docs").glob("*.md")]
    if path.is_file()
]


@pytest.mark.parametrize("path", sorted(DOCS), ids=lambda p: p.name)
def test_the_docs_name_the_published_package_not_the_repository(path: Path):
    """Documentation is exported verbatim, so it has to be right where it is read.

    `pip install 'db_ops[postgres]'` is true in this checkout and false everywhere the docs are
    actually read, because the export copies them unchanged into a tree that installs as
    `dbabrain`. Unlike a Python module, a markdown file cannot ask the runtime what it is called —
    so the published name is the one that goes in, and this is what keeps it there.
    """
    text = path.read_text(encoding="utf-8")
    wrong = re.findall(r"pip install '?db_ops\[[^\]]*\]", text)
    assert not wrong, (
        f"{path.name} tells a reader to install {wrong}, which does not exist on PyPI; "
        f"the published distribution is 'dbabrain'")
