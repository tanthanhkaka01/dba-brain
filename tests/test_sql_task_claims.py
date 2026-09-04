import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from db_ops.lib.text_format import format_message_time
from db_ops.lib.time_window import MANUAL_ONLY, TimeWindow
from db_ops.db import DbOpsStore
from db_ops.sql_tasks import runner


class RecordingSqlRunStore:
    def __init__(self):
        self.inserted = []
        self.updated = []
        self.messages = []

    def insert_sql_run(self, **kwargs):
        self.inserted.append(kwargs)
        return len(self.inserted)

    def update_sql_run(self, **kwargs):
        self.updated.append(kwargs)

    def insert_telegram_send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeLogger:
    def __init__(self):
        self.records = []

    def log(self, level, message, extra=None):
        self.records.append({"level": level, "message": message, "extra": extra or {}})


def sql_command(*, script_type="single", script_files=("001.sql",), sql_id=9, sql_code="SQLSERVER-009"):
    return runner.SqlCommand(
        sql_id=sql_id,
        sql_code=sql_code,
        sql_name="Deploy task",
        db_type="sqlserver",
        script_type=script_type,
        script_path=None,
        script_paths=(),
        script_files=tuple(script_files),
        active=True,
    )


def sql_target(*, sql_id=9, target_no=1, database_name="APPDB", repeat_interval=60, timeout=60,
               output_format="none", output_chat="", output_chat_id="", output_max_rows=0,
               alert_on_error=None):
    return runner.SqlTarget(
        sql_id=sql_id,
        target_no=target_no,
        server_id="server",
        db_type="sqlserver",
        service_name="svc",
        instance_name="inst",
        credential_name="cred",
        time_window=TimeWindow(from_day=1, to_day=31, from_hour=0, to_hour=23, repeat_interval=repeat_interval, timeout=timeout),
        active=True,
        database_name=database_name,
        # Every target add_sql_task writes carries this rule; without it the notify fallback in
        # resolve_output_chat_id has nothing to fall back to.
        logging_on_run=runner.NotifyRule(enabled=True, telegram_chat="logging"),
        alert_on_error=alert_on_error or runner.NotifyRule(),
        output_format=output_format,
        output_chat=output_chat,
        output_chat_id=output_chat_id,
        output_max_rows=output_max_rows,
    )


def inventory_and_credentials():
    return (
        [{"server_id": "server", "ip": "127.0.0.1", "databases": [{"db_type": "sqlserver", "service_name": "svc", "instance_name": "inst"}]}],
        [{"server_id": "server", "db_type": "sqlserver", "service_name": "svc", "instance_name": "inst", "credentials": [{"credential_name": "cred", "username": "user", "password": "pass"}]}],
    )


def insert_running_sql(store, *, run_key="9|1|server|sqlserver|svc|inst|APPDB", started_at="2026-01-01T00:00:00Z"):
    return store.insert_sql_run(
        run_key=run_key,
        sql_id=9,
        sql_code="SQLSERVER-009",
        target_no=1,
        server_id="server",
        db_type="sqlserver",
        service_name="svc",
        instance_name="inst",
        database_name="APPDB",
        credential_name="cred",
        status="running",
        level="logging",
        message="started",
        started_at=started_at,
        metadata={"claimed_by": "worker"},
    )


def _insert_error_run(store, *, run_key, finished_at):
    # sql_run_time() falls back to started_at when finished_at is NULL, so started_at carries
    # the run time here; insert_sql_run has no finished_at parameter.
    return store.insert_sql_run(
        run_key=run_key, sql_id=9, sql_code="SQLSERVER-009", target_no=1, server_id="server",
        db_type="sqlserver", service_name="svc", instance_name="inst", database_name="APPDB",
        credential_name="cred", status="error", level="error", message="failed",
        started_at=finished_at,
    )


def test_failing_sql_task_backs_off_instead_of_running_every_tick(tmp_path):
    """Regression: a task whose only runs are 'error' must not be re-run every scan tick.
    Before the fix due_sql_tasks looked only at done/running runs, so a failing task had no
    recent run on record and repeat_due(None) was always True -> it hammered the target."""
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    commands = {9: sql_command()}
    target = sql_target(repeat_interval=72000, timeout=7200)
    targets = [target]
    run_key = target.run_key

    # A failure one minute ago: inside repeat_interval AND inside the default retry backoff.
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_error_run(store, run_key=run_key, finished_at=recent)
    latest_dr = store.fetch_latest_done_or_running_sql_runs_by_run_key()   # empty: no done/running
    latest_any = store.fetch_latest_sql_runs_by_run_key()                  # has the error row
    due = runner.due_sql_tasks(commands=commands, targets=targets,
                               latest_runs=latest_dr, latest_any_runs=latest_any)
    assert due == [], "a task that just failed must back off, not re-run this tick"

    # A failure well beyond the interval: due again.
    old = (datetime.now(timezone.utc) - timedelta(seconds=80000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_error_run(store, run_key=run_key, finished_at=old)
    # keep only the old error as the latest by clearing the recent one
    with sqlite3.connect(tmp_path / "db_ops.sqlite") as conn:
        conn.execute("DELETE FROM sql_runs WHERE started_at = ?;", (recent,))
        conn.commit()
    latest_any = store.fetch_latest_sql_runs_by_run_key()
    due = runner.due_sql_tasks(commands=commands, targets=targets,
                               latest_runs={}, latest_any_runs=latest_any)
    assert len(due) == 1, "a task that failed longer ago than its interval should run again"


@pytest.mark.xfail(strict=True, reason="Design gap: sql_runs has no atomic active claim/unique RUNNING row per run_key.")
def test_concurrent_sql_task_workers_claim_one_run(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()

    first_id = insert_running_sql(store)
    second_id = insert_running_sql(store)

    with sqlite3.connect(tmp_path / "db_ops.sqlite") as conn:
        rows = conn.execute(
            "SELECT sql_run_id, status, metadata_json FROM sql_runs WHERE run_key = ? AND status = 'running';",
            ("9|1|server|sqlserver|svc|inst|APPDB",),
        ).fetchall()

    assert first_id == second_id
    assert len(rows) == 1
    assert json.loads(rows[0][2])["claimed_by"]


@pytest.mark.xfail(strict=True, reason="Design gap: folder SQL deploys have no durable per-target database lock.")
def test_same_folder_deploy_cannot_execute_concurrently_for_same_target_database(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()

    insert_running_sql(store, run_key="13|1|server|sqlserver|svc|inst|APPDB")
    insert_running_sql(store, run_key="13|1|server|sqlserver|svc|inst|APPDB")

    with sqlite3.connect(tmp_path / "db_ops.sqlite") as conn:
        running_count = conn.execute(
            "SELECT count(*) FROM sql_runs WHERE run_key = ? AND status = 'running';",
            ("13|1|server|sqlserver|svc|inst|APPDB",),
        ).fetchone()[0]

    assert running_count == 1


@pytest.mark.xfail(strict=True, reason="Design gap: sql_runs has no active-run uniqueness constraint for duplicate run_key inserts.")
def test_race_condition_insert_result_does_not_duplicate_run_key(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    run_ids = [insert_running_sql(store), insert_running_sql(store)]

    assert len(set(run_ids)) == 1
    latest = store.fetch_latest_done_or_running_sql_runs_by_run_key()
    assert latest["9|1|server|sqlserver|svc|inst|APPDB"]["sql_run_id"] == run_ids[0]


@pytest.mark.xfail(strict=True, reason="Design gap: no claim_sql_run API or TTL lease expiration exists.")
def test_lock_expiration_allows_reclaim_after_ttl(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    stale_started = (datetime.now(timezone.utc) - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
    insert_running_sql(store, started_at=stale_started)

    assert hasattr(store, "claim_sql_run")
    claim = store.claim_sql_run(run_key="9|1|server|sqlserver|svc|inst|APPDB", claimed_by="worker-2", ttl_seconds=60)
    assert claim["claimed_by"] == "worker-2"
    assert claim["status"] == "running"


@pytest.mark.xfail(strict=True, reason="Design gap: no heartbeat_sql_run API exists to protect active leases from stealing.")
def test_heartbeat_prevents_lock_steal(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    insert_running_sql(store)

    assert hasattr(store, "heartbeat_sql_run")
    store.heartbeat_sql_run(run_key="9|1|server|sqlserver|svc|inst|APPDB", claimed_by="worker-1")
    stolen = store.claim_sql_run(run_key="9|1|server|sqlserver|svc|inst|APPDB", claimed_by="worker-2", ttl_seconds=60)
    assert stolen is None


def _stale_running_row(*, sql_run_id=77, age_seconds=120):
    """One abandoned run, as the reaper reads them: a list of rows, not a map keyed by run_key."""
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        {
            "sql_run_id": sql_run_id,
            "run_key": "9|1|server|sqlserver|svc|inst|APPDB",
            "sql_id": 9,
            "sql_code": "SQLSERVER-009",
            "target_no": 1,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "created_at": started_at,
        }
    ]


def test_stale_running_sql_run_is_marked_error_before_retry():
    store = RecordingSqlRunStore()

    runner.mark_stale_running_sql_runs(
        store=store,
        commands={9: sql_command()},
        targets=[sql_target(timeout=1)],
        running_runs=_stale_running_row(),
        telegram_groups={},
        logger=FakeLogger(),
    )

    assert store.updated[0]["sql_run_id"] == 77
    assert store.updated[0]["status"] == "error"
    assert store.updated[0]["finished_at"]
    assert "stale running exceeded timeout_seconds=1" in store.updated[0]["error_text"]


def test_a_run_that_a_later_run_replaced_is_still_reaped(tmp_path):
    """The 2026-09-04 silence: two runs of sql_id 28 were killed by a worker restart, and each
    time the next cycle started a fresh run while the killed row was still inside its timeout.
    From that moment the killed row was no longer the latest of its run_key, and the reaper -
    which read `fetch_latest_done_or_running_sql_runs_by_run_key` - could not see it again. It
    stayed `running` all day: no error row, no alert, and nothing in the error chat between
    07:00 and 20:00 local while two runs had in fact died."""
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    run_key = "9|1|server|sqlserver|svc|inst|APPDB"
    abandoned = insert_running_sql(store, started_at="2026-01-01T00:00:00Z")
    # The run that took its place and finished normally - which is what hid the row above.
    replacement = insert_running_sql(store, started_at="2026-01-01T00:05:00Z")
    store.update_sql_run(sql_run_id=replacement, status="done", level="logging",
                         message="finished", finished_at="2026-01-01T00:06:00Z")

    latest = store.fetch_latest_done_or_running_sql_runs_by_run_key()
    assert latest[run_key]["sql_run_id"] == replacement, "the newer run is the schedule's view"
    assert [row["sql_run_id"] for row in store.fetch_running_sql_runs()] == [abandoned]


def test_a_reaped_run_alerts_the_error_chat_like_any_other_failure():
    """A run whose process was killed never reaches the exception handler in run_sql_target, so
    for a long time this whole error class was written to the store and to nothing else. sql_id 28
    sat in `error` for half an hour on 2026-09-03 with no message anywhere, while the SQL it had
    started went on running on the server and blocked every following cycle."""
    store = RecordingSqlRunStore()
    target = sql_target(timeout=1, alert_on_error=runner.NotifyRule(enabled=True, telegram_chat="sql"))

    runner.mark_stale_running_sql_runs(
        store=store,
        commands={9: sql_command()},
        targets=[target],
        running_runs=_stale_running_row(),
        telegram_groups={"sql": "chat-7"},
        logger=FakeLogger(),
    )

    assert len(store.messages) == 1
    assert store.messages[0]["tlgchat_id"] == "chat-7"
    assert "stale running exceeded timeout_seconds=1" in store.messages[0]["message_text"]
    # The lesson of 2026-09-03: the row is closed but the server may not be.
    assert "may still be" in store.messages[0]["message_text"]


def test_the_alert_says_when_the_run_died_and_on_which_clock():
    """A reap can be reported hours after the run it is about - the row is only revisited on the
    next scan - so an alert carrying no time reads as "this is happening now". Two times, both
    labelled with their offset: when the run started, and when this message was written."""
    store = RecordingSqlRunStore()
    target = sql_target(timeout=1, alert_on_error=runner.NotifyRule(enabled=True, telegram_chat="sql"))

    runner.mark_stale_running_sql_runs(
        store=store,
        commands={9: sql_command()},
        targets=[target],
        running_runs=_stale_running_row(age_seconds=3600),
        telegram_groups={"sql": "chat-7"},
        logger=FakeLogger(),
    )

    text = store.messages[0]["message_text"]
    started = datetime.now(timezone.utc) - timedelta(seconds=3600)
    assert f"It started at {format_message_time(started)}" in text
    assert "was still 'running' 60 minutes later" in text
    assert any(line.startswith("time: ") and "UTC+00:00" in line for line in text.splitlines())


def test_a_target_that_does_not_want_error_alerts_is_still_not_told():
    """alert_on_error is the switch for this, exactly as it is for a task that fails normally -
    reaping a run must not become a back door that ignores the target's own notify block."""
    store = RecordingSqlRunStore()

    runner.mark_stale_running_sql_runs(
        store=store,
        commands={9: sql_command()},
        targets=[sql_target(timeout=1)],
        running_runs=_stale_running_row(),
        telegram_groups={"sql": "chat-7"},
        logger=FakeLogger(),
    )

    assert store.updated[0]["status"] == "error"
    assert store.messages == []


def test_array_script_failure_stops_later_script_and_records_failed_file(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    script_dir = data_dir / "sql" / "tasks" / "sqlserver"
    script_dir.mkdir(parents=True)
    (script_dir / "001_ok.sql").write_text("SELECT 'ok';", encoding="utf-8")
    (script_dir / "002_fail.sql").write_text("SELECT 'fail';", encoding="utf-8")
    (script_dir / "003_after.sql").write_text("SELECT 'after';", encoding="utf-8")
    command = sql_command(
        script_type="array",
        script_files=(
            "sql/tasks/sqlserver/001_ok.sql",
            "sql/tasks/sqlserver/002_fail.sql",
            "sql/tasks/sqlserver/003_after.sql",
        ),
    )
    store = RecordingSqlRunStore()
    executed_scripts = []

    def fake_execute_sql(**kwargs):
        sql_text = kwargs["sql_text"].strip()
        executed_scripts.append(sql_text)
        if "fail" in sql_text:
            raise RuntimeError("boom in 002_fail.sql")
        return {"row_count": 1, "result_sets": []}

    monkeypatch.setattr(runner, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(runner, "log_event", lambda *args, **kwargs: None)
    inventory, credentials = inventory_and_credentials()

    success = runner.run_one_sql_task(
        store=store,
        data_dir=data_dir,
        telegram_groups={},
        command=command,
        target=sql_target(),
        inventory=inventory,
        credentials=credentials,
        secrets={},
        logger=None,
    )

    assert success is False
    assert executed_scripts == ["SELECT 'ok';", "SELECT 'fail';"]
    assert store.updated[-1]["status"] == "error"
    file_results = store.updated[-1]["metadata"]["file_results"]
    assert [item["file_name"] for item in file_results] == ["sql/tasks/sqlserver/001_ok.sql"]
    assert "002_fail.sql" in store.updated[-1]["error_text"]
    assert all("003_after" not in item["file_name"] for item in file_results)


def test_a_manual_task_is_never_picked_up_by_the_scheduler():
    """"Run this when I ask, not on a timer" is `time_window.repeat_interval = -1`.

    It is not `active: false` — that works for the scheduler but hides the task from
    /spbot_list_sql_tasks ("inactive entries hidden"), so the operator loses sight of a task
    they use regularly. And it is not `repeat_interval = 0`, which is run-once and therefore
    still runs the first time. -1 stays listed and forced-runnable while the scan skips it.
    """
    commands = {9: sql_command()}
    scheduled = sql_target()
    manual = sql_target(sql_id=9, target_no=2, repeat_interval=MANUAL_ONLY)

    due = runner.due_sql_tasks(commands=commands, targets=[scheduled, manual],
                               latest_runs={}, latest_any_runs={})

    assert [target.target_no for _command, target in due] == [1]
    assert manual.manual_only is True   # derived from the window, not a second flag
    assert manual.active is True        # still listed; only the schedule ignores it


def test_output_none_and_xlsx_keep_the_rows_out_of_the_message_body():
    """A target that ships its rows as a file, or wants status only, must not also paste the
    markdown table — for xlsx that duplicates the attachment, and `none` exists precisely to
    stop a maintenance UPDATE from dumping rows into the chat."""
    result = {"files": [{"result_sets": [{"columns": ["a"], "rows": [[1], [2]]}]}]}

    def queued_text(output_format):
        store = RecordingSqlRunStore()
        runner.enqueue_sql_task_message(
            store=store, telegram_groups={"logging": "chat-1"},
            rule=runner.NotifyRule(enabled=True, telegram_chat="logging"),
            command=sql_command(), target=sql_target(output_format=output_format),
            status="done", message="finished", sql_run_id=1, result=result,
        )
        return store.messages[-1]["message_text"]

    assert "| a |" in queued_text("plain")
    assert "| a |" not in queued_text("xlsx")
    assert "| a |" not in queued_text("none")


def test_a_target_must_say_what_it_does_with_its_rows():
    """This used to assert that an absent block reads as `plain`, and the concern behind it was
    right: inferring `none` from silence would quietly stop delivering results people depend on,
    with nothing in the config to show why.

    The concern is now met by a stronger means. Silence is not interpreted at all — it is
    refused, by name, with the block to add. And the thirteen targets that had relied on the
    default now say `plain` in `sql_targets.json`, which is what they were already doing, so
    nothing stopped being delivered. "Nothing in the config would show why" was the real
    complaint, and the config shows it now."""
    with pytest.raises(RuntimeError, match="'output' is required"):
        runner._target_output({"sql_id": 16})

    assert runner._target_output({"output": {"format": "none"}})["output_format"] == "none"
    for file_format in ("xlsx", "csv", "txt", "xml"):
        assert runner._target_output(
            {"output": {"format": file_format}})["output_format"] == file_format

    with pytest.raises(RuntimeError, match="output.format must be one of"):
        runner._target_output({"sql_id": 9, "output": {"format": "pdf"}})


def test_the_workbook_is_queued_as_its_own_document_message(tmp_path):
    """send_queue turns metadata.document_path into a Telegram document with the text as its
    caption, so the path has to travel on a queued row — a file written but not referenced is a
    file nobody receives."""
    store = RecordingSqlRunStore()
    result = {"files": [{"result_sets": [{"columns": ["PackageBarcode", "Quantity"],
                                          "rows": [["PB-1", 5], ["PB-2", 7]]}]}]}

    path = runner.write_sql_task_xlsx(
        command=sql_command(), target=sql_target(output_format="xlsx"),
        result=result, sql_run_id=1, output_dir=tmp_path / "out")

    assert path is not None and path.exists() and path.suffix == ".xlsx"
    runner.enqueue_sql_task_document(
        store=store, telegram_groups={"logging": "chat-1"},
        command=sql_command(), target=sql_target(output_format="xlsx"),
        sql_run_id=1, document_path=path, row_count=2,
    )
    assert store.messages[-1]["metadata"]["document_path"] == str(path)
    # The caption states no status of its own, so the header guess must stay off it.
    assert store.messages[-1]["message_type"] == "plain"


def test_the_export_goes_to_whoever_asked_for_it():
    """A forced run from Telegram passes the requesting chat. Without it the file lands in the
    target's notify chat — that is where the first xlsx went, to the "Ops - Logging" group,
    while the operator who typed /spbot_run_sql_task saw only "finished" and no file."""
    groups = {"logging": "chat-logging", "sql": "chat-sql"}
    unconfigured = sql_target(output_format="xlsx")

    # 1. the chat that asked wins over everything
    assert runner.resolve_output_chat_id(unconfigured, groups, override="chat-asked") == "chat-asked"
    # 2. then the target's own explicit chat_id, then its notify level
    assert runner.resolve_output_chat_id(
        sql_target(output_format="xlsx", output_chat_id="chat-pinned"), groups) == "chat-pinned"
    assert runner.resolve_output_chat_id(
        sql_target(output_format="xlsx", output_chat="sql"), groups) == "chat-sql"
    # 3. a target that sets a format but no chat still delivers, rather than dropping the file
    assert runner.resolve_output_chat_id(unconfigured, groups) == "chat-logging"


def test_an_xlsx_target_fetches_the_whole_result_not_the_100_row_preview():
    """The 100-row cap is the preview stored in sql_runs and pasted into a message. Applying it
    to an export produced a workbook holding the first 100 rows of a 5133-row answer — and
    nothing said so, because the file looked complete."""
    assert sql_target(output_format="xlsx").capture_max_rows == runner.XLSX_MAX_ROWS
    assert sql_target(output_format="plain").capture_max_rows == runner.MAX_RESULT_ROWS
    assert sql_target(output_format="none").capture_max_rows == runner.MAX_RESULT_ROWS
    assert runner.XLSX_MAX_ROWS > runner.MAX_RESULT_ROWS


def test_how_many_rows_reach_the_chat_is_the_targets_decision_not_a_literal():
    """`output.max_rows` is config because how much of an answer is worth reading belongs to the
    task. It used to be a hard 100 in the runner, which was invisible and applied to everything."""
    assert sql_target(output_format="plain").capture_max_rows == runner.DEFAULT_INLINE_MAX_ROWS
    assert sql_target(output_format="plain", output_max_rows=25).capture_max_rows == 25


def test_a_targets_row_request_cannot_exceed_the_flood_ceiling():
    """The bound on inline output is Telegram, not memory: rows go out ~30 to a message and a
    group is rate-limited to roughly 20 messages a minute, so an unbounded request would 429
    partway and arrive in pieces. A target that got past validation is still clamped."""
    assert sql_target(output_format="plain",
                      output_max_rows=10 ** 6).capture_max_rows == runner.MAX_INLINE_MAX_ROWS


def test_a_max_rows_the_transport_cannot_deliver_is_refused_at_load_rather_than_clamped():
    """Silently reducing 20000 to 5000 would look like it worked until somebody counted rows."""
    assert runner._target_output({"output": {"format": "plain", "max_rows": "250"}}
                                 )["output_max_rows"] == 250
    assert runner._target_output({"output": {"format": "plain"}})["output_max_rows"] == 0
    for bad in ("0", "-5", "20000", "lots"):
        with pytest.raises(RuntimeError, match="output.max_rows"):
            runner._target_output({"sql_id": 9, "output": {"format": "plain", "max_rows": bad}})


def test_showing_more_rows_in_chat_does_not_enlarge_every_stored_run_row():
    """`STORED_RESULT_MAX_ROWS` used to be `= MAX_RESULT_ROWS`. Raising how much an operator sees
    would then have multiplied the size of every `sql_runs.result_json` in Postgres as a side
    effect — the store's budget and the reader's are different questions."""
    assert runner.STORED_RESULT_MAX_ROWS == 100
    assert runner.DEFAULT_INLINE_MAX_ROWS > runner.STORED_RESULT_MAX_ROWS


def test_the_store_keeps_a_preview_even_when_the_export_fetched_everything():
    """The workbook needs every row; sql_runs.result_json must not also grow to hold them, or a
    single export turns into a multi-megabyte row in Postgres. row_count stays the real total —
    reporting it as the trimmed length would understate the export."""
    rows = [[i] for i in range(runner.STORED_RESULT_MAX_ROWS + 250)]
    result = {"row_count": len(rows), "files": [{"result_sets": [{"columns": ["n"], "rows": rows}]}]}

    stored = runner.trim_result_for_store(result)
    kept = stored["files"][0]["result_sets"][0]

    assert len(kept["rows"]) == runner.STORED_RESULT_MAX_ROWS
    assert kept["rows_omitted"] == 250
    assert stored["row_count"] == len(rows)
    # ... and the original is untouched, so the workbook still sees every row.
    assert len(result["files"][0]["result_sets"][0]["rows"]) == len(rows)


def test_a_script_that_returns_nothing_produces_no_workbook(tmp_path):
    """An UPDATE has no result set. Writing a workbook with a single empty sheet would send the
    operator a file that says nothing; the run still reports success on its own."""
    path = runner.write_sql_task_xlsx(
        command=sql_command(), target=sql_target(output_format="xlsx"),
        result={"files": [{"result_sets": []}]}, sql_run_id=1, output_dir=tmp_path / "out")

    assert path is None


def test_a_task_can_export_csv_and_txt_not_only_a_workbook(tmp_path):
    """`output.format` was xlsx-or-nothing, so a task whose consumer wanted a CSV had no way to
    say so and someone converted the workbook by hand. The file formats now go through
    db_ops.lib.result_format — the same renderer `run-sql --format` uses, so a scheduled
    export and an ad-hoc one cannot drift into two different-looking artifacts."""
    result = {"files": [{"result_sets": [{"columns": ["name", "qty"],
                                          "rows": [["PB-1", 5], ["PB-2", None]]}]}]}
    written = {}
    for file_format in ("csv", "txt", "xml"):
        path = runner.write_sql_task_output(
            command=sql_command(), target=sql_target(output_format=file_format),
            result=result, sql_run_id=1, output_dir=tmp_path / file_format,
        )
        assert path is not None and path.suffix == f".{file_format}"
        written[file_format] = path.read_text(encoding="utf-8")

    assert written["csv"].splitlines()[0] == '"name","qty"'
    # A NULL must not arrive as the string "None" in a file someone opens in a spreadsheet.
    assert written["csv"].splitlines()[2] == '"PB-2",'
    assert "NULL" in written["txt"]
    assert 'null="true"' in written["xml"]


def test_every_file_format_captures_the_whole_result_not_a_preview():
    """The row cap exists so a 20-row inline table does not pull thousands of rows into
    sql_runs.result_json. An export needs the opposite, and the rule was written as
    `== "xlsx"` — so csv and txt would have silently exported a 100-row preview."""
    for file_format in ("xlsx", "csv", "txt", "xml"):
        assert sql_target(output_format=file_format).capture_max_rows == runner.XLSX_MAX_ROWS
    assert sql_target(output_format="plain").capture_max_rows == runner.MAX_RESULT_ROWS


def test_a_file_export_replaces_the_inline_table_rather_than_repeating_it():
    assert runner.FILE_OUTPUT_FORMATS == ("xlsx", "csv", "txt", "xml", "json")
    for file_format in runner.FILE_OUTPUT_FORMATS:
        assert file_format not in {"plain", "none"}


# --------------------------------------------------------------------------- #
# Reading the history back, which nothing did until 2026-09-03
# --------------------------------------------------------------------------- #
def _history_row(**over):
    row = {"sql_run_id": 77, "sql_id": 9, "sql_code": "SQLSERVER-009", "target_no": 1,
           "server_id": "server", "service_name": "svc", "status": "done", "level": "logging",
           "message": "finished", "started_at": "2026-09-03T11:57:43Z",
           "finished_at": "2026-09-03T12:02:01Z", "duration_ms": 258000, "row_count": 69,
           "error_text": None}
    row.update(over)
    return row


def test_a_run_line_leads_with_the_status_because_that_is_what_is_scanned_for():
    """Read on a phone by whoever just got an alert. Everything else on the line is context for
    the one word they came for."""
    from db_ops.common import sql_run_history

    text = sql_run_history.render([_history_row()])

    assert "#77 [DONE] sql_id=9 SQLSERVER-009" in text
    assert "2026-09-03 11:57:43 took 258s rows=69 on server" in text


def test_a_failed_run_carries_its_reason_so_the_store_need_not_be_opened():
    """The trip to the store is exactly what this command exists to save. A listing that says
    ERROR and stops has not saved it."""
    from db_ops.common import sql_run_history

    text = sql_run_history.render([_history_row(
        status="error", duration_ms=None, row_count=None,
        error_text="stale running exceeded timeout_seconds=900.\nsecond line ignored")])

    assert "[ERROR]" in text
    assert "stale running exceeded timeout_seconds=900." in text
    assert "second line ignored" not in text, "one line of the reason, not the whole traceback"


def test_a_successful_run_does_not_repeat_a_stale_error_column():
    """`error_text` outlives the row it belonged to in some shapes; printing it beside DONE would
    report a failure that did not happen."""
    from db_ops.common import sql_run_history

    text = sql_run_history.render([_history_row(error_text="an error from a previous attempt")])

    assert "an error from a previous attempt" not in text


def test_the_listing_stops_before_telegram_truncates_it_and_says_so():
    """Telegram cuts at 4096 characters. A listing the transport truncates loses its NEWEST rows
    with nothing to say it happened, which is the wrong end and a silent one."""
    from db_ops.common import sql_run_history

    rows = [_history_row(sql_run_id=n, sql_code="SQLSERVER-%03d-A-RATHER-LONG-TASK-NAME" % n)
            for n in range(200)]

    text = sql_run_history.render(rows)

    assert len(text) < 4096
    assert "more not shown (message size limit)." in text
    assert "#0 " in text, "the newest rows are the ones that must survive"


def test_an_empty_history_says_so_rather_than_printing_a_bare_header():
    from db_ops.common import sql_run_history

    assert sql_run_history.render([]) == "No SQL task runs recorded yet."
    assert sql_run_history.render([], sql_id=28) == "No SQL task runs recorded for sql_id 28 yet."


def test_the_limit_is_bounded_so_a_typo_cannot_ask_for_the_whole_table():
    from db_ops.common import sql_run_history

    asked = []

    class Store:
        def fetch_recent_sql_runs(self, *, limit, sql_id):
            asked.append(limit)
            return []

    sql_run_history.collect(Store(), limit=10_000)
    sql_run_history.collect(Store(), limit=0)

    assert asked == [sql_run_history.MAX_LIMIT, 1]

