"""A command that asks for one `yes` must guard an operation that costs one `yes`.

Two files decide a confirmed command, and neither knows about the other. The catalogue says how
many answers the bot **collects** — one `confirm` parameter is one answer. `emergency_operations`
says how many the gate **demands**, and an operation it does not list is priced at the strictest
level: two answers, the second the target's own id typed out. Nothing made those numbers agree, and
they disagreed in the way that is hardest to see: on the estate they matched, and on a fresh
install they did not.

Found on 2026-09-04 while cutting v0.7.0. `db-ops init` wrote the command catalogue and **not**
`emergency_operations.json`, and the package did not carry one — so every install that was not this
one priced `kill-spid`, `shrink-log`, `start-job`, `disable-job` and `run-sql-task` at level 100
while the commands collected a single `yes`. The gate then refused with "the request does not carry
answer 2 of 2", which reads like a bug in the command. The public tree's suite is where the two
worlds separated: the same tests passed here and failed there, because the file only exists here.

So this asserts the pairing against **what an install actually receives** — the packaged catalogue
and the packaged ladder, not this operator's `data/`.
"""

import json
from pathlib import Path

import pytest

from db_ops.common import confirm

PACKAGE = Path(confirm.__file__).resolve().parents[1]
COMMANDS = PACKAGE / "telegram" / "catalogue" / "telegram_support_commands.json"
LADDER = PACKAGE / "common" / "catalogue" / "emergency_operations.json"


def _confirmed_commands() -> list[tuple[str, str, int]]:
    """``(command, operation, answers it collects)`` for every command that names an operation."""
    entries = json.loads(COMMANDS.read_bytes().decode("utf-8-sig"))["telegram_support_commands"]
    found = []
    for entry in entries:
        config = entry.get("action_config") or {}
        operation = str(config.get("emergency_operation") or "").strip()
        if not operation:
            continue
        # A parameter whose validator accepts only the confirmation word, or whose name is the
        # gate's own field, is an answer. Everything else is an argument.
        answers = sum(
            1 for item in (config.get("parameters") or [])
            if str(item.get("name") or "") in {"confirm", "confirm_target"}
        )
        found.append((entry["command_text"], operation, answers))
    return found


def test_the_package_carries_the_ladder() -> None:
    """Without it, `load_operation` prices every operation at the strictest level and says nothing."""
    assert LADDER.is_file(), (
        f"{LADDER} is missing: a fresh install then confirms every operation at level 100 while "
        "its commands collect one answer, and every confirmed command is refused.")


@pytest.mark.parametrize("command, operation, collected", _confirmed_commands())
def test_a_command_collects_exactly_the_answers_its_operation_demands(
    command: str, operation: str, collected: int
) -> None:
    """Not "at least": a spare answer means a prompt the operator is asked and nothing reads."""
    rules = confirm.load_operation(operation, path=LADDER)

    assert collected == rules["confirmations"], (
        f"/{command} collects {collected} answer(s) but {operation} costs "
        f"{rules['confirmations']} (level {rules['level']}). The command and the ladder are "
        "edited in different files; they have to be changed together.")


def test_every_named_operation_is_in_the_ladder() -> None:
    """An operation the file does not list is not a smaller risk — it is an unpriced one."""
    listed = set(json.loads(LADDER.read_bytes().decode("utf-8-sig"))["operations"])
    named = {operation for _, operation, _ in _confirmed_commands()}

    assert named <= listed, (
        f"commands name operations the ladder does not price: {sorted(named - listed)}. They cost "
        "two answers each and collect one, so they are refused wherever they run.")


def test_the_scaffold_writes_the_ladder_into_a_new_tool_root(tmp_path: Path) -> None:
    """`init` is the only thing that turns the packaged copy into a file the gate reads."""
    from db_ops import scaffold

    scaffold.initialise(tmp_path, app_name="dbabrain")

    written = tmp_path / "data" / "emergency_operations.json"
    assert written.is_file()
    assert "kill-spid" in json.loads(written.read_bytes().decode("utf-8-sig"))["operations"]


def test_an_install_with_no_ladder_of_its_own_is_priced_by_the_packaged_one(tmp_path) -> None:
    """The case that was broken: installed, not yet initialised, every command refused."""
    rules = confirm.load_operation("kill-spid", path=tmp_path / "nothing here.json")

    assert rules["confirmations"] == 1, "an absent ladder must fall back, not price at strictest"


def test_a_ladder_that_will_not_parse_is_still_the_strictest(tmp_path) -> None:
    """Absent and broken are different faults.

    No file means nobody wrote one. A file that will not parse means somebody did, and it is
    wrong — pricing from the packaged table there would confirm against a ladder its operator is
    not looking at.
    """
    broken = tmp_path / "emergency_operations.json"
    broken.write_text("{ not json", encoding="utf-8")

    assert confirm.load_operation("kill-spid", path=broken)["confirmations"] == 2
