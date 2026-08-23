import json
from io import StringIO
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db_ops.lib.time_window import TimeWindow
import pytest

from conftest import shipped_config
from db_ops.jobs import daemon


class FakeTelegram:
    """The daemon reads none of this — routing comes from the shared router — but the config
    object it is handed still carries a telegram block."""

    def __init__(self, enabled=False):
        self.enabled = enabled
        self.level_chat_map = {"error": "grp-error", "critical": "grp-critical"}


class FakeStore:

    @classmethod
    def from_config(cls, config, **kwargs):
        """Store doubles must offer the same constructor contract as the real classes."""
        return cls(getattr(config, 'sqlite_path', None))
    def __init__(self, latest=None):
        self.latest = latest or {}
        self.inserted = []
        self.updated = []
        self.telegram_sent = []

    def fetch_latest_job_runs_by_job_code(self):
        return self.latest

    def fetch_running_job_runs(self, job_code_prefix: str = ""):
        """Every open row, which is what stale recovery reconciles.

        The fake keeps deriving these from `latest` so existing tests keep their setup, but the
        real store returns rows a job_code lookup can no longer reach — that is the whole point
        of the query, and the reason 177 rows had accumulated unclosed.
        """
        rows = []
        for job_code, row in self.latest.items():
            merged = dict(row)
            merged.setdefault("job_code", job_code)
            if str(merged.get("status") or "").strip().lower() == "running":
                rows.append(merged)
        return rows

    def insert_job_run(self, item):
        self.inserted.append(item)
        return len(self.inserted)

    def update_job_run(self, **kwargs):
        self.updated.append(kwargs)

    def insert_telegram_send_message(self, **kwargs):
        self.telegram_sent.append(kwargs)
        return len(self.telegram_sent)


class FakeConfig:
    def __init__(self, log_dir=None, telegram_enabled=False):
        self.log_dir = Path(log_dir or "tools/db_ops/tmp_test_logs")
        self.sqlite_path = Path("runtime/db_ops.sqlite")
        self.telegram = FakeTelegram(enabled=telegram_enabled)



    @property
    def store(self):
        """SQLite store declaration matching this fake's sqlite_path (see db_ops.config)."""
        from db_ops.config import SqliteStoreConfig, StoreConfig
        from pathlib import Path as _Path

        return StoreConfig(sqlite=SqliteStoreConfig(path=_Path(str(self.sqlite_path))))

class FakeProcess:
    next_pid = 1000

    def __init__(self, returncode=None):
        FakeProcess.next_pid += 1
        self.pid = FakeProcess.next_pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 124

    def kill(self):
        self.killed = True
        self.returncode = 124

    def wait(self, timeout=None):
        return self.returncode


def write_app_commands(data_dir, commands):
    data_dir.mkdir()
    (data_dir / "app_commands.json").write_text(json.dumps({"app_commands": commands}), encoding="utf-8")


def app_command(app_command_id, *, repeat_interval=1, timeout=60, active=True, command_text=None):
    return {
        "app_command_id": app_command_id,
        "app_name": app_command_id.lower(),
        "display_name": app_command_id,
        "log_scope": app_command_id.lower().replace("app-", "").replace("-", "_"),
        "working_dir": ".",
        "command_text": command_text or "python -c \"print('ok')\"",
        "time_window": {
            "from_year": None,
            "to_year": None,
            "from_month": None,
            "to_month": None,
            "from_day": 1,
            "to_day": 31,
            "from_hour": 0,
            "to_hour": 23,
            "from_minute": None,
            "to_minute": None,
            "repeat_interval": repeat_interval,
            "timeout": timeout,
        },
        "active": active,
    }


def test_daemon_accepts_key_base64_argument():
    args = daemon.parse_args(
        [
            "--config",
            "config.json",
            "--delay-seconds",
            "10",
            "--key_base64",
            "SkZuc2gjJDcyM0hzM2g=",
        ]
    )
    assert args.key is None
    assert args.key_base64 == "SkZuc2gjJDcyM0hzM2g="


def test_daemon_default_scan_delay_is_two_seconds():
    assert daemon.parse_args([]).delay_seconds == 2


def test_repository_telegram_repeat_interval_is_one_second():
    commands = daemon.load_app_commands(shipped_config("app_commands.json"))
    assert commands["APP-TELEGRAM"].repeat_interval_seconds == 1


def _after_interpreter(command: str) -> str:
    """The spawned command with its leading interpreter dropped.

    These tests are about **key forwarding**, not about which Python runs. The daemon rewrites a
    bare `python` to `sys.executable`, so that a venv install schedules its own interpreter rather
    than whatever `PATH` resolves to — see `tests/test_daemon_runs_its_own_interpreter.py`.
    Asserting on the whole string made four of these fail for a reason none of them is about.
    """
    import shlex

    head = shlex.split(command, posix=daemon.os.name != "nt")[0]
    return command[command.index(head) + len(head):].lstrip()


def test_daemon_appends_key_base64_to_spawned_command(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(
        data_dir,
        [
            app_command(
                "APP-TELEGRAM",
                command_text="python -m db_ops.telegram.cli --config config.json run-workflow",
            )
        ],
    )
    started = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: started.append(args[0]) or FakeProcess(returncode=None))

    daemon.run_scheduler_scan(
        config=FakeConfig(tmp_path / "logs"),
        store=FakeStore(),
        data_dir=data_dir,
        logger=None,
        running_commands={},
        forwarded_key_args=daemon.ForwardedKeyArgs("--key-base64", "QUJDRA=="),
    )

    assert [_after_interpreter(c) for c in started] == [
        "-m db_ops.telegram.cli --config config.json --key-base64 QUJDRA== run-workflow"]


def test_daemon_sets_secret_key_env_for_spawned_restore_command(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(
        data_dir,
        [
            app_command(
                "APP-RESTORE-WORKFLOW",
                command_text="python -m db_ops.metrics.cli collect --config config.json",
            )
        ],
    )
    started = []

    def fake_popen(*args, **kwargs):
        started.append((args[0], kwargs["env"].get("DB_OPS_SECRET_KEY")))
        return FakeProcess(returncode=None)

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)

    daemon.run_scheduler_scan(
        config=FakeConfig(tmp_path / "logs"),
        store=FakeStore(),
        data_dir=data_dir,
        logger=None,
        running_commands={},
        forwarded_key_args=daemon.ForwardedKeyArgs("--key-base64", "c2VjcmV0LXBocmFzZQ=="),
    )

    assert _after_interpreter(started[0][0]).startswith("-m db_ops.metrics.cli --key-base64")
    assert started[0][1] == "secret-phrase"


def test_daemon_appends_plain_key_to_spawned_command(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(
        data_dir,
        [
            app_command(
                "APP-METRICS",
                command_text="python -m db_ops.metrics.cli --config config.json collect",
            )
        ],
    )
    started = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: started.append(args[0]) or FakeProcess(returncode=None))

    daemon.run_scheduler_scan(
        config=FakeConfig(tmp_path / "logs"),
        store=FakeStore(),
        data_dir=data_dir,
        logger=None,
        running_commands={},
        forwarded_key_args=daemon.ForwardedKeyArgs("--key", "plain key"),
    )

    tail = [_after_interpreter(c) for c in started]
    if daemon.os.name == "nt":
        assert tail == ['-m db_ops.metrics.cli --config config.json --key "plain key" collect']
    else:
        assert tail == ["-m db_ops.metrics.cli --config config.json --key 'plain key' collect"]


def test_daemon_does_not_append_key_when_not_supplied(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-METRICS")])
    started = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: started.append(args[0]) or FakeProcess(returncode=None))

    daemon.run_scheduler_scan(
        config=FakeConfig(tmp_path / "logs"),
        store=FakeStore(),
        data_dir=data_dir,
        logger=None,
        running_commands={},
    )

    assert [_after_interpreter(c) for c in started] == ['-c "print(\'ok\')"']


def test_app_daemon_loads_only_app_commands(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-METRICS")])
    loaded_paths = []
    started = []

    def fake_load_json_file(path):
        loaded_paths.append(Path(path).name)
        return json.loads(Path(path).read_text(encoding="utf-8"))

    monkeypatch.setattr(daemon, "load_json_file", fake_load_json_file)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: started.append(args) or FakeProcess(returncode=None))

    daemon.run_scheduler_scan(
        config=FakeConfig(tmp_path / "logs"),
        store=FakeStore(),
        data_dir=data_dir,
        logger=None,
        running_commands={},
    )

    assert loaded_paths == ["app_commands.json"]
    assert len(started) == 1


def test_due_app_commands_start_independently(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-METRICS"), app_command("APP-TELEGRAM")])
    started = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: started.append(kwargs) or FakeProcess(returncode=None))

    running = {}
    daemon.run_scheduler_scan(config=FakeConfig(tmp_path / "logs"), store=FakeStore(), data_dir=data_dir, logger=None, running_commands=running)

    assert set(running) == {"APP-METRICS", "APP-TELEGRAM"}
    assert len(started) == 2


def test_long_running_app_does_not_block_another_due_app(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-METRICS"), app_command("APP-TELEGRAM")])
    metrics = daemon.load_app_commands(data_dir / "app_commands.json")["APP-METRICS"]
    running = {
        "APP-METRICS": daemon.RunningAppCommand(
            app_command=metrics,
            process=FakeProcess(returncode=None),
            started_at=datetime.now(timezone.utc),
            log_id=1,
            working_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            stdout_file=StringIO(),
            stderr_file=StringIO(),
        )
    }
    started = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: started.append(args) or FakeProcess(returncode=None))

    daemon.run_scheduler_scan(config=FakeConfig(tmp_path / "logs"), store=FakeStore(), data_dir=data_dir, logger=None, running_commands=running)

    assert set(running) == {"APP-METRICS", "APP-TELEGRAM"}
    assert len(started) == 1


def test_duplicate_app_command_is_skipped_while_running(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-METRICS")])
    command = daemon.load_app_commands(data_dir / "app_commands.json")["APP-METRICS"]
    running = {
        "APP-METRICS": daemon.RunningAppCommand(
            app_command=command,
            process=FakeProcess(returncode=None),
            started_at=datetime.now(timezone.utc),
            log_id=1,
            working_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            stdout_file=StringIO(),
            stderr_file=StringIO(),
        )
    }
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not start")))

    daemon.run_scheduler_scan(config=FakeConfig(tmp_path / "logs"), store=FakeStore(), data_dir=data_dir, logger=None, running_commands=running)

    assert set(running) == {"APP-METRICS"}


def test_repeat_interval_is_measured_from_started_at(tmp_path):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-METRICS", repeat_interval=10)])
    command = daemon.load_app_commands(data_dir / "app_commands.json")["APP-METRICS"]
    started = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    latest = {
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:09Z",
        "created_at": "2026-01-01T00:00:00Z",
    }

    assert not daemon.app_command_is_due(command, latest, now=started + timedelta(seconds=9))
    assert daemon.app_command_is_due(command, latest, now=started + timedelta(seconds=10))


def test_failed_app_is_due_after_default_retry_interval(tmp_path):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-METRICS", repeat_interval=300)])
    command = daemon.load_app_commands(data_dir / "app_commands.json")["APP-METRICS"]
    started = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    latest = {
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:05Z",
        "created_at": "2026-01-01T00:00:00Z",
        "status": "error",
    }

    assert not daemon.app_command_is_due(command, latest, now=started + timedelta(seconds=59))
    assert daemon.app_command_is_due(command, latest, now=started + timedelta(seconds=60))


def test_timeout_seconds_terminates_long_running_app(tmp_path):
    command = daemon.AppCommand(
        app_command_id="APP-RESTORE-WORKFLOW",
        app_code="APP-RESTORE-WORKFLOW",
        app_name="restore",
        display_name="Restore",
        log_scope="restore_workflow",
        working_dir=".",
        command_text="restore",
        time_window=TimeWindow(from_day=1, to_day=31, from_hour=0, to_hour=23, repeat_interval=1, timeout=1),
        active=True,
    )
    process = FakeProcess(returncode=None)
    running = {
        command.app_command_id: daemon.RunningAppCommand(
            app_command=command,
            process=process,
            started_at=datetime.now(timezone.utc) - timedelta(seconds=2),
            log_id=7,
            working_dir=Path("."),
            logs_dir=tmp_path / "logs",
            stdout_file=StringIO("out"),
            stderr_file=StringIO("err"),
        )
    }
    store = FakeStore()

    daemon.collect_running_commands(store=store, logger=None, running_commands=running)

    assert running == {}
    assert process.terminated is True
    assert store.updated[0]["status"] == "timeout"


def test_app_daemon_log_includes_scope(monkeypatch):
    messages = []
    command = daemon.AppCommand(
        app_command_id="APP-METRICS",
        app_code="APP-METRICS",
        app_name="metrics",
        display_name="Metrics",
        log_scope="metrics",
        working_dir=".",
        command_text="metrics",
        time_window=TimeWindow(from_day=1, to_day=31, from_hour=0, to_hour=23, repeat_interval=1, timeout=60),
        active=True,
    )
    monkeypatch.setattr(daemon, "log_event", lambda logger, level, message: messages.append(message))

    daemon.log_app_event(object(), "app.daemon.command.start", app_command=command, pid=1234, status="running")

    assert messages == [
        "app.daemon.command.start|scope=metrics|app_command_id=APP-METRICS|app_name=metrics|display_name=Metrics|pid=1234|status=running"
    ]


def test_missing_log_scope_raises_clear_validation_error(tmp_path):
    data_dir = tmp_path / "data"
    command = app_command("APP-METRICS")
    command.pop("log_scope")
    write_app_commands(data_dir, [command])

    try:
        daemon.load_app_commands(data_dir / "app_commands.json")
    except RuntimeError as exc:
        assert "app_command_id=APP-METRICS missing required log_scope" in str(exc)
    else:
        raise AssertionError("missing log_scope should fail fast")


def test_runtime_log_records_command_execution_metadata(tmp_path):
    command = daemon.AppCommand(
        app_command_id="APP-METRICS",
        app_code="APP-METRICS",
        app_name="not_the_filename",
        display_name="Not The Filename",
        log_scope="sql_tasks",
        working_dir=".",
        command_text="python -m example",
        time_window=TimeWindow(from_day=1, to_day=31, from_hour=0, to_hour=23, repeat_interval=1, timeout=60),
        active=True,
    )

    daemon.write_command_runtime_event(
        command,
        "app.daemon.command.done",
        logs_dir=tmp_path,
        working_dir=tmp_path,
        exit_code=0,
        duration_seconds=1.25,
        stdout_summary="ok",
        stderr_summary="",
        status="done",
    )

    assert (tmp_path / "sql_tasks_runtime.log").exists()
    assert not (tmp_path / "not_the_filename_runtime.log").exists()
    assert not (tmp_path / "APP-METRICS_runtime.log").exists()
    assert not (tmp_path / "Not The Filename_runtime.log").exists()
    text = (tmp_path / "sql_tasks_runtime.log").read_text(encoding="utf-8")
    assert "command_text=python -m example" in text
    assert "working_dir=" in text
    assert "exit_code=0" in text
    assert "duration_seconds=1.25" in text
    assert "timeout_seconds=60" in text
    assert "stdout_summary=ok" in text


# ── Stale RUNNING detection tests ─────────────────────────────────────────────

def _make_app_command(app_command_id="APP-RESTORE-WORKFLOW", *, timeout=7200, retry_interval=600,
                      repeat_interval=None):
    return daemon.AppCommand(
        app_command_id=app_command_id,
        app_code=app_command_id,
        app_name="restore",
        display_name="Restore Workflow",
        log_scope="restore_workflow",
        working_dir=".",
        # A module this distribution ships. The behaviour under test - the daemon injecting
        # --key-base64 for a CLI that declares it, and raising exactly one alert for a stale
        # run - is not specific to backup_restore, and naming it made the test unable to run
        # wherever that component is not installed: the daemon cannot introspect a module
        # that is not there, so it correctly does something else and the test reads as a bug.
        command_text="python -m db_ops.metrics.cli collect",
        time_window=TimeWindow(
            from_day=1, to_day=31, from_hour=0, to_hour=23,
            repeat_interval=repeat_interval, retry_interval=retry_interval, timeout=timeout,
        ),
        active=True,
    )


def _make_row(status, started_seconds_ago=0, log_id=1):
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=started_seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "log_id": log_id,
        "status": status,
        "started_at": started_at,
        "finished_at": None,
        "created_at": started_at,
    }


def test_stale_running_is_retryable_after_timeout():
    cmd = _make_app_command(timeout=3600, retry_interval=600)
    # Started 4 hours ago, still shows "running" in DB (daemon was restarted).
    row = _make_row("running", started_seconds_ago=4 * 3600)
    assert daemon.app_command_is_due(cmd, row)


def test_running_within_timeout_is_not_retryable():
    cmd = _make_app_command(timeout=7200, retry_interval=600)
    # Started 30 minutes ago, within timeout.
    row = _make_row("running", started_seconds_ago=1800)
    assert not daemon.app_command_is_due(cmd, row)


def test_recover_stale_running_jobs_marks_timeout_and_logs(tmp_path):
    cmd = _make_app_command(timeout=3600)
    app_commands = {cmd.app_command_id: cmd}
    stale_row = _make_row("running", started_seconds_ago=5 * 3600, log_id=42)
    store = FakeStore(latest={cmd.app_command_id: stale_row})
    messages = []

    daemon.recover_stale_running_jobs(
        store=store,
        app_commands=app_commands,
        config=FakeConfig(),
        logger=None,
    )

    assert len(store.updated) == 1
    update = store.updated[0]
    assert update["log_id"] == 42
    assert update["status"] == "timeout"
    assert "stale" in update["error_text"].lower()


def test_recover_stale_running_jobs_skips_within_timeout():
    cmd = _make_app_command(timeout=7200)
    app_commands = {cmd.app_command_id: cmd}
    # Started only 1 hour ago — within timeout.
    row = _make_row("running", started_seconds_ago=3600, log_id=10)
    store = FakeStore(latest={cmd.app_command_id: row})

    daemon.recover_stale_running_jobs(
        store=store,
        app_commands=app_commands,
        config=FakeConfig(),
        logger=None,
    )

    assert store.updated == []


def test_recover_stale_running_jobs_sends_telegram_when_enabled(tmp_path, monkeypatch):
    # The daemon holds no Telegram settings: it asks the shared router and obeys the answer.
    from db_ops.lib import telegram_route as _route

    monkeypatch.setattr(_route, "telegram_route",
                        lambda level, **_: {"enabled": True, "alert": True, "chat_id": "grp-error"})
    cmd = _make_app_command(timeout=3600)
    app_commands = {cmd.app_command_id: cmd}
    stale_row = _make_row("running", started_seconds_ago=5 * 3600, log_id=7)
    store = FakeStore(latest={cmd.app_command_id: stale_row})

    daemon.recover_stale_running_jobs(
        store=store,
        app_commands=app_commands,
        config=FakeConfig(telegram_enabled=True),
        logger=None,
    )

    assert len(store.telegram_sent) == 1
    assert store.telegram_sent[0]["tlgchat_id"] == "grp-error"
    msg = store.telegram_sent[0]["message_text"]
    assert "Stale running workflow" in msg
    assert cmd.app_command_id in msg


def test_recover_stale_running_jobs_no_telegram_when_the_router_says_no(monkeypatch):
    """Telegram off, level muted, or no chat mapped all reach the daemon as one answer: "".
    It must not re-derive any of them from config — that is how it used to drift."""
    from db_ops.lib import telegram_route as _route

    monkeypatch.setattr(_route, "telegram_route",
                        lambda level, **_: {"enabled": True, "alert": False, "chat_id": ""})
    cmd = _make_app_command(timeout=3600)
    app_commands = {cmd.app_command_id: cmd}
    stale_row = _make_row("running", started_seconds_ago=5 * 3600, log_id=7)
    store = FakeStore(latest={cmd.app_command_id: stale_row})

    daemon.recover_stale_running_jobs(
        store=store,
        app_commands=app_commands,
        config=FakeConfig(telegram_enabled=False),
        logger=None,
    )

    assert store.telegram_sent == []


def test_app_command_is_due_stale_running_boundary():
    cmd = _make_app_command(timeout=3600, retry_interval=600)
    # Exactly at timeout boundary: NOT yet due.
    row_just_before = _make_row("running", started_seconds_ago=3599)
    assert not daemon.app_command_is_due(cmd, row_just_before)
    # One second past timeout: due.
    row_just_after = _make_row("running", started_seconds_ago=3601)
    assert daemon.app_command_is_due(cmd, row_just_after)


def test_log_command_not_due_includes_workflow_state_and_next_retry():
    cmd = _make_app_command(timeout=7200, retry_interval=600)
    row = _make_row("error", started_seconds_ago=300, log_id=5)
    messages = []

    import db_ops.jobs.daemon as d
    original = d.log_event
    d.log_event = lambda logger, level, message: messages.append(message)
    try:
        daemon._log_command_not_due(object(), cmd, row)
    finally:
        d.log_event = original

    assert messages
    assert "workflow_state=error" in messages[0]
    assert "next_retry_at=" in messages[0]
    assert "retry_interval_seconds=600" in messages[0]


def test_recover_stale_running_ignores_non_running_statuses():
    cmd = _make_app_command(timeout=100)
    app_commands = {cmd.app_command_id: cmd}
    for status in ("done", "error", "timeout", "success"):
        store = FakeStore(latest={cmd.app_command_id: _make_row(status, started_seconds_ago=999, log_id=1)})
        daemon.recover_stale_running_jobs(store=store, app_commands=app_commands, config=FakeConfig(), logger=None)
        assert store.updated == [], f"should not update status={status}"


def test_resolve_working_dir_maps_tools_db_ops_sentinel_to_tool_root(tmp_path):
    # "tools/db_ops" is the logical alias for the tool root and must resolve to
    # TOOL_ROOT regardless of the physical layout (flat dev checkout or the image's
    # /app/tools/db_ops). Regression: the daemon crash-looped after db_ops was moved
    # out of repo/tools/db_ops because this relied on REPO_ROOT being tools' parent.
    assert daemon.resolve_working_dir("tools/db_ops", data_dir=tmp_path) == daemon.TOOL_ROOT
    assert daemon.resolve_working_dir(r"tools\db_ops", data_dir=tmp_path) == daemon.TOOL_ROOT


def test_resolve_working_dir_passes_absolute_path_through(tmp_path):
    assert daemon.resolve_working_dir(str(tmp_path), data_dir=tmp_path) == tmp_path


def test_recovery_closes_stale_rows_a_job_code_lookup_can_no_longer_reach():
    """Reading only the newest row per job_code meant a stale run that had been overtaken could
    never be closed again — it is no longer the latest for its code, but it is still open. They
    accumulated: 177 job_runs and 104 metric_runs were still RUNNING on the worker, the oldest
    from 2026-05-18, which makes "is anything running now" unanswerable."""
    cmd = _make_app_command(timeout=600)
    overtaken = _make_row("running", started_seconds_ago=48 * 3600, log_id=1)
    newest = _make_row("running", started_seconds_ago=24 * 3600, log_id=2)

    class _Store(FakeStore):
        def fetch_running_job_runs(self, job_code_prefix: str = ""):
            return [dict(overtaken, job_code=cmd.app_command_id),
                    dict(newest, job_code=cmd.app_command_id)]

    store = _Store()
    daemon.recover_stale_running_jobs(
        store=store, app_commands={cmd.app_command_id: cmd}, config=FakeConfig(), logger=None)

    assert sorted(u["log_id"] for u in store.updated) == [1, 2]


def test_a_long_running_service_is_closed_quietly_and_still_restarts_at_once():
    """APP-WEBHOST is `timeout=0, retry_interval=0, repeat_interval=0`: a service that must come
    back up the moment it stops.

    Three things have to hold at once, and each has already been got wrong. Its row MUST be
    closed — recovery runs at startup, when the daemon owns no children, so an open row belongs
    to a life that ended; skipping it leaked one row per restart. It must NOT alert — a service
    ending with its daemon is not a crash, and the old `elapsed < 0` comparison reported it stale
    on every startup. And it must be closed as an ERROR status: `job_due` only restarts a
    run-once entry from one of those, so anything else reads as "finished, never repeat" and the
    web host would never come back.
    """
    # Exactly APP-WEBHOST's real time_window: run once, no retry gap, no timeout.
    service = _make_app_command("APP-WEBHOST", timeout=0, retry_interval=0, repeat_interval=0)
    stale = _make_row("running", started_seconds_ago=72 * 3600, log_id=9)

    class _Store(FakeStore):
        def fetch_running_job_runs(self, job_code_prefix: str = ""):
            return [dict(stale, job_code="APP-WEBHOST")]

    store = _Store()
    daemon.recover_stale_running_jobs(
        store=store, app_commands={"APP-WEBHOST": service}, config=FakeConfig(), logger=None)

    assert len(store.updated) == 1                      # closed: no row leaks per restart
    assert store.updated[0]["status"] == "timeout"      # ...in a state job_due will restart from
    assert store.telegram_sent == []                    # ...without crying wolf
    from db_ops.lib.time_window import ERROR_STATUSES
    assert store.updated[0]["status"] in ERROR_STATUSES
    assert daemon.app_command_is_due(service, dict(stale, status="timeout")) is True


def test_a_two_week_old_stale_row_is_reconciled_without_an_alert():
    """The spam had a root cause, and it was not "too many messages": it was alerting about
    bookkeeping as if it were news. Rows from 2026-07-20 were pushed to Telegram at 11:20 on
    2026-08-03 formatted as fresh incidents. Nothing about them needed doing — the daemon had
    restarted many times since. Reconcile them, log them, do not wake anyone."""
    cmd = _make_app_command(timeout=300)
    ancient = _make_row("running", started_seconds_ago=14 * 24 * 3600, log_id=1)

    class _Store(FakeStore):
        def fetch_running_job_runs(self, job_code_prefix: str = ""):
            return [dict(ancient, job_code=cmd.app_command_id)]

    store = _Store()
    daemon.recover_stale_running_jobs(
        store=store, app_commands={cmd.app_command_id: cmd}, config=FakeConfig(), logger=None)

    assert len(store.updated) == 1          # still closed: the books must balance
    assert store.telegram_sent == []        # but it is not an incident


@pytest.fixture
def routed_alerts(monkeypatch):
    """Give the daemon a Telegram route instead of letting it read one.

    `recover_stale_running_jobs` asks `telegram_route(level)` where an alert should go, and that
    reads the operator's `telegram_config.json`. A tree without one resolves to no chat, the daemon
    returns before queueing, and a test about *how many* alerts a startup raises fails saying zero
    - which reads as the alert being broken rather than as the routing being absent.

    Routing is not what these tests are about. Supplying it makes them measure the thing they name:
    one alert for a whole startup, not one per stale row.
    """
    from db_ops.lib import notify_route

    monkeypatch.setattr(notify_route, "chat_from_route", lambda route: "chat-under-test")
    yield


def test_a_workflow_that_died_today_still_raises_one_alert(routed_alerts):
    """The guard above must not silence the case the alert exists for."""
    cmd = _make_app_command(timeout=300)
    recent = _make_row("running", started_seconds_ago=2 * 3600, log_id=2)

    class _Store(FakeStore):
        def fetch_running_job_runs(self, job_code_prefix: str = ""):
            return [dict(recent, job_code=cmd.app_command_id)]

    store = _Store()
    daemon.recover_stale_running_jobs(
        store=store, app_commands={cmd.app_command_id: cmd}, config=FakeConfig(telegram_enabled=True),
        logger=None)

    assert len(store.updated) == 1
    assert len(store.telegram_sent) == 1


def test_one_alert_carries_the_whole_startup_not_one_message_per_row(routed_alerts):
    """171 rows became 171 Telegram messages. An operator's phone is not a log file."""
    cmd = _make_app_command(timeout=300)
    rows = [dict(_make_row("running", started_seconds_ago=3600 + i, log_id=i),
                 job_code=cmd.app_command_id) for i in range(30)]

    class _Store(FakeStore):
        def fetch_running_job_runs(self, job_code_prefix: str = ""):
            return rows

    store = _Store()
    daemon.recover_stale_running_jobs(
        store=store, app_commands={cmd.app_command_id: cmd}, config=FakeConfig(telegram_enabled=True),
        logger=None)

    assert len(store.updated) == 30
    assert len(store.telegram_sent) == 1
    body = store.telegram_sent[0]["message_text"]
    assert "recent_crashes=30" in body
    assert "...and 20 more" in body          # bounded: the log keeps the rest


def test_a_failed_run_records_why_it_failed_not_just_that_it_did():
    """`job_runs.error_text` held only "Command exited with return code 1". Diagnosing the
    PostgreSQL NUL-byte failure meant reading source comments and correlating deploy timestamps —
    the traceback the child had already printed went to a log file and was never linked to the run
    that produced it."""
    text = daemon._failed_run_error_text(
        1, "Traceback (most recent call last):\n  ...\nValueError: invalid byte sequence", "")

    assert "return code 1" in text
    assert "ValueError: invalid byte sequence" in text


def test_the_cause_falls_back_to_stdout():
    """Several db_ops CLIs print their failure to stdout, not stderr."""
    text = daemon._failed_run_error_text(2, "", "ERROR: target not found")

    assert "ERROR: target not found" in text


def test_the_tail_is_kept_because_a_traceback_ends_with_its_cause():
    noise = "chatty progress line\n" * 500
    text = daemon._failed_run_error_text(1, noise + "RuntimeError: the actual cause", "")

    assert text.endswith("RuntimeError: the actual cause")
    assert len(text) <= daemon.FAILED_RUN_ERROR_TEXT_CHARS + 200
    assert "earlier chars omitted" in text


def test_a_successful_run_records_no_error_text():
    assert daemon._failed_run_error_text(0, "", "") == "Command exited with return code 0."

# ---------------------------------------------------------------------------
# A clean stop is not a crash
# ---------------------------------------------------------------------------
def test_a_clean_stop_closes_its_own_rows_so_the_next_start_has_nothing_to_report():
    """`docker stop` sends SIGTERM, and Python's default action is to die where it stands.

    So every deploy left this daemon's app-command rows at `running`, and the next startup could
    not tell that from a crash: "Stale running workflows recovered on startup. recent_crashes=2",
    at error level, to the alert chat, every single time. Eight of those went out on 2026-08-08
    and not one was true. Closing the rows on the way out is what makes the remaining alerts mean
    something - an open row at startup is now really a crash or a SIGKILL.
    """
    from types import SimpleNamespace

    closed = []

    class _Store:
        def update_job_run(self, **kwargs):
            closed.append(kwargs)

    command = daemon.AppCommand(
        app_command_id="APP-METRICS", app_code="m", app_name="metrics", display_name="Metrics",
        log_scope="metrics", working_dir=".", command_text="x",
        time_window=TimeWindow(timeout=300), active=True,
    )
    running = {"APP-METRICS": SimpleNamespace(app_command=command, log_id=7)}

    n = daemon.close_running_on_shutdown(store=_Store(), logger=None,
                                         running_commands=running, reason="signal_15")

    assert n == 1
    assert closed[0]["log_id"] == 7
    # Retry still has to resume it: job_due only restarts a run whose last status is an error
    # status, so a friendlier word here would read as "finished, never repeat".
    assert closed[0]["status"] == "timeout"
    assert closed[0]["level"] == "logging", "a deliberate stop is not an error"
    assert closed[0]["error_text"] is None
    assert "daemon stopped" in closed[0]["message"]


def test_tidying_up_never_stops_the_daemon_exiting():
    """A store that is already gone must not turn a shutdown into a traceback."""
    from types import SimpleNamespace

    class _Broken:
        def update_job_run(self, **_kwargs):
            raise RuntimeError("store is gone")

    command = daemon.AppCommand(
        app_command_id="APP-X", app_code="x", app_name="x", display_name="X", log_scope="x",
        working_dir=".", command_text="x", time_window=TimeWindow(timeout=60), active=True,
    )

    assert daemon.close_running_on_shutdown(
        store=_Broken(), logger=None,
        running_commands={"APP-X": SimpleNamespace(app_command=command, log_id=1)},
        reason="signal_15") == 0


def test_sigterm_unwinds_instead_of_killing_the_process():
    """The handler has to raise something the broad `except Exception` handlers cannot swallow."""
    assert not issubclass(daemon._DaemonStopped, Exception)
    assert issubclass(daemon._DaemonStopped, BaseException)

