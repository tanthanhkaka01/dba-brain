"""A release note's *Upgrading* steps are numbered in order - the reader follows them by number.

0.26.0's notes numbered item 6 twice, and said nothing of a change that broke a production task on
the first node upgraded (0.27.0 item 1.94). The numbering is the part a test can hold: inside the
*Upgrading* section an ordered list goes 1, 2, 3 ... and may start again at 1 for a second list,
never repeat or skip. Notes published before this rule are left as they were released.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RELEASES = Path(__file__).resolve().parent.parent / "docs" / "releases"
FIRST_HELD = (0, 27, 0)


def _version(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in path.stem.lstrip("v").split("."))


def _held_notes() -> list[Path]:
    return sorted(p for p in RELEASES.glob("v*.md")
                  if re.fullmatch(r"v\d+\.\d+\.\d+", p.stem) and _version(p) >= FIRST_HELD)


def _upgrading_numbers(text: str) -> list[int]:
    section = re.search(r"^## Upgrading\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not section:
        return []
    return [int(n) for n in re.findall(r"^(\d+)\.\s", section.group(1), re.M)]


def _out_of_order(numbers: list[int]) -> list[tuple[int, int]]:
    """Pairs where the next number is neither the one after, nor 1 (a new list)."""
    return [(a, b) for a, b in zip(numbers, numbers[1:]) if b not in (a + 1, 1)]


def test_the_rule_catches_the_0_26_0_slip():
    assert _out_of_order([1, 2, 3, 4, 5, 6, 6, 7]) == [(6, 6)]
    assert _out_of_order([1, 2, 3, 1, 2]) == []
    assert _out_of_order([1, 3]) == [(1, 3)]


@pytest.mark.parametrize("notes", _held_notes(), ids=lambda p: p.name)
def test_every_upgrading_list_counts_up_by_one(notes):
    numbers = _upgrading_numbers(notes.read_text(encoding="utf-8"))
    assert numbers[:1] in ([], [1]), f"{notes.name}: Upgrading starts at {numbers[0]}"
    assert not _out_of_order(numbers), f"{notes.name}: Upgrading numbered {numbers}"
