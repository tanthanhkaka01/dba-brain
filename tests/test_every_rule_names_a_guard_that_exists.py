"""`docs/rules.md` is the one list of the rules dbabrain keeps, and this holds the list to itself.

The rules used to be written in five places - the architecture page, the `common` and `lib` pages,
`CONTRIBUTING.md` and an internal conformance report - each a little different, and some with no
test behind them at all while reading as if they had one. A rule that names a guard nobody wrote is
worse than no rule: it tells the reader the tree is checked where it is not. So the page names its
guard for every rule, and this file checks each name against the tests that exist.

A rule with no guard yet is allowed, but only by name, in :data:`OWED_A_GUARD` - a list that may only
shrink. A rule that is a judgement no test can make says **review**, and is named in
:data:`REVIEW_ONLY`, so moving a rule there is a visible decision rather than a way out of writing a
guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "docs" / "rules.md"

#: Rules that hold but have no guard yet. Writing one's guard means naming it on the page and deleting
#: it here, in the same change. Empty since 0.24.0 (R18, R21, R22, R29, R36 were the last); a new
#: rule is written with its guard, or marked **review**.
OWED_A_GUARD: frozenset[str] = frozenset()

#: Rules no test can decide - where a thing belongs, what counts as configuration, whether a doc was
#: updated, whether a comment says why.
REVIEW_ONLY = frozenset({"R12", "R19", "R34", "R37"})

#: The pages that explain rules in their own context. Each points at the list instead of keeping a
#: second copy of it.
POINTS_HERE = ("docs/architecture.md", "docs/13_common.md", "docs/14_lib.md", "CONTRIBUTING.md")

_GUARD_RE = re.compile(r"`(tests/[\w/]+\.py)::(test_\w+)`")
_SECTION_RE = re.compile(r"^### (R\d\d)\s*$", re.MULTILINE)


def _index() -> list[dict[str, str]]:
    """The index rows: one line per rule - #, rule, mark, what is left."""
    rows = []
    for line in RULES.read_text(encoding="utf-8").splitlines():
        if not re.match(r"\| R\d\d \|", line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) == 4, f"an index row has four cells - #, rule, mark, left: {line[:80]}"
        rows.append(dict(zip(("id", "rule", "mark", "left"), cells)))
    return rows


def _sections() -> dict[str, str]:
    """Each rule's own section, ``### Rnn`` up to the next heading - where its guards are named."""
    text = RULES.read_text(encoding="utf-8")
    heads = list(_SECTION_RE.finditer(text))
    sections = {}
    for position, head in enumerate(heads):
        end = heads[position + 1].start() if position + 1 < len(heads) else len(text)
        body = text[head.end():end]
        body = re.split(r"^#{1,3} ", body, maxsplit=1, flags=re.MULTILINE)[0]
        assert head.group(1) not in sections, f"{head.group(1)} has two sections"
        sections[head.group(1)] = body
    return sections


def _guard_line(section: str) -> str:
    """What follows ``**Guard:**`` - the named tests, or ``review`` / ``none``."""
    match = re.search(r"\*\*Guard:\*\*(.*?)(?=\*\*Mark:\*\*|\Z)", section, re.DOTALL)
    return match.group(1).strip() if match else ""


def _rules() -> list[dict[str, str]]:
    sections = _sections()
    return [{**row, "guard": _guard_line(sections.get(row["id"], ""))} for row in _index()]


def test_the_page_holds_rules():
    assert len(_rules()) >= 30


def test_the_rules_are_numbered_once_with_no_gap():
    """A renumbered or repeated id breaks every reference another page makes to it. The index groups
    rules by subject, so R38-R43 stand with the layer rules - the order on the page is free, the
    numbers are not."""
    ids = [rule["id"] for rule in _index()]
    assert len(ids) == len(set(ids)), "a rule is in the index twice"
    assert sorted(ids) == [f"R{n:02d}" for n in range(1, len(ids) + 1)]


def test_every_rule_in_the_index_has_its_own_section_and_no_other_does():
    assert set(_sections()) == {rule["id"] for rule in _index()}


@pytest.mark.parametrize("rule", _rules(), ids=lambda rule: rule["id"])
def test_every_rule_names_a_guard_or_says_why_it_has_none(rule):
    guards = _GUARD_RE.findall(rule["guard"])
    if rule["id"] in OWED_A_GUARD:
        assert rule["guard"] == "none", f"{rule['id']} is owed a guard: its section says **Guard:** none"
    elif rule["id"] in REVIEW_ONLY:
        assert rule["guard"] == "review", f"{rule['id']} is a review rule: its section says **Guard:** review"
    else:
        assert guards, (f"{rule['id']} names no guard. Write one, or - only if no test can decide "
                        "it - mark it **review** and add it to REVIEW_ONLY.")


@pytest.mark.parametrize("rule", _rules(), ids=lambda rule: rule["id"])
def test_every_guard_the_page_names_exists(rule):
    for path, name in _GUARD_RE.findall(rule["guard"]):
        source = REPO / path
        assert source.exists(), f"{rule['id']} names {path}, which is not there"
        assert re.search(rf"^def {name}\(", source.read_text(encoding="utf-8"), re.MULTILINE), (
            f"{rule['id']} names {path}::{name}, which that file does not define")


def test_the_owed_list_only_names_rules_still_without_a_guard():
    """It shrinks: a rule that got its guard leaves the list in the same change."""
    unguarded = {rule["id"] for rule in _rules() if rule["guard"] == "none"}
    assert unguarded == set(OWED_A_GUARD)


def test_the_review_list_only_names_review_rules():
    review = {rule["id"] for rule in _rules() if rule["guard"] == "review"}
    assert review == set(REVIEW_ONLY)


@pytest.mark.parametrize("page", POINTS_HERE)
def test_a_page_that_explains_rules_points_at_the_list(page):
    assert "rules.md" in (REPO / page).read_text(encoding="utf-8"), (
        f"{page} explains rules and does not point at docs/rules.md - a second list drifts")
