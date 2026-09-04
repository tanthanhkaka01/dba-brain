"""A forced SQL task run costs one typed `yes`, and the same one everywhere.

`--force` skips the time window, the repeat interval **and** the active flag, so
`/spbot_run_sql_task 24` re-ran a production payroll engine with nothing between the operator and
the write but a Telegram clearance. Clearance is the wrong control for that: `command_type`
answers *who may ask*, never *how hard it is to ask*, and raising it to 50 on 2026-09-04 made the
command as privileged as `/spbot_kill_spid` while it still cost nothing to run.

The confirmation therefore lives where every other one lives — `db_ops.common.confirm`, priced by
`data/emergency_operations.json` — and the app reaches it the way an app reaches `common`: over
the CLI (`common.cli authorize`), never by importing it. So this file has two halves: the gate's
own face, and what `sql_tasks` asks it.
"""

import json
import types

import pytest

from db_ops.common import confirm
from db_ops.sql_tasks import runner
from db_ops.sql_tasks.runner import authorize_forced_run, parse_args


@pytest.fixture(autouse=True)
def no_terminal(monkeypatch):
    """The worker's world, stated rather than inherited.

    `is_interactive` opens the controlling terminal to answer, and on Windows `CON` opens even
    under pytest — so without this the suite would sit at a confirmation prompt nobody can see.
    The test that is about the terminal says so by patching it back.
    """
    monkeypatch.setattr(confirm, "is_interactive", lambda: False)
    monkeypatch.setattr(confirm, "open_terminal_write", lambda: None)


# --------------------------------------------------------------------------- #
# The gate's own face: `common.cli authorize`
# --------------------------------------------------------------------------- #
REQUEST = {
    "operation": "run-sql-task",
    "target_id": "9",
    "target_label": "sql_id 9 recalculate today",
    "effects": ["runs on 1 target: ACME-192-0-2-115"],
}


def test_the_answer_may_arrive_in_the_request():
    """Telegram asks the prompt and passes the reply; there is no terminal on the worker."""
    report = confirm.authorize_request({**REQUEST, "confirm": "yes"})

    assert report["ok"] is True
    assert report["facts"]["authorization"]["by"] == "request"


def test_a_run_nobody_answered_is_refused():
    """Intent is not presence: `"confirm": true` says the payload meant it, not that a human saw it."""
    assert confirm.authorize_request({**REQUEST, "confirm": True})["ok"] is False


def test_a_wrong_answer_is_refused():
    """The whole word or nothing. `y` is what a hand types while reading something else."""
    assert confirm.authorize_request({**REQUEST, "confirm": "y"})["ok"] is False


def test_unattended_automation_has_to_say_so():
    """`assume_yes` is a declaration, and a greppable one — recorded as *no human was prompted*."""
    report = confirm.authorize_request({**REQUEST, "confirm": True, "assume_yes": True})

    assert report["ok"] is True and report["facts"]["authorization"]["by"] == "assume_yes"


def test_an_operation_nobody_registered_costs_more_not_less():
    """A gate added to a CLI and forgotten in the config must get harder, never easier.

    An unlisted operation is priced at level 100, so one `yes` is one answer of the two it owes.
    """
    report = confirm.authorize_request(
        {"operation": "run-sql-task-typo", "target_id": "9", "confirm": "yes"})

    assert report["ok"] is False


def test_the_operation_has_to_be_named():
    with pytest.raises(ValueError, match="operation"):
        confirm.authorize_request({"target_id": "9", "confirm": "yes"})


def test_a_terminal_run_is_asked_rather_than_waved_through(monkeypatch):
    """`--force` on a command line is intent; the prompt is the presence half."""
    asked = []
    monkeypatch.setattr(confirm, "is_interactive", lambda: True)
    monkeypatch.setattr(confirm, "read_answer",
                        lambda prompt, **kwargs: asked.append(prompt) or "yes")

    assert confirm.authorize_request({**REQUEST, "confirm": True})["ok"] is True
    assert asked, "a terminal run must be asked"


# --------------------------------------------------------------------------- #
# What `sql_tasks` asks it
# --------------------------------------------------------------------------- #
@pytest.fixture
def task(tmp_path):
    """One inactive task, one target — the case `--force` exists for."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    delivery = {
        "output": {"format": "plain", "telegram_chat": "sql", "chat_id": ""},
        "notify": {
            "logging_on_run": {"enabled": False, "telegram_chat": "sql"},
            "alert_on_error": {"enabled": False, "telegram_chat": "sql"},
        },
    }
    (data_dir / "sql_commands.json").write_text(json.dumps({"sql_commands": [{
        **delivery, "sql_id": 9, "sql_code": "PAYROLL", "sql_name": "recalculate today",
        "db_type": "sqlserver", "script_type": "single", "script_path": "sql/tasks/payroll.sql",
        "active": False,
    }]}), encoding="utf-8")
    (data_dir / "sql_targets.json").write_text(json.dumps({"sql_targets": [{
        **delivery, "sql_id": 9, "target_no": 1, "server_id": "ACME-192-0-2-115",
        "db_type": "sqlserver", "service_name": "svc", "instance_name": "inst",
        "credential_name": "cred",
        "time_window": {"from_day": 1, "to_day": 1, "from_hour": 0, "to_hour": 0,
                        "repeat_interval": 999999},
        "active": True,
    }]}), encoding="utf-8")
    return data_dir


@pytest.fixture
def gate(monkeypatch):
    """The `authorize` call, captured instead of spawned.

    The real one is a subprocess that may open a terminal and wait on it, which a test suite has
    no business doing. What belongs here is the *request*; the gate's own behaviour is pinned
    above, in process.
    """
    calls = []

    def fake(command, request, **kwargs):
        calls.append((command, request))
        return True, {"gates": []}, ""

    monkeypatch.setattr(runner.common_cli, "run_allowing_failure", fake)
    return calls


def test_the_operator_is_told_what_will_run_before_being_asked(task, gate):
    """"Run task 9?" tells nobody anything they can check.

    The facts are read with the loaders the run itself uses, so the prompt cannot describe a task
    other than the one that executes.
    """
    authorize_forced_run(sql_id=9, data_dir=task, answer="yes", channel="telegram")

    command, request = gate[0]
    assert command == "authorize" and request["operation"] == "run-sql-task"
    assert "recalculate today" in request["target_label"]
    effects = " | ".join(request["effects"])
    assert "ACME-192-0-2-115" in effects, "the prompt must name where the task will run"
    assert "active flag" in effects, "the prompt must say the run is forced"
    assert "INACTIVE" in effects, "an inactive task is the surprising case; say it"
    assert request["confirm"] == "yes"
    assert request["authorized_by"] == {"channel": "telegram"}


def test_the_flag_declares_intent_and_the_word_answers(task, gate):
    """With no `--confirm`, the request still says `confirm` — that is `--force` speaking.

    Intent alone authorizes nothing (the gate above proves it); it is what makes a terminal ask
    instead of refusing outright.
    """
    authorize_forced_run(sql_id=9, data_dir=task)
    assert gate[0][1]["confirm"] is True and "assume_yes" not in gate[0][1]

    authorize_forced_run(sql_id=9, data_dir=task, assume_yes=True)
    assert gate[1][1]["assume_yes"] is True


def test_a_gate_that_cannot_be_asked_is_a_refusal(task, monkeypatch):
    """A confirmation that fails open is not a confirmation."""
    def explode(command, request, **kwargs):
        raise runner.common_cli.CommonCliError("no interpreter")

    monkeypatch.setattr(runner.common_cli, "run_allowing_failure", explode)

    assert authorize_forced_run(sql_id=9, data_dir=task, answer="yes") is False


def test_an_unreadable_config_still_costs_an_answer(tmp_path, gate):
    """A gate that opens itself when it cannot read the target opens on the day the file is wrong."""
    empty = tmp_path / "data"
    empty.mkdir()

    authorize_forced_run(sql_id=9, data_dir=empty, answer="yes")

    assert gate[0][1]["operation"] == "run-sql-task"


def test_the_cli_takes_the_answer_and_the_waiver():
    args = parse_args(["--config", "c.json", "run-sql-id", "--sql-id", "9", "--force",
                       "--confirm", "yes"])

    assert args.confirm == "yes" and args.assume_yes is False
    assert parse_args(["--config", "c.json", "run-sql-id", "--sql-id", "9", "--force",
                       "--assume-yes"]).assume_yes is True


# --------------------------------------------------------------------------- #
# Where the gate sits in main(), which is the part that decides whether SQL runs
# --------------------------------------------------------------------------- #
def _main_without_its_world(monkeypatch, tmp_path, *, authorized):
    """`main` with everything but the decision replaced: no config, no store, no logger, no SQL."""
    ran = []
    asked = []

    monkeypatch.setattr(runner, "resolve_config_path", lambda app, path: "config.json")
    monkeypatch.setattr(runner, "load_config", lambda path: types.SimpleNamespace(log_dir=tmp_path))
    monkeypatch.setattr(runner, "patch_stdout", lambda *a, **k: None)
    monkeypatch.setattr(runner, "setup_app_logger", lambda *a, **k: None)
    monkeypatch.setattr(runner, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(runner, "telegram_groups", lambda: {})
    monkeypatch.setattr(runner.DbOpsStore, "from_config", classmethod(
        lambda cls, config: types.SimpleNamespace(initialize=lambda: None)))

    def fake_authorize(**kwargs):
        asked.append(kwargs)
        return authorized

    def fake_run(**kwargs):
        ran.append(kwargs)
        return types.SimpleNamespace(due_count=1, error_count=0)

    monkeypatch.setattr(runner, "authorize_forced_run", fake_authorize)
    monkeypatch.setattr(runner, "run_sql_id_tasks", fake_run)
    monkeypatch.setattr(runner, "run_scheduler_scan", fake_run)
    return ran, asked


def test_a_refused_run_executes_nothing(monkeypatch, tmp_path):
    """The point of the gate. A non-zero exit is what Telegram reports back as a failure."""
    ran, asked = _main_without_its_world(monkeypatch, tmp_path, authorized=False)

    exit_code = runner.main(["run-sql-id", "--sql-id", "9", "--force"])

    assert exit_code == 1
    assert asked and not ran, "nothing may execute after a refusal"


def test_a_confirmed_run_proceeds(monkeypatch, tmp_path):
    ran, asked = _main_without_its_world(monkeypatch, tmp_path, authorized=True)

    assert runner.main(["run-sql-id", "--sql-id", "9", "--force", "--confirm", "yes"]) == 0
    assert len(ran) == 1 and asked[0]["answer"] == "yes"


def test_a_rehearsal_is_not_asked_to_confirm(monkeypatch, tmp_path):
    """Confirming something that will not happen is how people learn to answer without reading."""
    ran, asked = _main_without_its_world(monkeypatch, tmp_path, authorized=False)

    assert runner.main(["--dry-run", "run-sql-id", "--sql-id", "9", "--force"]) == 0
    assert not asked and len(ran) == 1


def test_the_scheduled_scan_is_never_asked(monkeypatch, tmp_path):
    """A schedule was authorized when the operator wrote it; a daemon has nobody to ask at 03:00."""
    ran, asked = _main_without_its_world(monkeypatch, tmp_path, authorized=False)

    assert runner.main([]) == 0
    assert not asked and len(ran) == 1
