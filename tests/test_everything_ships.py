"""Every package ships, and the lists that could stop one say why.

The distribution went from seven withheld packages to none between `v0.1.0` and `v0.3.2`. That is
the goal, not an accident, and the interesting question is no longer "what is withheld" but
"can something be withheld again without anybody noticing why".

So these do not assert that the lists are empty — a future exclusion is allowed, and the manifest
exists to make one explainable. They assert that the lists stay *honest*: an entry names something
real, states a reason, and does not contradict the packaging.

The one exception is the test list, which keeps a single entry: a test of the Win32 Python 2.7
bridge under `tools/`, which never ships because the payload it tests never ships.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from db_ops.lib.distribution import (
    DOC_FOR_PACKAGE,
    PRIVATE_PACKAGES,
    PRIVATE_SUBPATHS,
    PRIVATE_TESTS,
    PUBLIC_PACKAGES,
)

REPO = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO / "db_ops"


def test_every_package_in_the_tree_ships_or_says_why():
    on_disk = {
        entry.name for entry in PACKAGE_ROOT.iterdir()
        if entry.is_dir() and (entry / "__init__.py").is_file() and entry.name != "__pycache__"
    }
    undecided = sorted(on_disk - set(PUBLIC_PACKAGES) - set(PRIVATE_PACKAGES))
    assert not undecided, (
        f"these packages are in neither list, so nobody decided about them: {undecided}")


def test_control_ships_because_the_manifest_it_reads_already_did():
    """The last exclusion, and the reason it did not survive review.

    `control` was withheld so that "the thing that produces the public tree" would not be in it —
    partly because it carries the private-forever list. But the list is `db_ops/lib/distribution.py`
    and that has shipped since `v0.1.0`. The map was never withheld; only the tool that reads it,
    which hid nothing and cost readers a working `bump-version`, `build-image` and `deploy`.
    """
    assert "control" in PUBLIC_PACKAGES
    assert (PACKAGE_ROOT / "lib" / "distribution.py").is_file()


@pytest.mark.parametrize("name", sorted(PRIVATE_PACKAGES))
def test_a_withheld_package_names_something_real_and_says_why(name: str):
    assert (PACKAGE_ROOT / name).is_dir(), f"{name} is withheld but does not exist"
    assert PRIVATE_PACKAGES[name].strip(), f"{name} is withheld without a reason"
    assert name in DOC_FOR_PACKAGE, f"{name} is withheld and has no component doc recorded"


@pytest.mark.parametrize("name", sorted(PRIVATE_TESTS))
def test_a_withheld_test_names_something_real_and_says_why(name: str):
    """A reason is required everywhere; existence can only be checked where it should exist.

    In the exported tree a withheld test is *supposed* to be missing, so asserting its presence
    would fail by design — the same trap `test_every_excluded_package_still_exists` fell into.
    The reason string travels with the manifest, so that half is checked in both trees.
    """
    assert PRIVATE_TESTS[name].strip(), f"{name} is withheld without a reason"
    if not (REPO / "tests" / name).is_file():
        pytest.skip(f"{name} is withheld and absent - this is the exported tree, not the source")


def test_the_only_withheld_test_is_the_one_for_a_payload_that_never_ships():
    """A tripwire, not a rule: withhold another and this fails, which is the moment to explain it."""
    assert sorted(PRIVATE_TESTS) == ["test_legacy_oracle_tool_is_python2_safe.py"], (
        "the withheld-test list changed. If that is deliberate, say why here — a test that does "
        "not ship is a check the public tree cannot re-run.")


def test_no_file_inside_a_shipped_package_is_withheld_by_name():
    """`PRIVATE_SUBPATHS` is empty, and an entry should mean the data can move instead.

    A file kept out by name is a file nobody in the public tree can see is missing. When product
    code and operator data share a directory, moving the data out - to `audits/`, with an example
    left in its place - beats withholding it.
    """
    assert PRIVATE_SUBPATHS == {}, (
        f"{sorted(PRIVATE_SUBPATHS)} are withheld from inside shipped packages. Can the data move "
        f"to audits/ with an example in its place instead?")
