"""Every picture the README shows is a file this repository ships.

The README is read in three places - on GitHub, on PyPI, and out of the source distribution - and a
relative image path only works in the first: PyPI renders the same file and shows a broken picture
for anything it cannot fetch. So the README names each picture by its full URL into the public
repository's `docs/img/`, and that is a string nothing executes. Rename a file, move the folder, or
drop the line from `MANIFEST.in`, and every other test stays green while the project's front page
shows grey boxes - on PyPI for good, because a published description cannot be edited.

A picture is also the one kind of file the export cannot read, so it refuses any that nobody named
(`distribution.PUBLIC_BINARIES`). That refusal arrives at the export, on release day; here it
arrives when the picture is added.

Four things are held together: the URL names a file that exists, the export has a decision for
that file, the source distribution carries it, and nothing sits in `docs/img/` that no page shows -
a picture nobody references is one nobody will notice going stale.
"""

from __future__ import annotations

import re
from pathlib import Path

from dbabrain.lib.distribution import PUBLIC_BINARIES

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
PICTURES = REPO_ROOT / "docs" / "img"

#: Where the public repository serves a file of its default branch from, byte for byte.
RAW_BASE = "https://raw.githubusercontent.com/tanthanhkaka01/dba-brain/main/"

#: `![alt](target)` - the target of every picture on the page, whatever it points at.
_PICTURE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")

#: `recursive-include docs/img *.jpg *.svg` - the suffixes the sdist takes from that folder.
_MANIFEST_LINE = re.compile(r"^recursive-include\s+docs/img\s+(.+)$", re.M)


def _pictures_the_readme_shows() -> list[str]:
    return _PICTURE.findall(README.read_text(encoding="utf-8"))


def _files_in_docs_img() -> list[Path]:
    return sorted(path for path in PICTURES.iterdir() if path.is_file())


def _suffixes_the_sdist_carries() -> set[str]:
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    patterns = " ".join(_MANIFEST_LINE.findall(manifest)).split()
    return {pattern.removeprefix("*") for pattern in patterns}


def _listed(names: list[str]) -> str:
    return "\n".join(f"  {name}" for name in names)


def test_the_readme_shows_pictures_at_all() -> None:
    """A pattern that stops matching would make every check below pass by reading nothing."""
    assert len(_pictures_the_readme_shows()) >= 6, (
        "The README shows four screenshots and two diagrams; fewer were found, so either they were "
        "removed or this test no longer reads the page."
    )


def test_every_picture_is_named_by_its_full_url() -> None:
    relative = [
        target for target in _pictures_the_readme_shows() if not target.startswith(RAW_BASE)
    ]
    assert not relative, (
        "PyPI renders this README too, and a picture it cannot fetch is a broken picture there. "
        f"Name each one under {RAW_BASE}docs/img/ :\n" + _listed(relative)
    )


def test_every_picture_the_readme_shows_is_a_file_in_docs_img() -> None:
    shown = {
        target for target in _pictures_the_readme_shows() if target.startswith(RAW_BASE)
    }
    missing = sorted(
        target for target in shown if not (REPO_ROOT / target.removeprefix(RAW_BASE)).is_file()
    )
    assert not missing, (
        "The README shows these pictures and the repository does not hold them, so the page shows "
        "a broken picture on GitHub and on PyPI:\n" + _listed(missing)
    )


def test_the_export_has_a_decision_for_every_picture() -> None:
    """The export refuses a file it cannot read unless somebody named it - on release day."""
    held = {path.relative_to(REPO_ROOT).as_posix() for path in _files_in_docs_img()}

    undecided = sorted(held - set(PUBLIC_BINARIES))
    assert not undecided, (
        "These are in docs/img and not in distribution.PUBLIC_BINARIES, so export-public refuses "
        "the tree. Name each one there, with where it came from:\n" + _listed(undecided)
    )

    gone = sorted(set(PUBLIC_BINARIES) - held)
    assert not gone, (
        "distribution.PUBLIC_BINARIES names files that are not there. An allowance for a file "
        "that is gone is one the next file of that name inherits unread:\n" + _listed(gone)
    )


def test_every_picture_travels_in_the_source_distribution() -> None:
    carried = _suffixes_the_sdist_carries()
    left_behind = [path.name for path in _files_in_docs_img() if path.suffix not in carried]
    assert not left_behind, (
        "MANIFEST.in does not take these from docs/img, so the sdist ships pages whose pictures "
        "are missing from the same archive:\n" + _listed(left_behind)
    )


def test_no_picture_is_kept_that_no_page_shows() -> None:
    pages = [README, *sorted((REPO_ROOT / "docs").glob("*.md"))]
    text = "\n".join(page.read_text(encoding="utf-8") for page in pages)
    orphans = [path.name for path in _files_in_docs_img() if f"img/{path.name}" not in text]
    assert not orphans, (
        "These files are in docs/img and no page shows them. A picture nobody references is one "
        "nobody notices going stale - show it or delete it:\n" + _listed(orphans)
    )
