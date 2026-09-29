"""``/spbot_self_status`` says what this node schedules, not only what machine it is.

Asked for on 2026-09-23. Two schedulers were running disjoint sets of the estate's work on two
schemas - the container worker and a PC soak node - and the one answer an operator can get from a
phone described the machine: version, memory, disk, links. Which app commands this node would run,
and whether they had run, took a shell.

What these tests pin:

* the list comes from ``app_commands.json`` through the same role rule the daemon uses, so the
  report cannot disagree with the scheduler about what runs here;
* inactive commands and commands for another role are **listed**, because the list says what
  exists - hiding them is how a node running nothing looked the same as one running everything;
* the last-run column is the one part allowed to fail, and it fails into a named reason, never into
  no report - this is still the status asked for first when the store is what is down;
* the role rule itself lives in one place (``db_ops.lib.node_role``); the daemon and the Telegram
  processor had two copies that already disagreed about an empty role.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from db_ops.common import self_status
from db_ops.db import ops_status
from db_ops.lib import node_role
from db_ops.lib.time_window import weekdays_text

NOW = datetime(2026, 9, 23, 7, 0, 0, tzinfo=timezone.utc)


def _command(code, *, role="worker", active=True, interval=60, **extra):
    record = {"app_command_id": code, "app_code": code, "node_role": role, "active": active,
              "time_window": {"from_hour": 0, "to_hour": 23, "repeat_interval": interval}}
    record.update(extra)
    return record


# --------------------------------------------------------------------------- #
# The role rule, once
# --------------------------------------------------------------------------- #
def test_all_and_its_legacy_spellings_run_on_every_node():
    for spelling in ("all", "both", "any", "ALL"):
        assert node_role.runs_on(spelling, "worker")
        assert node_role.runs_on(spelling, "master")


def test_a_named_role_runs_only_on_that_role():
    assert node_role.runs_on("worker", "worker")
    assert not node_role.runs_on("worker", "master")


def test_a_node_with_no_role_is_the_master():
    assert node_role.runs_on("master", "")
    assert not node_role.runs_on("worker", None)


def test_an_empty_command_role_means_what_the_caller_says_it_means():
    """The daemon reads an empty role as everywhere; the Telegram processor keeps it on the worker.
    Both were right for their records, and the difference is now an argument instead of a copy."""
    assert node_role.runs_on("", "master", default="all")
    assert not node_role.runs_on("", "master", default="worker")


# --------------------------------------------------------------------------- #
# The section
# --------------------------------------------------------------------------- #
def test_every_command_is_listed_including_the_ones_that_do_not_run_here():
    apps = self_status.summarize_apps(
        [_command("APP-A"), _command("APP-B", role="master"), _command("APP-C", active=False)],
        node_role="worker", last_runs={}, now=NOW)

    assert [item["app"] for item in apps["items"]] == ["APP-A", "APP-B", "APP-C"]
    assert apps["configured"] == 3
    assert apps["scheduled_here"] == 1

    lines = self_status._app_lines(apps)
    assert any("APP-B" in line and "master only" in line for line in lines)
    assert any("APP-C" in line and " off " in line for line in lines)


def test_the_role_collect_reports_by_default_is_read_as_a_role():
    """collect() spells an unset role ``master (default)``; the rule has to see ``master``."""
    apps = self_status.summarize_apps([_command("APP-M", role="master")],
                                      node_role="master (default)", last_runs={}, now=NOW)
    assert apps["items"][0]["runs_here"] is True


def test_the_line_names_how_it_runs_when_and_on_which_days():
    commands = [_command("APP-ASYNC", run_mode="async", max_parallel=4, interval=30),
                _command("APP-WEEKLY", interval=72000,
                         time_window={"from_hour": 1, "to_hour": 5, "repeat_interval": 72000,
                                      "weekdays": [7]}),
                _command("APP-SERVICE", interval=0)]
    lines = self_status._app_lines(
        self_status.summarize_apps(commands, node_role="worker", last_runs={}, now=NOW))
    text = "\n".join(lines)

    assert "async x4" in text and "every 30s" in text
    assert "every 20h 01-05h on Sun" in text
    assert "service 00-23h" in text


def test_a_never_day_is_printed_as_one_because_it_is_a_configuration():
    assert weekdays_text([]) == "no day"
    assert weekdays_text([6, 1]) == "Mon,Sat"


def test_the_last_run_is_shown_with_its_age():
    apps = self_status.summarize_apps(
        [_command("APP-A")], node_role="worker",
        last_runs={"APP-A": {"status": "DONE", "started_at": "2026-09-23T06:59:30Z"}}, now=NOW)
    assert apps["items"][0]["last_status"] == "done"
    assert any("last done 30s ago" in line for line in self_status._app_lines(apps))


def test_a_scheduled_command_with_no_run_in_the_window_says_so():
    """Absent from job_runs is a finding for a command that should be running here - it is the
    shape of the 2026-08-12 outage, where a scan that exited every minute wrote nothing."""
    lines = self_status._app_lines(self_status.summarize_apps(
        [_command("APP-A")], node_role="worker", last_runs={}, now=NOW))
    assert any("APP-A" in line and "no run in 24 h" in line for line in lines)


def test_a_store_that_cannot_be_read_costs_the_column_and_says_why():
    apps = self_status.summarize_apps([_command("APP-A")], node_role="worker", last_runs=None,
                                      store_error="store not read: connection refused", now=NOW)
    lines = self_status._app_lines(apps)

    assert any(line.lstrip().startswith("APP-A") for line in lines), "the list survives"
    assert not any("no run in 24 h" in line for line in lines), (
        "an unread store must not be reported as a command that never ran")
    assert lines[-1].strip() == "(last run unknown: store not read: connection refused)"


def test_no_app_commands_file_is_not_configured_rather_than_an_error():
    lines = self_status._app_lines(self_status.summarize_apps(None, node_role="worker",
                                                              last_runs=None))
    assert lines == ["", "apps      : not configured (no data/app_commands.json)"]


def test_an_invalid_run_mode_is_named_on_its_line_instead_of_breaking_the_report():
    apps = self_status.summarize_apps([_command("APP-A", run_mode="sometimes")],
                                      node_role="worker", last_runs={}, now=NOW)
    assert apps["items"][0]["run_mode"].startswith("invalid")


def test_the_section_stays_inside_a_telegram_message():
    """The body is capped at 4096. A long note must never reach it, and nine commands must fit."""
    commands = [_command(f"APP-{n:02d}", note="x" * 3000) for n in range(12)]
    text = "\n".join(self_status._app_lines(
        self_status.summarize_apps(commands, node_role="worker", last_runs={}, now=NOW)))
    assert "xxxx" not in text
    assert len(text) < 1500


# --------------------------------------------------------------------------- #
# The store read: one query for every code
# --------------------------------------------------------------------------- #
class _SqliteStore:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def test_latest_runs_returns_each_codes_newest_run_in_one_query():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE job_runs (job_code TEXT, status TEXT, started_at TEXT)")
    conn.executemany("INSERT INTO job_runs VALUES (?, ?, ?)", [
        ("APP-A", "done", "2026-09-23T06:00:00Z"),
        ("APP-A", "error", "2026-09-23T06:30:00Z"),
        ("APP-B", "done", "2026-09-21T06:00:00Z"),   # outside the window
        ("APP-C", "done", "2026-09-23T06:10:00Z"),   # not asked for
    ])

    runs = ops_status.latest_runs(_SqliteStore(conn), ["APP-A", "APP-B"],
                                  since="2026-09-22T07:00:00Z")

    assert runs == {"APP-A": {"status": "error", "started_at": "2026-09-23T06:30:00Z"}}


def test_latest_runs_with_no_codes_does_not_touch_the_store():
    class _Refuses:
        def connect(self):
            raise AssertionError("no query should be made")

    assert ops_status.latest_runs(_Refuses(), [], since="2026-09-22T07:00:00Z") == {}


# --------------------------------------------------------------------------- #
# One command, and who states the last runs (0.24.0, rules R43)
# --------------------------------------------------------------------------- #
def test_the_telegram_command_calls_common_and_the_bot_finishes_it():
    """`db.cli self-status` was a second door to this report, there only because `common` may not
    open the store (R04). It went (the operator, 2026-09-26): the bot runs `common.cli self-status`
    and states the last-run column itself - so every copy of the command must point there, and the
    bot must know to finish it, or the reply comes back without the column and nobody notices."""
    import json

    from db_ops.telegram import command_processor

    root = Path(__file__).resolve().parents[1]
    copies = [root / "db_ops" / "telegram" / "catalogue" / "telegram_support_commands.json",
              root / "data" / "telegram_support_commands.json",
              root / "data" / "telegram_support_commands.example.json"]
    for path in (p for p in copies if p.is_file()):
        rows = json.loads(path.read_text(encoding="utf-8-sig"))["telegram_support_commands"]
        command = next(r for r in rows if r["command_text"] == "spbot_self_status")
        argv = command["action_config"]["command_argv"]
        assert argv[2:4] == ["db_ops.common.cli", "self-status"], f"{path.name}: {argv}"
        # Refusing without a key would cost the whole reply; the key only buys one column.
        assert not command["action_config"].get("requires_secret_key"), path.name
    assert command_processor.finishes("self-status")


class _Store:
    pass


def test_the_bot_states_each_apps_newest_run_from_its_store(monkeypatch, tmp_path):
    import json

    from db_ops.telegram import command_processor

    (tmp_path / "app_commands.json").write_text(json.dumps({"app_commands": [
        _command("APP-A"), _command("APP-B")]}), encoding="utf-8")
    seen = {}

    def latest(store, codes, *, since):
        seen.update(store=store, codes=codes)
        return {"APP-A": {"status": "done", "started_at": "2026-09-23T06:59:30Z"}}

    monkeypatch.setattr(ops_status, "latest_runs", latest)
    store = _Store()

    finished = command_processor.with_last_runs({"format": "txt"}, store=store, data_dir=tmp_path)

    assert finished["last_runs"] == {"APP-A": {"status": "done", "started_at": "2026-09-23T06:59:30Z"}}
    assert seen == {"store": store, "codes": ["APP-A", "APP-B"]}
    assert finished["format"] == "txt"


def test_a_store_the_bot_cannot_read_costs_the_column_never_the_reply(monkeypatch, tmp_path):
    from db_ops.telegram import command_processor

    def down(*_args, **_kwargs):
        raise ConnectionRefusedError("could not connect to server\nsecond line")

    monkeypatch.setattr(ops_status, "latest_runs", down)

    finished = command_processor.with_last_runs({}, store=_Store(), data_dir=tmp_path)

    assert "last_runs" not in finished
    assert finished["store_error"] == "store not read: could not connect to server"


def _no_config(monkeypatch):
    import db_ops.lib.config as db_ops_config
    from db_ops.common import cli as common_cli
    from db_ops.lib import config as lib_config

    # The load has to FAIL, not be skipped: loading this tree's config.json binds the process-wide
    # display timezone, and the first version of this test left it bound for every test after it -
    # two metric-window tests then read their naive hours on +07 and failed in a full run only.
    def refuse(*_a, **_kw):
        raise FileNotFoundError("no config on this machine")

    monkeypatch.setattr(db_ops_config, "load_config", refuse)
    monkeypatch.setattr(lib_config, "load_config", refuse)
    monkeypatch.setattr(common_cli, "read_app_commands",
                        lambda data_dir=None: [_command("APP-A")])
    return common_cli


def test_common_answers_with_no_config_and_says_the_column_was_not_stated(monkeypatch, capsys):
    common_cli = _no_config(monkeypatch)

    assert common_cli.main(["self-status", '{"format": "txt"}']) == 0
    out = capsys.readouterr().out
    assert "APP-A" in out
    assert "not stated" in out, "an unstated column must not read as never ran"


def test_common_reports_the_last_runs_it_is_given(monkeypatch, capsys):
    import json

    common_cli = _no_config(monkeypatch)
    moment = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    request = {"format": "txt", "last_runs": {"APP-A": {"status": "done", "started_at": moment}}}

    assert common_cli.main(["self-status", json.dumps(request)]) == 0
    out = capsys.readouterr().out
    assert "last done" in out and "not stated" not in out
