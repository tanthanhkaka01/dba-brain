"""A "you are missing a driver" message must name a package that can actually be installed.

Until 0.29.0 the repository installed under one name and the published distribution under another.
Every optional driver reported its absence with a hard-coded `pip install '<name>[...]'`, which was
right in a developer checkout and **wrong for every reader of the published package** — pip found a
different project or none, and a missing extra read as a broken toolkit. Both names are `dbabrain`
now; the hint is still built from the installed distribution, never spelled out.

Found by installing the wheel into an empty virtualenv and collecting OS metrics: ten metrics
warned at once, each naming a package that is not on PyPI. Seven such messages existed, one per
driver, so this is about all of them rather than the one that happened to fire.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from dbabrain.lib.distribution import PUBLIC_DISTRIBUTION_NAME
from dbabrain.lib.packaging import FALLBACK_DISTRIBUTION, distribution_name, install_hint

SOURCE = Path(__file__).resolve().parent.parent / "dbabrain"


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
        f"use dbabrain.lib.packaging.install_hint(extra) so it is right whatever the build is called")


DOCS = [
    path
    for path in [Path(__file__).resolve().parent.parent / "README.md",
                 *(Path(__file__).resolve().parent.parent / "docs").glob("*.md")]
    if path.is_file()
]


@pytest.mark.parametrize("path", sorted(DOCS), ids=lambda p: p.name)
def test_the_docs_name_the_published_package_not_the_repository(path: Path):
    """Documentation is exported verbatim, so it has to be right where it is read.

    Until 0.29.0 this checkout installed under another name, and a `pip install '<that name>[...]'`
    in a page was true here and false everywhere the docs are actually read - the export copies them
    unchanged. Unlike a Python module, a markdown file cannot ask the runtime what it is called, so
    the published name is the one that goes in, and this is what keeps it there.
    """
    text = path.read_text(encoding="utf-8")
    wrong = sorted(set(re.findall(r"pip install '?([A-Za-z0-9_.-]+)\[", text)) - {PUBLIC_DISTRIBUTION_NAME})
    assert not wrong, (
        f"{path.name} tells a reader to install {wrong}, which is not the published distribution "
        f"'{PUBLIC_DISTRIBUTION_NAME}'")
