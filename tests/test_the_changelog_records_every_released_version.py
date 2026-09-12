"""Every released version has a `CHANGELOG.md` section, and a test says so rather than a document.

v0.14.0 shipped on 2026-09-09 with complete release notes (`docs/releases/v0.14.0.md`) and no
changelog entry at all. The gap was found two days later by somebody reading the file, and the
cause was that nothing in the executed checklist asked for one: `CHANGELOG.md` appeared exactly
once across the whole release procedure, as a trailing sentence in `release_process.md` §3.4.

A step that exists in prose and in no list does not happen. So the rule lives here instead, and it
fires at the moment it can still be acted on: when `PUBLIC_VERSION` moves to the next candidate,
the version it just left has to be fully recorded.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
RELEASES = ROOT / "docs" / "releases"

#: `## [0.14.0] - 2026-09-09`
_SECTION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})\s*$", re.M)
_NOTES_RE = re.compile(r"^v(\d+\.\d+\.\d+)\.md$")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _public_version() -> str:
    from db_ops.lib.distribution import PUBLIC_VERSION

    return PUBLIC_VERSION


def _changelog_versions() -> set[str]:
    return {match.group(1) for match in _SECTION_RE.finditer(CHANGELOG.read_text(encoding="utf-8"))}


def _released_notes() -> list[str]:
    """Versions with a notes file, excluding the one still being prepared.

    `PUBLIC_VERSION` is the candidate: its notes are written at step 1 of the soak and its
    changelog section is cut at the same step, but neither is a failure until it is released. Every
    version *below* it either shipped or was abandoned — and an abandoned one never gets a notes
    file, so it never reaches this list.
    """
    current = _version(_public_version())
    found = []
    for path in RELEASES.glob("v*.md"):
        match = _NOTES_RE.match(path.name)
        if match and _version(match.group(1)) < current:
            found.append(match.group(1))
    return sorted(found, key=_version)


def test_every_released_version_has_a_changelog_section() -> None:
    missing = [version for version in _released_notes() if version not in _changelog_versions()]
    assert not missing, (
        f"released with notes but no CHANGELOG.md section: {missing}. Cut one from [Unreleased] — "
        "only what changed since the previous released version. See release_process.md §3.4a."
    )


def test_the_current_candidate_is_not_required_to_be_cut_yet() -> None:
    """The guard must not block development of the version in flight.

    Requiring a section for `PUBLIC_VERSION` would fail the suite for everybody from the moment the
    number is bumped until the release lands — a gate that stops unrelated work is a gate that gets
    deleted.
    """
    assert _public_version() not in _released_notes()


def test_the_changelog_keeps_an_unreleased_heading() -> None:
    """Entries are written as the work lands (the file's own preamble). Without the heading there
    is nowhere for them to land, and the next release reconstructs itself from a diff."""
    assert re.search(r"^## \[Unreleased\]\s*$", CHANGELOG.read_text(encoding="utf-8"), re.M)


def test_changelog_sections_are_newest_first_and_each_dated() -> None:
    """Out of order, a reader comparing two versions reads the wrong direction."""
    matches = _SECTION_RE.findall(CHANGELOG.read_text(encoding="utf-8"))
    versions = [_version(version) for version, _ in matches]

    assert versions == sorted(versions, reverse=True), (
        f"CHANGELOG.md sections are not newest first: {[v for v, _ in matches]}")
    assert len(set(versions)) == len(versions), "a version has two sections"


def test_an_abandoned_number_has_no_section() -> None:
    """A version that never finished its soak was never released, so it has nothing to say to
    somebody deciding whether to upgrade. Its content is described under the number that ships it
    (R5). 0.13.0 is the first — built, soaked two hours, superseded, never tagged."""
    notes = {match.group(1) for path in RELEASES.glob("v*.md")
             if (match := _NOTES_RE.match(path.name))}

    orphans = sorted(_changelog_versions() - notes - {_public_version()},
                     key=_version, reverse=True)
    # Versions before `docs/releases/` existed (0.4.0 is the first notes file) predate the rule.
    orphans = [version for version in orphans if _version(version) > (0, 4, 0)]
    assert not orphans, (
        f"CHANGELOG.md has a section for a version with no release notes: {orphans}. "
        "Either the notes are missing, or this number was abandoned and should not be recorded."
    )
