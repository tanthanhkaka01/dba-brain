"""Giving a discovered group its job, without opening the file.

`save-updates` finds every group the bot belongs to and writes each one **inert** — no
`notify_level`, no `allow_command`. That is right: discovering a chat is not the same as deciding
what it is for, and a group that started routing criticals the moment somebody added the bot would
be worse than one that does nothing.

The step after it had no command at all. Standing a node up on 2026-09-10 meant hand-editing
`telegram_groups.json` eight times, once per group — in a file the toolkit itself rewrites whenever
`save-updates` runs. A hand-edit that races a writer is a hand-edit that gets lost.

The matching rule is the part worth testing hardest: a substring is accepted only when it names
exactly one group. "Errors" matching both "Errors" and "SQL Errors" and quietly taking the first is
how the wrong chat starts receiving the alerts, and nothing about the result would look wrong.
"""

from __future__ import annotations

import json

import pytest

from db_ops.telegram.updates import KNOWN_NOTIFY_LEVELS, set_group_level


def _groups(path, records):
    path.write_text(json.dumps({"telegram_groups": records}), encoding="utf-8")
    return path


def _record(group_id, title, **extra):
    base = {"group_id": group_id, "title": title, "group_type": "group",
            "notify_level": "", "allow_command": 0, "status": "active"}
    base.update(extra)
    return base


def test_a_discovered_group_gets_its_level(tmp_path):
    path = _groups(tmp_path / "g.json", [_record("-100", "DBABRAIN - Logs")])
    answer = set_group_level(group="-100", level="logging", allow_command=10, groups_path=path)

    assert answer["after"] == {"notify_level": "logging", "allow_command": 10}
    assert answer["before"] == {"notify_level": "", "allow_command": 0}
    written = json.loads(path.read_text(encoding="utf-8"))["telegram_groups"][0]
    assert written["notify_level"] == "logging"
    assert written["allow_command"] == 10


def test_a_group_can_be_named_by_its_title(tmp_path):
    path = _groups(tmp_path / "g.json", [_record("-100", "DBABRAIN - Criticals")])
    answer = set_group_level(group="DBABRAIN - Criticals", level="critical", groups_path=path)
    assert answer["group_id"] == "-100"


def test_the_title_match_ignores_case_because_nobody_retypes_it_exactly(tmp_path):
    path = _groups(tmp_path / "g.json", [_record("-100", "DBABRAIN - SLA")])
    assert set_group_level(group="dbabrain - sla", level="sla", groups_path=path)["group_id"] == "-100"


def test_a_substring_works_when_it_names_exactly_one_group(tmp_path):
    path = _groups(tmp_path / "g.json", [
        _record("-100", "DBABRAIN - Logs"), _record("-200", "DBABRAIN - Backup")])
    assert set_group_level(group="Backup", level="backup", groups_path=path)["group_id"] == "-200"


def test_an_ambiguous_substring_is_refused_rather_than_resolved_to_the_first(tmp_path):
    # The failure this rule exists for: the wrong chat receives the alerts and the result of the
    # command looks exactly like success.
    path = _groups(tmp_path / "g.json", [
        _record("-100", "DBABRAIN - Errors"), _record("-200", "DBABRAIN - SQL Errors")])
    with pytest.raises(RuntimeError) as excinfo:
        set_group_level(group="Errors", level="error", groups_path=path)
    assert "matches 2 groups" in str(excinfo.value)
    # Nothing was written on the way to refusing.
    assert all(item["notify_level"] == ""
               for item in json.loads(path.read_text(encoding="utf-8"))["telegram_groups"])


def test_an_exact_title_wins_over_a_substring_of_a_longer_one(tmp_path):
    path = _groups(tmp_path / "g.json", [
        _record("-100", "DBABRAIN - Errors"), _record("-200", "DBABRAIN - SQL Errors")])
    answer = set_group_level(group="DBABRAIN - Errors", level="error", groups_path=path)
    assert answer["group_id"] == "-100"


def test_a_name_that_matches_nothing_lists_what_there_is(tmp_path):
    path = _groups(tmp_path / "g.json", [_record("-100", "DBABRAIN - Logs")])
    with pytest.raises(RuntimeError) as excinfo:
        set_group_level(group="Warnings", level="warning", groups_path=path)
    assert "DBABRAIN - Logs" in str(excinfo.value)


def test_an_empty_file_says_to_discover_the_groups_first(tmp_path):
    path = _groups(tmp_path / "g.json", [])
    with pytest.raises(RuntimeError) as excinfo:
        set_group_level(group="anything", level="logging", groups_path=path)
    assert "save-updates" in str(excinfo.value)


def test_allow_command_is_left_alone_unless_it_is_given(tmp_path):
    # Two different decisions: what a group is for, and who may drive the bot from it.
    path = _groups(tmp_path / "g.json", [_record("-100", "G", allow_command=100)])
    answer = set_group_level(group="-100", level="logging", groups_path=path)
    assert answer["after"]["allow_command"] == 100


def test_a_level_outside_the_known_set_is_flagged_but_still_written(tmp_path):
    # `notify_level` is a free string the config and the apps agree on, so a new one is legal —
    # and a typo looks identical to a new one, which is why the answer says which it might be.
    path = _groups(tmp_path / "g.json", [_record("-100", "G")])
    answer = set_group_level(group="-100", level="critcal", groups_path=path)
    assert answer["unknown_level"] is True
    assert answer["after"]["notify_level"] == "critcal"
    assert "critical" in answer["known_levels"]


def test_a_known_level_is_not_flagged(tmp_path):
    path = _groups(tmp_path / "g.json", [_record("-100", "G")])
    assert set_group_level(group="-100", level="critical", groups_path=path)["unknown_level"] is False


def test_every_level_the_shipped_catalogue_routes_on_is_known():
    for level in ("logging", "warning", "error", "critical"):
        assert level in KNOWN_NOTIFY_LEVELS


def test_the_command_is_registered_and_reaches_the_function():
    from db_ops.telegram import cli

    args = cli.parse_args(["group-level", "--group", "G", "--level", "logging"])
    assert args.command == "group-level"
    assert args.telegram_function is set_group_level
