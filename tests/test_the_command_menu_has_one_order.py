"""The bot's commands are listed in one order, and that order is data.

Asked for on 2026-09-16: *status first, self-status second, everything that only lists before
everything that runs* — and a number that lets a new command be slotted **between** two existing
ones without renumbering the file.

Before this, the same 29 commands were listed three times in three different hand-made orders: the
BotFather block at the top of `telegram_support_commands.md`, the Command Details table below it,
and the Usage sections below that. The JSON was in a fourth order — the order entries happened to
be appended in — and `/spbot_list_all_command` read that one. Nothing was wrong with any of them
and no two agreed.

`menu_order` is a **float** on purpose. Whole numbers today; a command that belongs between 2 and 3
takes 2.5. Renumbering instead would touch every entry below the insertion, which makes the diff
unreadable and hides whether anything else moved with it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import shipped_config

from db_ops.telegram.command_processor import menu_order_of

MD = Path("data") / "telegram_support_commands.md"


def _configured() -> list[dict]:
    raw = json.loads(shipped_config("telegram_support_commands.json").read_bytes().decode("utf-8-sig"))
    return raw["telegram_support_commands"]


def _ranks() -> dict[str, float]:
    return {c["command_text"]: float(c["menu_order"]) for c in _configured()}


def test_every_command_carries_a_menu_order() -> None:
    missing = [c["command_text"] for c in _configured() if c.get("menu_order") is None]

    assert not missing, f"no menu_order: {missing}"


def test_no_two_commands_claim_the_same_place() -> None:
    """A tie is resolved by whatever sort happens to do, which is the unstated order this replaces."""
    ranks = [float(c["menu_order"]) for c in _configured()]

    assert len(ranks) == len(set(ranks)), "menu_order repeats"


def test_the_number_is_a_float_so_a_command_fits_between_two_others() -> None:
    """The whole point of the field. If it were declared an int, 2.5 would not survive a round trip
    through the file and the next insert would renumber twenty entries."""
    assert menu_order_of({"menu_order": 2.5}) == 2.5
    assert menu_order_of({"menu_order": "2.5"}) == 2.5
    assert menu_order_of({"menu_order": 3}) == 3.0


def test_a_command_nobody_placed_sorts_last_not_first() -> None:
    """Forgetting to place a command must not push it to the top of the menu, where it is the first
    thing an operator sees. Last is visible without being loud."""
    assert menu_order_of({}) == float("inf")
    assert menu_order_of({"menu_order": None}) == float("inf")
    assert menu_order_of({"menu_order": "not a number"}) == float("inf")


def test_status_comes_first_and_self_status_second() -> None:
    """Stated as a rule rather than left to the numbers, because it is the rule that was asked for
    and the numbers can be edited by anyone."""
    order = sorted(_ranks(), key=lambda name: _ranks()[name])

    assert order[0] == "spbot_status"
    assert order[1] == "spbot_self_status"


def test_everything_that_only_lists_comes_before_everything_that_runs() -> None:
    """A menu that opens with `restart_server` invites the wrong first click. The read-only half
    goes first, and the half that changes the estate follows it."""
    ranks = _ranks()
    listings = [name for name in ranks if name.startswith("spbot_list_")]
    # The commands that act on a database or a host, as opposed to printing what is configured.
    acting = ["spbot_backup", "spbot_restore", "spbot_run_sql_task", "spbot_shrink_log",
              "spbot_kill_spid", "spbot_start_job", "spbot_disable_job", "spbot_restart_server"]

    assert max(ranks[name] for name in listings) < min(ranks[name] for name in acting)


def test_the_most_destructive_command_is_last() -> None:
    """`restart_server` stops every service on a host. It is the end of the menu, not the middle."""
    ranks = _ranks()

    assert ranks["spbot_restart_server"] == max(ranks.values())


@pytest.mark.parametrize("section", ["botfather block", "command details table", "usage sections"])
def test_the_document_lists_them_in_that_order(section: str) -> None:
    """All three listings in the doc, because they drifted apart independently and a reader who
    scrolls past the block into the table should not meet a different order."""
    ranks = _ranks()
    text = MD.read_text(encoding="utf-8")

    if section == "botfather block":
        block = text.split("```")[1]
        found = [line.split(" - ")[0].strip() for line in block.splitlines() if " - " in line]
    elif section == "command details table":
        found = [row.split("`")[1].lstrip("/")
                 for row in re.findall(r"^\| `/spbot_[^\n]*", text, re.M)]
    else:
        found = [head.lstrip("/") for head in re.findall(r"^### `(/spbot_[a-z0-9_]+)`", text, re.M)]

    placed = [name for name in found if name in ranks]
    assert placed, f"{section}: no commands found - has the document's shape changed?"
    assert placed == sorted(placed, key=lambda name: ranks[name]), (
        f"{section} is not in menu_order")
