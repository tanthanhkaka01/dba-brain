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
    from dbabrain.lib.distribution import PUBLIC_VERSION

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


# --------------------------------------------------------------------------- #
# Each version links to its own diff
# --------------------------------------------------------------------------- #
#: A section with no tag behind it, and why. Its comparison link would lead nowhere.
NEVER_TAGGED = {"0.3.4": "bumped and recorded on 2026-08-24, never tagged - its fix shipped in 0.4.0"}

_LINK_RE = re.compile(r"^\[([^\]]+)\]: (https://\S+)\s*$", re.M)


def test_every_version_links_to_what_changed_since_the_one_before() -> None:
    """Keep a Changelog ends with a link per heading: without it `## [0.39.0]` renders as bare
    brackets, and the one thing a heading can point a reader at - the diff - is not there. A link
    added by hand at release time is the step that gets forgotten, so its absence fails here."""
    text = CHANGELOG.read_text(encoding="utf-8")
    links = dict(_LINK_RE.findall(text))
    versions = [version for version, _ in _SECTION_RE.findall(text)]

    missing = [v for v in versions if v not in links and v not in NEVER_TAGGED]
    assert not missing, f"CHANGELOG.md sections with no link at the end of the file: {missing}"
    assert links.get("Unreleased", "").endswith(f"/compare/v{versions[0]}...HEAD"), (
        f"[Unreleased] must compare the newest released version, v{versions[0]}, with HEAD")


# --------------------------------------------------------------------------- #
# From 0.40.0 a version is written short (the operator, 2026-11-16)
# --------------------------------------------------------------------------- #
#: Keep a Changelog's sections in their order, with what to do on upgrade first.
SHORT_FORM_SECTIONS = ("Upgrading", "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
SHORT_FORM_FROM = (0, 40, 0)
MAX_ENTRY_LINES = 3

_HEADING_RE = re.compile(r"^## \[([^\]]+)\]", re.M)


def short_form_problems(body: str) -> list[str]:
    """What keeps one version's section from the short form; empty when it is short.

    0.39.0 ran to 263 lines, longer than its own release notes: each change a paragraph of why and
    how it was found, so the steps an upgrade needs were lost in the middle of them. The reasons
    belong in the release notes; here a change is a bullet of a line or two."""
    problems: list[str] = []
    order: list[str] = []
    entry_lines = 0
    for number, line in enumerate(body.splitlines(), start=1):
        if line.startswith("### "):
            name = line[4:].strip()
            if name not in SHORT_FORM_SECTIONS:
                problems.append(f"line {number}: '### {name}' is not one of {SHORT_FORM_SECTIONS}")
            order.append(name)
            entry_lines = 0
        elif line.startswith("- "):
            entry_lines = 1
        elif line.startswith(" ") and line.strip() and entry_lines:
            entry_lines += 1
            if entry_lines == MAX_ENTRY_LINES + 1:
                problems.append(f"line {number}: an entry runs past {MAX_ENTRY_LINES} lines")
        elif not line.strip():
            entry_lines = 0
        else:
            problems.append(f"line {number}: prose outside a bullet - {line[:60]!r}")
    known = [name for name in order if name in SHORT_FORM_SECTIONS]
    if known != sorted(known, key=SHORT_FORM_SECTIONS.index) or len(set(known)) != len(known):
        problems.append(f"sections out of order or repeated: {order}")
    return problems


def _sections_held_to_the_short_form() -> dict[str, str]:
    text = CHANGELOG.read_text(encoding="utf-8")
    text = text[:_LINK_RE.search(text).start()] if _LINK_RE.search(text) else text
    heads = list(_HEADING_RE.finditer(text))
    held = {}
    for index, head in enumerate(heads):
        name = head.group(1)
        if name != "Unreleased" and _version(name) < SHORT_FORM_FROM:
            continue
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        held[name] = text[head.end():end].split("\n", 1)[1] if "\n" in text[head.end():end] else ""
    return held


def test_unreleased_and_every_version_from_0_40_0_are_written_short() -> None:
    held = _sections_held_to_the_short_form()
    assert "Unreleased" in held, "entries land under [Unreleased]: it is held to the form as they land"

    found = {name: problems for name, body in held.items() if (problems := short_form_problems(body))}
    assert not found, (
        f"CHANGELOG.md is not in the short form (CONTRIBUTING.md, 'The changelog'): {found}. "
        "The reasons and the evidence go in docs/releases/vX.Y.Z.md.")


def test_the_short_form_check_passes_a_short_version_and_catches_a_long_one() -> None:
    short = ("\n### Upgrading\n\n- Run `dbabrain upgrade-config`: it writes `overwrite_existing: false`\n"
             "  into every restore entry that states none.\n\n### Fixed\n\n- A restore page shows its chain.\n")
    long = ("\n### Fixed\n\n- **A restore page lost its chain.** Found reading a report on one host:\n"
            "  the page read the newest row,\n  which was the wrong one,\n  so the chain was empty.\n\n"
            "  It reads the row by time now.\n\n### Upgrading\n\n- Nothing.\n")

    assert short_form_problems(short) == []
    problems = short_form_problems(long)
    assert any("past 3 lines" in problem for problem in problems)
    assert any("prose outside a bullet" in problem for problem in problems)
    assert any("out of order" in problem for problem in problems)
