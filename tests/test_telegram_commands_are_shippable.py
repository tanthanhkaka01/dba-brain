"""The Telegram command set describes operations, not this estate's machines.

A command that writes a server id, a database and a credential into its own configuration is not
a command — it is one estate's script wearing a command's clothes. Two of them existed
(`/spbot_update_allow_re_inspect`, `/spbot_update_package_barcode`): each ran one SQL file against
one company's database on one server, and between them they were the reason the command catalogue
could not ship with dba-brain.

They are SQL tasks now, where an estate belongs — `sql_targets.json` — reached through the generic
`/spbot_run_sql_task <id> <barcode>`. A third, `/spbot_trace_session`, had its server and database
written into its argv while taking only a SPID; it takes all three now, like `/spbot_shrink_log`
already did.

These tests hold the line in both directions: the packaged catalogue carries every command the
estate has except a named few, and nothing in it names a real machine.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from db_ops.lib.paths import DEFAULT_DATA_DIR, PACKAGE_DIR

PACKAGED = PACKAGE_DIR / "telegram" / "catalogue" / "telegram_support_commands.json"
ESTATE = DEFAULT_DATA_DIR / "telegram_support_commands.json"

#: Commands that stay behind, with the reason. It is a list of *decisions*, and it only shrinks:
#: anything else the estate adds is expected to be generic enough to ship.
ESTATE_ONLY: dict[str, str] = {
    "spbot_master_cli": "a cheat sheet of this operator's own hosts, paths and deploy commands - "
                        "estate documentation rather than a command the bot runs",
}

#: What a command must never carry in its own configuration, because then it is bound to one
#: machine. `parameters` is exempt: a prompt saying "e.g. ..." is help text, not a target.
TARGET_KEYS = ("server_id", "database_name", "credential_name", "instance_name", "service_name")


def _commands(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    document = json.loads(path.read_bytes().decode("utf-8-sig"))
    return document.get("telegram_support_commands") or document.get("commands") or []


def _named(path: Path) -> dict[str, dict]:
    return {c["command_text"]: c for c in _commands(path)}


# -- no command is bound to a machine ---------------------------------------------------------- #

@pytest.mark.parametrize("path", [PACKAGED, ESTATE], ids=["packaged", "estate"])
def test_no_command_writes_a_target_into_its_own_configuration(path: Path) -> None:
    """The defect this whole change is about, stated once.

    `sql_execute` commands carried `server_id`, `database_name` and `credential_name` in
    `action_config`. That is a SQL task's job: an estate belongs in `sql_targets.json`, where one
    file answers "what does this tool touch" and one edit re-points it.
    """
    bound = []
    for command in _commands(path):
        config = command.get("action_config") or {}
        present = [key for key in TARGET_KEYS if str(config.get(key) or "").strip()]
        if present:
            bound.append(f"{command['command_text']} carries {present}")

    assert not bound, (
        f"{path.name}: these commands name a target instead of taking one: {bound}. Register a "
        "SQL task and reach it through /spbot_run_sql_task, or make the target an argument.")


def test_trace_session_takes_the_server_it_reports() -> None:
    """It said one machine's name while its argv pointed at another's.

    Its `start_text` named one instance and its argv had the same pair hardcoded, so the command
    could only ever trace that one machine — and once the argv took arguments, a fixed start
    message would have been worse than none: it would report a server other than the one being
    traced.
    """
    config = _named(PACKAGED)["spbot_trace_session"]["action_config"]
    argv = json.dumps(config["command_argv"])
    names = [p["name"] for p in config["parameters"]]

    assert "{server_id}" in argv and "{database}" in argv
    assert names[:2] == ["server_id", "database"]
    assert "{server_id}" in config["start_text"] and "{database}" in config["start_text"]


# -- the shipped set is the estate's set --------------------------------------------------------- #

def test_every_estate_command_ships_unless_it_is_a_named_exception() -> None:
    """A command that works here and is absent there is one a dba-brain install cannot run.

    Six were in that state — job control, restart, shrink-log, trace-session, list-metrics — all
    of them operations rather than machines, kept back for no recorded reason.
    """
    missing = sorted(set(_named(ESTATE)) - set(_named(PACKAGED)) - set(ESTATE_ONLY))

    assert not missing, (
        f"commands the estate has and the package does not: {missing}. Promote them into "
        "db_ops/telegram/catalogue/, or add them to ESTATE_ONLY with the reason.")


def test_the_exceptions_are_still_real() -> None:
    """An exception for a command that no longer exists is a note nobody will ever remove."""
    estate = _named(ESTATE)

    stale = sorted(name for name in ESTATE_ONLY if estate and name not in estate)
    assert not stale, f"ESTATE_ONLY names commands that are gone: {stale}"


def test_the_two_estate_scripts_are_no_longer_commands() -> None:
    """They are `/spbot_run_sql_task 25 <InternalBarcode>` and `26 <PackageBarcode>` now.

    Pinned by name because the tempting fix, when somebody wants one of these back, is to add the
    command again rather than the SQL task — and it would ship the next time nobody looked.
    """
    for name in ("spbot_update_allow_re_inspect", "spbot_update_package_barcode"):
        assert name not in _named(PACKAGED)
        assert name not in _named(ESTATE)


def test_a_command_in_both_sets_does_the_same_thing() -> None:
    """`action_type` and the argv are what a command *is*; anything else may be local.

    Compared as they are, with no allowance for the estate's own names: an argv that needed one
    would be an argv naming a machine, which the first test in this file already forbids. Prompt
    text is where the two legitimately differ, and prompt text is not compared.

    Deliberately **not** `command_type`: that is a permission tier, and this estate runs
    `/spbot_report_hourly_metrics` at 2 where the shipped default is 10. A stricter default for
    somebody else's install is right, and a test that forced them equal would be forcing a policy
    decision to travel.

    The argv is the field that caught what this test was written for. `/spbot_kill_spid` shipped
    sending `"session_id"` while `common.cli kill-spid` takes `"spid"` — so the command was broken
    on every dba-brain install and worked here, because only the estate's copy had been corrected.
    """
    packaged, estate = _named(PACKAGED), _named(ESTATE)

    disagreements = []
    for name in sorted(set(packaged) & set(estate)):
        for field in ("action_type", "is_group", "is_private"):
            if packaged[name].get(field) != estate[name].get(field):
                disagreements.append(f"{name}.{field}")
        here = (estate[name].get("action_config") or {}).get("command_argv")
        there = (packaged[name].get("action_config") or {}).get("command_argv")
        if here and here != there:
            disagreements.append(f"{name}.command_argv")
    assert not disagreements, (
        f"the shipped catalogue disagrees with this estate's about what these commands do: "
        f"{disagreements}. The packaged copy is the one nobody edits, so it is the one that "
        "falls behind.")


#: Flags whose value identifies one estate's record rather than describing an operation. A command
#: that pins one is an alias for something in *this* `data/`, and it ships to installs that have a
#: different one.
PINNING_FLAGS = ("--sql-id", "--server-id", "--backup-id", "--restore-id")


@pytest.mark.parametrize("path", [PACKAGED, ESTATE], ids=["packaged", "estate"])
def test_no_command_pins_a_record_id_in_its_argv(path: Path) -> None:
    """Estate-specificity hides behind a number, and that is why the first sweep missed it.

    `/spbot_json_exp_ticket_detail` ran `run-sql-id --sql-id 14 --force`, with 14 written into the
    argv and again into its `result_file` block. A scan for server names and addresses found
    nothing in it — there was nothing to find — and it shipped to every install as a command that
    ran *this* estate's task 14, whatever that install's task 14 happened to be.

    A literal after one of these flags is a record in somebody's `data/`. A placeholder is the
    command asking which one.
    """
    pinned = []
    for command in _commands(path):
        argv = (command.get("action_config") or {}).get("command_argv") or []
        for flag, value in zip(argv, argv[1:]):
            if flag in PINNING_FLAGS and not str(value).startswith("{"):
                pinned.append(f"{command['command_text']}: {flag} {value}")

    assert not pinned, (
        f"{path.name}: these commands name one estate's record instead of asking for it: "
        f"{pinned}. Take it as an argument, or drop the command and use the generic one.")


def test_the_ticket_export_is_a_task_output_not_a_command() -> None:
    """What replaced it, pinned so the replacement cannot quietly rot.

    The command existed because a scheduled task could not ask for a JSON file: the renderer had
    produced `json` all along, but `OUTPUT_FORMATS` did not list it, so `run-sql --format json`
    worked and a task's `output.format` could not say the same word. Two vocabularies for one
    question, and the workaround was a bespoke command with an id in it.
    """
    from db_ops.lib.result_format import RESULT_FORMATS
    from db_ops.lib.task_output import FILE_OUTPUT_FORMATS, OUTPUT_FORMATS

    assert "json" in OUTPUT_FORMATS and "json" in FILE_OUTPUT_FORMATS
    assert not [f for f in FILE_OUTPUT_FORMATS if f not in RESULT_FORMATS], (
        "a task may only ask for a file format the renderer can actually produce")
    assert "spbot_json_exp_ticket_detail" not in _named(PACKAGED)
    assert "spbot_json_exp_ticket_detail" not in _named(ESTATE)


def test_the_shipped_example_is_the_packaged_catalogue() -> None:
    """A third hand-kept copy of the same list, and it had already drifted.

    `data/telegram_support_commands.example.json` ships in place of the estate's file on a public
    checkout, while `db_ops/telegram/catalogue/` is what `db-ops init` writes. Both are "the
    command set a stranger gets", so a difference between them is one of them being wrong — and on
    2026-08-27 the example was five commands behind and still carried one that had been removed
    that morning.

    Derived from the packaged copy now rather than maintained beside it. This test is what keeps
    that true, because nothing else would notice.
    """
    example = ESTATE.parent / "telegram_support_commands.example.json"
    if not example.is_file():
        pytest.skip("no shipped example in this tree")

    assert _commands(example) == _commands(PACKAGED)


@pytest.mark.parametrize("path", [PACKAGED, ESTATE], ids=["packaged", "estate"])
def test_command_ids_are_unique_within_a_catalogue(path: Path) -> None:
    """`command_id` keys the record, so two commands sharing one is a config that cannot sync.

    Promoting six commands on 2026-08-27 brought their estate ids with them, and one landed on an
    id the shipped catalogue was already using. Three tests failed — uniqueness, catalog keying and
    the split/rebuild round trip — and none of them privately: `data/*.json` does not ship, so only
    the exported tree reads the example that carries the same list.

    The two catalogues number independently on purpose. The shipped one is compact, 1..n; this
    estate's has grown gaps over years. Neither is wrong, and forcing them equal would import one
    operator's history into the product. What is wrong is a repeat inside either.
    """
    counts = Counter(command["command_id"] for command in _commands(path))

    duplicated = {number: count for number, count in counts.items() if count > 1}
    assert not duplicated, f"{path.name}: command_id used more than once: {duplicated}"
