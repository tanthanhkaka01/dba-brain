"""Without a valid dbabrain key the scheduler starts no reports and no backups - and keeps the rest (1.0.0, K4).

The operator, 2026-11-21 (translated): *without a key dbabrain still does the metrics and the instant
alerts, but no reports, no backup / restore*. Two ways that goes wrong, and both are held here:

* too far: a node with no key that also stops collecting metrics or sending alerts has lost the free
  half of the tool - so a command that needs no key is never even asked about;
* too loud, or too quiet: a skipped command is logged each scan like a closed window, and said on
  Telegram ONCE (a state, not a failure); and a restore a person asked for still runs after the
  key's grace - a key's end must never strand a restore.

The hand-run door is the same rule: `reports` refuses with exit 3 and the sentence that says how to
get a key; `backup_restore` lets a restore by id through, said CRITICAL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dbabrain.jobs import key_gate
from dbabrain.lib import dbabrain_key as dk
from dbabrain.lib import secret_text

from tests.test_dbabrain_key import Signer

pytestmark = pytest.mark.real_key_gate

PASSPHRASE = "a-throwaway-passphrase-for-this-test-only"
NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
REPORTS = "python -m dbabrain.reports.cli --config config.json run-scheduled"
BACKUPS = "python -m dbabrain.backup_restore.cli workflow --config config.json"
METRICS = "python -m dbabrain.metrics.cli --config config.json collect"


@pytest.fixture(autouse=True)
def _fresh_process():
    key_gate.forget_what_was_said()
    yield
    key_gate.forget_what_was_said()


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, PASSPHRASE)
    data = tmp_path / "data"
    data.mkdir()
    return data


def _with_key(node, monkeypatch, *, issued=NOW, **token_fields):
    signer = Signer()
    monkeypatch.setattr(dk, "SIGNING_PUBLIC_KEYS", signer.keys)
    secret_text.set_secret_text(node, dk.KEY_REF, signer.token(issued=issued, **token_fields), key=PASSPHRASE)


def test_which_scheduled_command_needs_which_feature():
    assert dk.feature_of_command(REPORTS) == "reports"
    assert dk.feature_of_command(BACKUPS) == "backup_restore"
    assert dk.feature_of_command("python -m dbabrain.mcp.cli connect --wait-for-link") == "mcp"
    assert dk.feature_of_command(METRICS) is None
    assert dk.feature_of_command("python -m dbabrain.sla.cli validate") is None
    assert dk.feature_of_command("python -m dbabrain.telegram.cli run-workflow") is None
    assert dk.feature_of_command("python -m dbabrain.webhost.cli serve") is None


def test_with_no_key_metrics_start_and_reports_and_backups_do_not(node):
    gate = key_gate.KeyGate.for_scan(node, now=NOW)

    assert gate.decide(METRICS, requested=False) is None
    assert gate.decide(REPORTS, requested=False)[1].allowed is False
    assert gate.decide(BACKUPS, requested=False)[1].allowed is False


def test_with_a_valid_key_everything_starts(node, monkeypatch):
    _with_key(node, monkeypatch)
    gate = key_gate.KeyGate.for_scan(node, now=NOW)

    assert gate.decide(REPORTS, requested=False)[1].allowed
    assert gate.decide(BACKUPS, requested=False)[1].allowed


def test_after_the_grace_a_requested_restore_run_still_starts_and_a_requested_report_does_not(node, monkeypatch):
    _with_key(node, monkeypatch)
    gate = key_gate.KeyGate.for_scan(node, now=NOW + timedelta(days=18))

    assert gate.decide(BACKUPS, requested=False)[1].allowed is False
    assert gate.decide(BACKUPS, requested=True)[1].allowed is True
    assert gate.decide(REPORTS, requested=True)[1].allowed is False


def test_a_store_that_does_not_open_is_a_reason_not_a_crash(node, monkeypatch):
    secret_text.set_secret_text(node, "OTHER", "x", key="the-real-passphrase")

    gate = key_gate.KeyGate.for_scan(node, now=NOW)

    assert gate.standing.state == "invalid"
    assert gate.decide(METRICS, requested=False) is None


def test_a_reason_is_said_on_telegram_once_per_process(node):
    gate = key_gate.KeyGate.for_scan(node, now=NOW)
    said: list[tuple[str, str]] = []
    feature, decision = gate.decide(REPORTS, requested=False)

    for _scan in range(5):
        gate.say_once(feature, decision, lambda level, text: said.append((level, text)))

    assert len(said) == 1
    assert "dbabrain key-request" in said[0][1]


def test_a_warning_is_not_said_by_the_gate_the_watch_says_it_once(node, monkeypatch):
    """2.87.111's first hour: the gate repeated the watch's warning for every gated command - four at once."""
    _with_key(node, monkeypatch, issued=NOW - timedelta(days=8))
    gate = key_gate.KeyGate.for_scan(node, now=NOW)
    said: list = []

    for command in (REPORTS, BACKUPS, "python -m dbabrain.mcp.cli connect"):
        feature, decision = gate.decide(command, requested=False)
        assert decision.allowed and decision.level == "warning"
        gate.say_once(feature, decision, lambda level, text: said.append(text))

    assert said == []


def test_the_daemon_skips_a_gated_command_and_closes_a_request_for_it(node, monkeypatch):
    from dbabrain.jobs import daemon

    closed: list[tuple[int, str]] = []
    pushed: list[str] = []
    events: list[tuple[str, dict]] = []

    class _Requests:
        def mark_done(self, request_id, *, note=""):
            closed.append((request_id, note))

    monkeypatch.setattr(daemon, "_run_request_store", lambda _store: _Requests())
    monkeypatch.setattr(daemon, "_push_daemon_telegram", lambda **kwargs: pushed.append(kwargs["message"]))
    monkeypatch.setattr(daemon, "log_app_event", lambda _logger, name, **fields: events.append((name, fields)))
    monkeypatch.setattr(daemon, "app_command_metadata", lambda _command, **extra: dict(extra))
    command = daemon.AppCommand(
        app_command_id="APP-REPORTS-CREATE", app_code="APP-REPORTS-CREATE", app_name="reports",
        display_name="Reports", log_scope="reports", working_dir="tools/dbabrain", command_text=REPORTS,
        time_window=None, active=True)
    gate = key_gate.KeyGate.for_scan(node, now=NOW)

    started = daemon._key_allows(gate, command, store=object(), logger=None, requested={"request_id": 7})

    assert started is False
    assert closed and closed[0][0] == 7 and "dbabrain key" in closed[0][1]
    assert [name for name, _ in events] == ["app.daemon.command.skip_key"]
    assert len(pushed) == 1 and pushed[0].startswith("Reports: ")


def test_the_reports_door_on_a_node_with_no_key_says_how_and_exits_0(node, monkeypatch, capsys):
    """No key is a state, not a failure: a metrics-only install is a correct one."""
    from dbabrain.reports import cli as reports_cli

    monkeypatch.setenv("DBABRAIN_DATA_DIR", str(node))

    code = reports_cli.main(["--config", "config.json", "run-scheduled"])

    assert code == 0
    assert "dbabrain key-request" in capsys.readouterr().err


def test_the_reports_door_after_a_key_ended_exits_3(node, monkeypatch, capsys):
    from dbabrain.reports import cli as reports_cli

    _with_key(node, monkeypatch, issued=NOW - timedelta(days=30))
    monkeypatch.setenv("DBABRAIN_DATA_DIR", str(node))

    code = reports_cli.main(["--config", "config.json", "run-scheduled"])

    assert code == dk.EXIT_NEEDS_KEY
    assert "dbabrain key-renew" in capsys.readouterr().err


def test_a_restore_by_hand_passes_the_door_without_a_key_said_critical(node, monkeypatch, capsys):
    decision = dk.gate_here("backup_restore", new_work=False, data_dir=node, now=NOW)

    assert decision.allowed and decision.level == "critical"
    assert dk.said_at_the_door(decision, app="backup_restore") is None
    assert "CRITICAL" in capsys.readouterr().err


def test_the_backup_door_knows_new_work_from_existing():
    from dbabrain.backup_restore import cli as backup_cli

    assert {"workflow", "backup", "offload"} <= backup_cli.NEW_WORK_COMMANDS
    assert not {"restore-by-id", "restore-latest", "restore-workflow", "list-backups",
                "certificates"} & backup_cli.NEW_WORK_COMMANDS
