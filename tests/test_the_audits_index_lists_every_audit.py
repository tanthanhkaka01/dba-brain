"""An audit nobody can find is an audit nobody wrote.

`audits/` holds dated snapshots — each true as of its date, never edited to match later reality.
That convention is what makes them trustworthy, and it is also what makes the index load-bearing:
the filenames carry a date and a topic slug and nothing else, so `audits/README.md` is the only
thing that says what is *in* one. A year later it is the difference between a finding you can
retrieve and forty files you would have to reopen.

It went thirteen files behind, and the reason is worth stating: the rows are paragraph-length
summaries, so falling behind costs a reading session to repair rather than a minute. The cheap
moment to write the row is when the audit is written, and this is what makes that moment
unavoidable.

`audits/` never ships, so this is navigability, not `G-02`. The suite runs in the exported tree
where the directory does not exist, hence the skip.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

AUDITS = Path(__file__).resolve().parent.parent / "audits"

pytestmark = pytest.mark.skipif(
    not AUDITS.is_dir(), reason="audits/ is private and does not exist in the exported tree")


def _linked(readme: Path) -> set[str]:
    """Markdown link targets, relative to the file they are written in.

    `removeprefix("./")` rather than `lstrip("./")`: lstrip strips a *set* of characters, so it
    turns `../CONFORMANCE_ARCHITECTURE_RULES.md` into `CONFORMANCE_ARCHITECTURE_RULES.md` and the
    link then looks broken from inside `go-live/`. It read as a real finding for a minute.
    """
    text = readme.read_text(encoding="utf-8")
    return {link.removeprefix("./") for link in re.findall(r"\]\(([^)]+\.md)\)", text)}


def test_every_audit_is_listed_in_the_index():
    linked = _linked(AUDITS / "README.md")
    on_disk = {p.name for p in AUDITS.glob("*.md")} - {"README.md"}
    missing = sorted(on_disk - linked)
    assert not missing, (
        "these audits are not in audits/README.md, so nothing but the filename says what is in "
        f"them: {missing}. Add a row when you write the audit — the summaries are long enough "
        f"that catching up later costs a reading session.")


def test_every_go_live_file_is_listed_in_its_own_index():
    """`go-live/` keeps a separate index, so the top-level one does not list its twenty files."""
    go_live = AUDITS / "go-live"
    linked = _linked(go_live / "README.md")
    on_disk = {p.name for p in go_live.glob("*.md")} - {"README.md"}
    missing = sorted(on_disk - linked)
    assert not missing, f"not listed in audits/go-live/README.md: {missing}"


@pytest.mark.parametrize("readme", ["README.md", "go-live/README.md"])
def test_the_index_never_points_at_a_file_that_is_gone(readme: str):
    """A link that resolves nowhere is worse than a missing row: it reads as a file that exists."""
    index = AUDITS / readme
    broken = sorted(
        link for link in _linked(index)
        if not (index.parent / link).exists()
    )
    assert not broken, f"{readme} links to files that do not exist: {broken}"
