"""Every app (and the shared common layer) exposes a ``python -m db_ops.<app>.cli``
entrypoint. These tests pin the facade contracts: they dispatch to the real
implementations and never duplicate logic."""

import pytest


def test_common_cli_usage_and_delegation():
    from db_ops.common import cli

    # No args -> usage + exit 2 (not a crash).
    assert cli.main([]) == 2
    # Unknown/known admin subcommands delegate to config_admin's parser: -h exits 0.
    with pytest.raises(SystemExit) as exc:
        cli.main(["add-sql", "--help"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc:
        cli.main(["metric-toggle", "--help"])
    assert exc.value.code == 0


def test_common_cli_list_targets_prints(capsys):
    from db_ops.common import cli

    assert cli.main(["list-targets"]) == 0
    assert capsys.readouterr().out.strip()


def test_jobs_cli_dispatches_daemon_and_status(monkeypatch):
    from db_ops.jobs import cli, daemon, status

    calls = {}
    monkeypatch.setattr(daemon, "main", lambda argv: calls.update(daemon=argv) or 0)
    monkeypatch.setattr(status, "main", lambda argv: calls.update(status=argv) or 0)
    assert cli.main(["status", "--json"]) == 0
    assert calls["status"] == ["--json"]
    assert cli.main(["daemon", "--config", "x.json"]) == 0
    assert calls["daemon"] == ["--config", "x.json"]
    # bare args (no subcommand) default to the daemon
    assert cli.main(["--config", "y.json"]) == 0
    assert calls["daemon"] == ["--config", "y.json"]


def test_sql_tasks_cli_delegates_to_runner(monkeypatch):
    from db_ops.sql_tasks import cli, runner

    seen = {}
    monkeypatch.setattr(runner, "main", lambda argv: seen.update(argv=argv) or 0)
    assert cli.main(["--dry-run"]) == 0
    assert seen["argv"] == ["--dry-run"]
