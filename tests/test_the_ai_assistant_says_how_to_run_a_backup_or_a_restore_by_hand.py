"""Asked how to run a backup or a restore by hand, the assistant answers from the entry's own
configuration, and every line it gives is one the real CLI takes (0.38.0, 1.327).

The operator, 2026-11-11: *backup and restore are complex - when a user asks over Telegram, the AI
must answer how to run one by hand, from the CLI and from a Telegram command, for a backup and for
a restore - thoroughly.* A line written from memory is how a person ends up typing a command that
refuses their entry, or one that does more than they meant:

* ``restore-latest`` does not run a script-driven entry and ``restore-by-id`` does not run a
  share-driven one - each kind is given only the commands that run it;
* ``/spbot_backup`` names no job, so on an entry with two jobs it runs both - said, beside the CLI
  line that runs one;
* ``backup --force`` never runs an inactive job - said before the line, not found out after it;
* a line is written for the node it is typed on, and for the master that reaches the worker.

So the lines are built by ``lib.backup_runbook`` from what the entry states, and held here to the
parsers that will read them: ``backup_restore.cli`` for the command, ``control.cli`` for the
master's ``worker-run`` form, and the bot's own parameter patterns for the Telegram command.
The estate below is made up (documentation addresses); it has one entry of every kind the sheet
names: the store's FULL, a SQL Server FULL, DIFF and LOG, a Windows server read over SSH and over
a share, and a restore from the server and from the repository.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dbabrain.backup_restore import cli as backup_cli
from dbabrain.backup_restore import config as backup_config
from dbabrain.control import cli as control_cli
from dbabrain.db.backend import StoreTarget
from dbabrain.lib import ai_assist as A
from dbabrain.lib import backup_runbook as R
from dbabrain.lib.restore import kind as restore_kind
from dbabrain.telegram import ai_tools as T

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The bot commands every node is seeded with: the runbook finds the ones that run a backup or a
#: restore by what they run, so the shipped catalogue is the fixture.
SHIPPED_COMMANDS = json.loads(
    (REPO_ROOT / "dbabrain" / "telegram" / "catalogue" / "telegram_support_commands.json").read_text(encoding="utf-8"))
TWINS = R.bot_twins(SHIPPED_COMMANDS)

STORE, WINDOWS, REPOSITORY, LAB, OTHER_PROD = (
    "ACME-192-0-2-20-PG-5432", "ACME-192-0-2-10", "ACME-192-0-2-30-HOST", "ACME-192-0-2-40-HOST",
    "ACME-192-0-2-50")
ENVIRONMENTS = {STORE: "production", WINDOWS: "production", LAB: "lab", OTHER_PROD: "prod"}
#: A weekday afternoon on the node's own clock: outside every nightly window below.
NOW = datetime(2026, 11, 11, 14, 20, tzinfo=timezone(timedelta(hours=7)))

NIGHT = {"from_hour": 1, "to_hour": 5, "repeat_interval": 72000, "retry_interval": 3600, "timeout": 7200}
NOTIFY = {"logging_on_run": {"enabled": True, "telegram_chat": "backup", "chat_id": ""},
          "alert_on_error": {"enabled": True, "telegram_chat": "backup", "chat_id": ""}}
NO_REPOSITORY = {"offload_enabled": False, "offload_server_id": "", "offload_folder": ""}


def _repository(**changes) -> dict:
    return {"offload_enabled": True, "offload_server_id": REPOSITORY, "offload_folder": "/mnt/repo",
            "offload_copy_max_retry": 3, "offload_transfer": "cat", "offload_encrypt": True,
            "offload_delete_source": True, "offload_source_keeps_newest_chain": True,
            "offload_repository_check": True, "offload_files_per_pass": 100, "offload_source_share": "",
            "offload_source_username": "", "offload_retention": {}, "offload_files_since": "", **changes}


def _job(name: str, level: str = "", *, script: str, window: dict | None = None, **more) -> dict:
    return {"job": name, "active": True, "script": script, "time_window": window or NIGHT, "notify": NOTIFY,
            "databases": [], "all_databases": True, **({"env": {"BACKUP_LEVEL": level}} if level else {}), **more}


ESTATE = {"backup_restore": {
    "backups": [
        # The store's FULL: a weekly baseline and a daily incremental in one entry, encrypted on its
        # way to the repository and released at the source.
        {"backup_id": "ACME_STORE_PG", "active": True, "db_type": "postgresql", "server_id": STORE,
         "backup_dir": "/opt/pgbackup/dbops", "cleanup_retention": {"default": "keep"},
         "note": "The runtime store's own cluster.",
         "jobs": [_job("database_full", "full", script="assets/backup/postgresql/pg_basebackup_database.sh",
                       window={**NIGHT, "weekdays": [7]}),
                  _job("database", "incr", script="assets/backup/postgresql/pg_basebackup_database.sh",
                       window={**NIGHT, "weekdays": [1, 2, 3, 4, 5, 6]})],
         **_repository(offload_source_keeps_newest_chain=False, offload_files_since="2026-11-01T00:00:00Z")},
        # A Windows SQL Server, read over the SSH it already answers.
        {"backup_id": "ACME_APPDB_FULL", "active": True, "db_type": "sqlserver", "server_id": WINDOWS,
         "backup_dir": "D:\\SQLBK\\dbops", "cleanup_retention": 1209600,
         "jobs": [_job("full", "full", script="assets/backup/sqlserver/mssql_backup_database.sh",
                       databases=["APPDB", "FINDB"], all_databases=False)],
         **_repository(offload_transfer="sftp")},
        # The same server, read over its share.
        {"backup_id": "ACME_APPDB_DIFF", "active": True, "db_type": "sqlserver", "server_id": WINDOWS,
         "backup_dir": "D:\\SQLBK\\dbops", "cleanup_retention": 1209600,
         "jobs": [_job("diff", "diff", script="assets/backup/sqlserver/mssql_backup_database.sh")],
         **_repository(offload_source_share="\\\\192.0.2.10\\D$\\SQLBK", offload_source_username="svc_backup",
                       offload_encrypt=False, offload_delete_source=False)},
        # Its log stream: every quarter hour, quiet unless it fails, never copied anywhere.
        {"backup_id": "ACME_APPDB_LOG", "active": True, "db_type": "sqlserver", "server_id": WINDOWS,
         "backup_dir": "D:\\SQLBK\\dbops", "cleanup_retention": 604800,
         "jobs": [_job("log", "log", script="assets/backup/sqlserver/mssql_backup_log.sh",
                       window={"repeat_interval": 900, "retry_interval": 300, "timeout": 900},
                       notify={**NOTIFY, "logging_on_run": {"enabled": False, "telegram_chat": "backup"}})],
         **NO_REPOSITORY},
        # Retired: switched off, kept for its history.
        {"backup_id": "ACME_OLD_ORA", "active": False, "db_type": "oracle", "server_id": OTHER_PROD,
         "backup_dir": "/opt/orabackup", "cleanup_retention": 691200,
         "jobs": [_job("database", "0", script="assets/backup/oracle/rman_database.sh")], **NO_REPOSITORY},
    ],
    "restores": [
        # Script-driven, from the server's own folder.
        {"restore_id": "ACME_STORE_TO_LAB", "db_type": "postgresql", "active": True, "server_id": STORE,
         "target_server_id": LAB, "target_container": "PG_LAB", "backup_dir": "/opt/pgbackup/dbops",
         "target_backup_dir": "/opt/restore/acme_store", "script": "assets/backup/postgresql/pg_restore_drill.sh",
         "cleanup_retention": 691200, "time_window": NIGHT, "notify": NOTIFY, "action": "restore",
         "restore_from": "server"},
        # Script-driven, from the repository's proven copies.
        {"restore_id": "ACME_STORE_FROM_REPO", "db_type": "postgresql", "active": True, "server_id": STORE,
         "target_server_id": LAB, "target_container": "PG_LAB2", "backup_dir": "/opt/pgbackup/dbops",
         "target_backup_dir": "/opt/restore/acme_store_repo", "script": "assets/backup/postgresql/pg_restore_drill.sh",
         "cleanup_retention": 691200, "time_window": NIGHT, "notify": NOTIFY, "action": "restore",
         "restore_from": "repository"},
        # Share-driven: SQL Server from the backup share, database by database.
        {"restore_id": "ACME_APPDB_TO_DRILL", "active": True, "server_id": WINDOWS, "target_server_id": LAB,
         "overwrite_existing": True, "cleanup_retention": 86400, "time_window": NIGHT, "notify": NOTIFY,
         "source": {"backup_share": "\\\\192.0.2.10\\SQLBK\\APPDB"},
         "target": {"sql_container": "MSSQL_DRILL_1433", "vm_import_linux_path": "/opt/restore/import/appdb"},
         "database_mappings": [{"source_database": "APPDB", "target_database": "APPDB_DRILL"}],
         "action": "restore", "restore_from": "server"},
        # Mis-aimed: its target is a production instance and the entry does not allow one.
        {"restore_id": "ACME_APPDB_ONTO_PROD", "db_type": "sqlserver", "active": True, "server_id": WINDOWS,
         "target_server_id": OTHER_PROD, "target_container": "MSSQL_PROD", "backup_dir": "/var/opt/mssql/backup",
         "target_backup_dir": "/opt/restore/appdb", "script": "assets/backup/sqlserver/mssql_restore_drill.sh",
         "cleanup_retention": 86400, "time_window": NIGHT, "notify": NOTIFY, "action": "restore",
         "restore_from": "server"},
        # In place: the target is a second container on the source's own host.
        {"restore_id": "ACME_ORA_IN_PLACE", "db_type": "oracle", "active": False, "server_id": OTHER_PROD,
         "target_server_id": OTHER_PROD, "target_container": "ORA_DRILL", "backup_dir": "/opt/orabackup",
         "script": "assets/backup/oracle/rman_restore_drill.sh", "cleanup_retention": 691200,
         "time_window": NIGHT, "notify": NOTIFY, "action": "restore", "restore_from": "server"},
    ],
}}

#: The worker: a container, pip-installed, bash - where the assistant runs in an estate.
WORKER = R.node_form({"install": "pip", "shell": "bash", "runtime": "docker", "python": "/usr/local/bin/python"},
                     node_role="worker", has_worker=False)
#: The master: a Windows PC running a source tree from its venv, with a worker in its config.json.
MASTER = R.node_form({"install": "source", "shell": "powershell-5.1", "runtime": "host",
                      "python": r"D:\dbabrain\.venv\Scripts\python.exe"}, node_role="master", has_worker=True)
#: One machine doing everything: no worker to reach.
SINGLE = R.node_form({"install": "pip", "shell": "bash", "runtime": "host", "python": "/usr/bin/python3"},
                     node_role="master", has_worker=False)
BACKUP_IDS = [item["backup_id"] for item in ESTATE["backup_restore"]["backups"]]
RESTORE_IDS = [item["restore_id"] for item in ESTATE["backup_restore"]["restores"]]


def _build(node: R.Node = WORKER, **asked) -> dict:
    return R.build(ESTATE, node=node, twins=TWINS, environments=ENVIRONMENTS, now=NOW,
                   report_base="http://192.0.2.115:8080/report_dba/", **asked)


def _backup(backup_id: str, node: R.Node = WORKER) -> dict:
    return _build(node, backup_id=backup_id)["backups"][0]


def _restore(restore_id: str, node: R.Node = WORKER) -> dict:
    return _build(node, restore_id=restore_id)["restores"][0]


def _step(runbook: dict, name: str) -> dict:
    return next(step for step in runbook["steps"] if step["step"] == name)


def _argv(line: str) -> list[str]:
    """A line as its shell hands it to the process. A Windows path keeps its backslashes, as
    PowerShell keeps them; elsewhere the split is bash's own."""
    if "\\" not in line:
        return shlex.split(line)
    parts = shlex.split(line, posix=False)
    return [part[1:-1] if len(part) > 1 and part[0] == part[-1] == '"' else part for part in parts]


def _parsed(line: str, node: R.Node):
    """The line as ``backup_restore.cli`` reads it - its real parser, or the test fails here."""
    argv = _argv(line)
    assert argv[:len(node.cli)] == list(node.cli), line
    return backup_cli.parse_args(argv[len(node.cli):])


def _parsed_from_master(line: str, node: R.Node, monkeypatch):
    """The master's line as ``control.cli`` reads it, and what follows ``--`` as the worker's
    ``backup_restore.cli`` does."""
    # The worker's login comes from the master's config.json; the test has none to read.
    monkeypatch.setattr(control_cli, "default_worker_user", lambda: "dba_user")
    argv = _argv(line)
    assert argv[:len(node.control)] == list(node.control), line
    outer = control_cli.parse_args(argv[len(node.control):])
    # No key on the line: worker-run reads the passphrase from the master's environment.
    assert outer.command == "worker-run" and outer.key_base64 is None and outer.key is None
    inner = [token for token in outer.cmd if token != "--"]
    assert inner[:3] == ["python", "-m", R.BACKUP_RESTORE_MODULE], line
    return backup_cli.parse_args(inner[3:])


# --------------------------------------------------------------------------- #
# Every line is one the real parsers take
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("node", [WORKER, MASTER, SINGLE], ids=["worker", "master", "single-node"])
@pytest.mark.parametrize("backup_id", BACKUP_IDS)
def test_every_line_of_a_backup_entry_is_one_the_cli_takes_and_names_that_entry(node, backup_id, monkeypatch):
    runbook = _backup(backup_id, node)
    lines = 0
    for step in runbook["steps"]:
        for parsed in ([_parsed(step["cli"], node)]
                       + ([_parsed_from_master(step["from_master"], node, monkeypatch)]
                          if "from_master" in step else [])):
            lines += 1
            assert parsed.backup_id == backup_id and parsed.config == "config.json"
            if parsed.command == "backup":
                assert parsed.force is True and parsed.job in [job["job"] for job in runbook["jobs"]]
                assert parsed.dry_run is (step["kind"] == "dry_run")
            elif parsed.command == "offload":
                assert parsed.dry_run is (step["kind"] == "dry_run")
            else:
                assert parsed.command in ("verify-backup-files", "backup-stop")
        assert ("from_master" in step) is node.has_worker
    assert lines >= 4 * (2 if node.has_worker else 1)
    assert _parsed(runbook["list"]["cli"], node).command == "list-backups"


@pytest.mark.parametrize("node", [WORKER, MASTER, SINGLE], ids=["worker", "master", "single-node"])
@pytest.mark.parametrize("restore_id", RESTORE_IDS)
def test_every_line_of_a_restore_entry_is_one_the_cli_takes_and_names_that_entry(node, restore_id, monkeypatch):
    runbook = _restore(restore_id, node)
    for step in runbook["steps"]:
        parsed = ([_parsed(step["cli"], node)] if "cli" in step else []) + (
            [_parsed_from_master(step["from_master"], node, monkeypatch)] if "from_master" in step else [])
        for args in parsed:
            if args.command == "restore-by-id":
                request = json.loads(args.request)
                assert request["restore_id"] == restore_id
                assert bool(request.get("dry_run")) is (step["kind"] == "dry_run")
            else:
                assert args.command in ("restore-workflow", "restore-latest") and args.restore_id == restore_id
                assert args.dry_run is (step["kind"] == "dry_run")
            assert args.config == "config.json"
        assert parsed or "cli_not_written" in step, step["step"]
    assert _parsed(runbook["list"]["cli"], node).command == "list-restores"


def test_the_point_in_time_line_carries_the_moment_as_one_argument():
    step = _step(_restore("ACME_STORE_TO_LAB"), "Restore to a moment (point in time)")
    assert _parsed(step["cli"], WORKER).point_in_time == R.MOMENT_PLACEHOLDER
    assert R.MOMENT_PLACEHOLDER in step["fill_in"], "the one thing the reader fills in is named"
    # ... and the form asked for is the one the CLI reads a moment in.
    from dbabrain.backup_restore.restore_database import parse_point_in_time

    assert parse_point_in_time("2026-11-11 02:30:00 +07:00") is not None
    assert re.fullmatch(r"YYYY-MM-DD HH:MM:SS \+HH:MM", R.MOMENT_PLACEHOLDER)


def test_the_telegram_command_fits_the_bots_own_parameters():
    """Position by position, each argument held to the pattern the shipped command declares - the
    check the bot itself makes before it runs one."""
    by_name = {record["command_text"]: record for record in SHIPPED_COMMANDS["telegram_support_commands"]}

    def fits(line: str) -> None:
        command, *given = line.split(" ", 2) if "restore" in line or "copy" in line else line.split(" ")
        declared = sorted((item for item in by_name[command.lstrip("/")]["action_config"]["parameters"]
                           if item.get("source", "arg") == "arg"), key=lambda item: item["position"])
        assert len(given) <= len(declared)
        for value, parameter in zip(given, declared):
            if parameter.get("pattern"):
                assert re.fullmatch(parameter["pattern"], value), (line, parameter["name"])

    backup = _backup("ACME_APPDB_FULL")["telegram"]
    assert backup["line"] == "/spbot_backup ACME_APPDB_FULL -" and backup["level"] == 10
    fits(backup["line"])
    restore = _restore("ACME_STORE_TO_LAB")["telegram"]
    assert restore["restore"]["line"] == "/spbot_restore ACME_STORE_TO_LAB LATEST"
    assert restore["copy_only"]["line"] == "/spbot_copy_backup ACME_STORE_TO_LAB LATEST"
    for block in restore.values():
        fits(block["line"])
        assert block["runs"].startswith("restore-workflow --restore-id ACME_STORE_TO_LAB")


def test_the_bot_command_is_found_by_what_it_runs_not_by_its_name():
    """A node may rename a command or switch it off; the level that decides is that command's."""
    renamed = json.loads(json.dumps(SHIPPED_COMMANDS))
    for record in renamed["telegram_support_commands"]:
        if record["command_text"] == "spbot_backup":
            record.update({"command_text": "spbot_take_backup", "command_type": 50})
    twin = R.bot_twins(renamed)[R.GATE_BACKUP]
    assert twin["command"] == "/spbot_take_backup" and twin["level"] == 50
    assert R.bot_twins({"telegram_support_commands": []}) == {}
    assert R.bot_twins(None) == {}


# --------------------------------------------------------------------------- #
# Built from the entry, never a generic guess
# --------------------------------------------------------------------------- #
def test_each_kind_of_restore_entry_is_given_only_the_commands_that_run_it():
    def commands(restore_id: str) -> set[str]:
        return {_parsed(step["cli"], WORKER).command for step in _restore(restore_id)["steps"] if "cli" in step}

    assert commands("ACME_STORE_TO_LAB") == {"restore-by-id", "restore-workflow"}
    assert commands("ACME_APPDB_TO_DRILL") == {"restore-workflow", "restore-latest"}
    assert "restore-latest refuses it" in _restore("ACME_STORE_TO_LAB")["entry_kind"]
    assert "restore-by-id refuses it" in _restore("ACME_APPDB_TO_DRILL")["entry_kind"]
    # The rule that tells the two apart is the app's own, in one place.
    assert backup_config.is_script_restore is restore_kind.is_script_restore


def test_the_dry_run_comes_before_the_run_it_rehearses():
    for runbook in [_backup(item) for item in BACKUP_IDS] + [_restore(item) for item in RESTORE_IDS]:
        kinds = [step["kind"] for step in runbook["steps"]]
        assert kinds[0] == "dry_run", runbook.get("backup_id") or runbook.get("restore_id")
        assert kinds.index("dry_run") < kinds.index("run")
    assert "dry run first" in _build(backup_id="ACME_STORE_PG")["node"]["order"]


def test_a_backup_entry_with_two_jobs_is_told_that_the_bot_command_runs_both():
    runbook = _backup("ACME_STORE_PG")
    assert [job["job"] for job in runbook["jobs"]] == ["database_full", "database"]
    assert "EVERY active job of the entry (database_full, database)" in runbook["telegram"]["runs"]
    jobs = {_parsed(step["cli"], WORKER).job for step in runbook["steps"] if step["kind"] == "run"
            and _parsed(step["cli"], WORKER).command == "backup"}
    assert jobs == {"database_full", "database"}, "the CLI line names one job each"
    assert "EVERY" not in _backup("ACME_APPDB_FULL")["telegram"]["runs"], "one job: nothing to warn of"


def test_a_job_states_its_own_level_schedule_and_scope():
    full = _backup("ACME_STORE_PG")["jobs"][0]
    assert full["level"].startswith("full") and full["takes"] == "the whole cluster"
    assert "hours 01-05" in full["window"] and "on Sun" in full["window"]
    # A Wednesday afternoon: the day closes it before the hour does, and the day is what is named.
    assert full["open_now"] is False and "weekday" in full["closed_because"]
    named = _backup("ACME_APPDB_FULL")["jobs"][0]
    assert named["takes"] == "database(s) APPDB, FINDB"
    log = _backup("ACME_APPDB_LOG")["jobs"][0]
    assert "log stream" in log["level"] and log["open_now"] is True
    assert log["notify"]["each run"] == "not sent (the rule is off)"
    assert log["notify"]["an error"] == "the chat of level backup"


def test_a_level_is_offered_only_where_the_script_derives_it():
    """A job that states its level, or ships the log stream, has no level for a word to force."""
    for backup_id in ("ACME_STORE_PG", "ACME_APPDB_FULL", "ACME_APPDB_LOG"):
        assert all("level_words" not in step for step in _backup(backup_id)["steps"])
    derived = json.loads(json.dumps(ESTATE))
    derived["backup_restore"]["backups"][1]["jobs"][0].pop("env")
    runbook = R.build(derived, backup_id="ACME_APPDB_FULL", node=WORKER, twins=TWINS, environments={})["backups"][0]
    words = _step(runbook, "Run job full now")["level_words"]
    assert "--backup-type <diff|full|log>" in words
    assert backup_cli.parse_args(["backup", "--backup-type", "diff"]).backup_type == "diff"
    # list-backups reads the same names for the same reason.
    assert backup_cli.LOG_STREAM_JOBS is R.LOG_STREAM_JOBS


def test_the_flow_says_which_steps_this_entry_runs_and_how_its_server_is_read():
    store = _backup("ACME_STORE_PG")
    assert [step["step"] for step in store["flow"]] == [
        "Backup", "Copy to the repository", "Verify the copy", "Encrypt", "Verify the encryption",
        "Delete at the source", "Repository check"]
    assert all(step["state"] == "on" for step in store["flow"])
    assert f"{REPOSITORY}:/mnt/repo/{STORE}/" in store["flow"][1]["does"]
    assert "every file goes, the newest chain too" in store["flow"][5]["does"]
    assert store["repository"]["only_files_since"].startswith("2026-11-01T00:00:00Z")
    # A Windows server over the SSH it answers, and the same server over its share.
    assert _backup("ACME_APPDB_FULL")["repository"]["source_read"] == "over SSH, by SFTP (offload_transfer sftp)"
    share = _backup("ACME_APPDB_DIFF")
    assert share["repository"]["source_read"].startswith("over its share \\\\192.0.2.10\\D$\\SQLBK (SMB, as svc_backup")
    assert [step["state"] for step in share["flow"]][3:6] == [
        "off - offload_encrypt is false", "off - offload_encrypt is false", "off - offload_delete_source is false"]
    # An entry with no repository has no repository command to be given.
    log = _backup("ACME_APPDB_LOG")
    assert [step["state"] for step in log["flow"]] == ["on", "off - offload_enabled is false"]
    assert not any("offload" in step.get("cli", "") for step in log["steps"])


def test_a_forced_run_says_what_it_touches_and_that_nothing_asks():
    """A forced backup of a production server by day: where it writes, that its window is closed,
    what follows it - and that the command asks no confirmation."""
    step = _step(_backup("ACME_STORE_PG"), "Run job database_full now")
    assert step["changes"] is True
    for needed in (f"on {STORE} into /opt/pgbackup/dbops", "--force skips the job's schedule",
                   "the window is closed now", "is a production instance in the inventory",
                   "copies the new files to the repository, encrypts them and deletes them at the source"):
        assert needed in step["touches"], needed
    assert step["confirmation"].startswith("none")
    offload = _step(_backup("ACME_APPDB_FULL"), "Run the repository pass now")
    assert f"DELETES on {WINDOWS} every file whose copy is proven" in offload["touches"]
    assert "deletes nothing on the server" in _step(_backup("ACME_APPDB_DIFF"), "Run the repository pass now")["touches"]
    guards = " ".join(_backup("ACME_STORE_PG")["guards"])
    assert "never doubled" in guards and "lockdown" in guards


def test_an_inactive_job_is_said_not_to_run_before_its_line():
    runbook = _backup("ACME_OLD_ORA")
    assert runbook["active"] is False and runbook["jobs"][0]["active"] is False
    assert "leaves it out" in _step(runbook, "Run job database now")["will_not_run"]
    assert "answers due 0" in _step(runbook, "Dry run of job database")["does"]


def test_a_restore_says_where_it_reads_what_it_replaces_and_what_can_refuse_it():
    server = _restore("ACME_STORE_TO_LAB")
    assert server["reads"] == f"the backup folder of {STORE}" and server["restore_from"] == "server"
    run = _step(server, "Restore to the newest backup")
    assert f"RESTORES the whole cluster onto {LAB} (container PG_LAB)" in run["touches"]
    assert "REPLACED by the restored copy" in run["touches"] and "/opt/restore/acme_store" in run["touches"]
    assert any("takes the entry's run claim" in guard for guard in server["guards"])
    repository = _restore("ACME_STORE_FROM_REPO")
    assert repository["restore_from"] == "repository" and "repository's proven copies" in repository["reads"]
    assert any("only the copies an offload proved" in guard for guard in repository["guards"])
    share = _restore("ACME_APPDB_TO_DRILL")
    assert share["reads"] == "the share \\\\192.0.2.10\\SQLBK\\APPDB" and share["databases"] == "APPDB -> APPDB_DRILL"
    assert "REPLACED (overwrite_existing is true)" in _step(share, "Restore to the newest backup")["touches"]
    assert any("takes no run claim" in guard for guard in share["guards"])


def test_a_restore_aimed_at_production_is_said_to_be_refused():
    runbook = _restore("ACME_APPDB_ONTO_PROD")
    assert runbook["target_environment"] == "prod"
    refused = _step(runbook, "Restore to the newest backup")["will_be_refused"]
    assert "production instance" in refused and "allow_production_target" in refused
    assert "will_be_refused" not in _step(_restore("ACME_STORE_TO_LAB"), "Restore to the newest backup")


def test_an_in_place_restore_copies_nothing_and_is_given_no_copy_only_line():
    runbook = _restore("ACME_ORA_IN_PLACE")
    assert runbook["in_place"] is True and "nothing is copied" in runbook["flow"][0]["does"]
    assert all(step["step"] != "Copy only - nothing restored" for step in runbook["steps"])
    assert "copy_only" not in runbook["telegram"]
    assert "the schedule never runs it" in _step(runbook, "Restore to the newest backup")["inactive"]


def test_an_answer_says_where_to_watch_the_run():
    watch = " ".join(_backup("ACME_APPDB_FULL")["watch"])
    for needed in ("ACME_APPDB_FULL · 1. Backup", "2-5. Repository", "the chat of level backup", "job_runs",
                   "backup_files", "http://192.0.2.115:8080/report_dba/backup-restore.html"):
        assert needed in watch, needed
    restore = " ".join(_restore("ACME_STORE_TO_LAB")["watch"])
    assert "restore_id=ACME_STORE_TO_LAB" in restore and "restore_runs" in restore
    unpublished = R.build(ESTATE, backup_id="ACME_APPDB_LOG", node=WORKER, twins=TWINS, environments={})
    assert "no published address is configured" in " ".join(unpublished["backups"][0]["watch"])


# --------------------------------------------------------------------------- #
# Written for the node it is typed on
# --------------------------------------------------------------------------- #
def test_a_line_takes_the_form_of_the_node_it_is_typed_on():
    name = "Dry run of job full"
    worker = _step(_backup("ACME_APPDB_FULL", WORKER), name)
    assert worker["cli"] == ("dbabrain backup-restore backup --config config.json --backup-id ACME_APPDB_FULL "
                             "--job full --force --dry-run")
    assert worker["from_master"] == (
        "python -m dbabrain.control.cli worker-run -- python -m dbabrain.backup_restore.cli backup "
        "--config config.json --backup-id ACME_APPDB_FULL --job full --force --dry-run")
    master = _step(_backup("ACME_APPDB_FULL", MASTER), name)
    assert master["cli"].startswith(r"D:\dbabrain\.venv\Scripts\python.exe -m dbabrain.backup_restore.cli backup")
    assert master["from_master"].startswith(
        r"D:\dbabrain\.venv\Scripts\python.exe -m dbabrain.control.cli worker-run -- python -m ")
    assert "from_master" not in _step(_backup("ACME_APPDB_FULL", SINGLE), name), "no worker to reach"


def test_the_answer_says_how_to_read_its_lines_on_this_node():
    worker = _build(WORKER, backup_id="ACME_APPDB_FULL")["node"]
    assert "docker exec <container>" in worker["cli"], "a container says how its host reaches it"
    assert "the master's own" in worker["from_master"], "a worker does not know the master's Python"
    master = _build(MASTER, backup_id="ACME_APPDB_FULL")["node"]
    assert "use the from_master line" in master["which_to_use"]
    for node in (worker, master):
        assert "DBABRAIN_SECRET_KEY" in node["passphrase"] and "Never write the passphrase" in node["passphrase"]
        assert "the master's DBABRAIN_SECRET_KEY" in node["from_master"]
    unknown = R.node_block(R.node_form({}, node_role="", has_worker=False))
    assert "was not read" in unknown["cli"]


def test_no_line_carries_the_passphrase_or_a_placeholder_for_it(tmp_path):
    """Every tool result passes through the secret redactor, which takes whatever follows a key
    flag for a key. A placeholder there came out as `--key-base64 *** passphrase>"` - a line that
    could not be typed. So no line names a key: each reads `DBABRAIN_SECRET_KEY`, and what the
    model is given is, character for character, what was built."""
    context = _context(tmp_path, node_role="master")
    (tmp_path / "config.json").write_text(json.dumps({"worker": [{"host": "192.0.2.115"}]}), encoding="utf-8")
    for asked in ([{"backup_id": item} for item in BACKUP_IDS] + [{"restore_id": item} for item in RESTORE_IDS]):
        output = T.run_tool(context, "backup_runbook", asked)
        assert "***" not in output.content and "--key" not in output.content, asked
        given = json.loads(output.content)
        node = R.node_form(context.environment, node_role="master", has_worker=True)
        built = R.for_asker(R.build(ESTATE, node=node, twins=TWINS, environments=ENVIRONMENTS, now=NOW,
                                    report_base="http://192.0.2.115:8080/report_dba/", **asked), asker_level=100,
                            may_run=lambda level: level <= 100 and level >= 0)
        assert given == built, "the redactor changed nothing: there was nothing secret-shaped to change"


def test_inline_json_is_written_for_bash_only():
    """``restore-by-id`` takes its request as one JSON argument, which PowerShell does not pass
    through whole: there the line is not written, and the reader is sent to the one that needs none."""
    bash = _step(_restore("ACME_STORE_TO_LAB", WORKER), "Plan the restore (dry run)")
    assert bash["cli"].endswith("""restore-by-id --config config.json '{"restore_id": "ACME_STORE_TO_LAB", "dry_run": true}'""")
    assert "from_master_not_written" in bash, "the master's shell is not this node's to know"
    windows = _step(_restore("ACME_STORE_TO_LAB", MASTER), "Plan the restore (dry run)")
    assert "cli" not in windows and "restore-workflow" in windows["cli_not_written"]
    assert "cli" in _step(_restore("ACME_STORE_TO_LAB", MASTER), "Dry run of the workflow")


def test_a_path_with_a_space_is_started_as_powershell_starts_one():
    line = R.render([r"C:\Program Files\dbabrain\python.exe", "-m", R.BACKUP_RESTORE_MODULE, "list-backups"],
                    "powershell-5.1")
    assert line == r'& "C:\Program Files\dbabrain\python.exe" -m dbabrain.backup_restore.cli list-backups'


# --------------------------------------------------------------------------- #
# Finding the entry
# --------------------------------------------------------------------------- #
def test_with_no_id_every_entry_is_listed_so_the_next_call_can_name_one():
    answer = _build()
    assert [item["backup_id"] for item in answer["backups"]] == BACKUP_IDS
    store = answer["backups"][0]
    assert store["server_id"] == STORE and store["jobs"] == ["database_full (full)", "database (incr)"]
    assert [item["restore_id"] for item in answer["restores"]] == RESTORE_IDS
    assert "steps" not in json.dumps(answer), "ids only: no line is given for an entry nobody named"


def test_a_server_id_gives_every_entry_of_that_server():
    answer = _build(server_id=WINDOWS)
    assert [item["backup_id"] for item in answer["backups"]] == ["ACME_APPDB_FULL", "ACME_APPDB_DIFF", "ACME_APPDB_LOG"]
    assert [item["restore_id"] for item in answer["restores"]] == ["ACME_APPDB_TO_DRILL", "ACME_APPDB_ONTO_PROD"]
    onto = _build(server_id=LAB)
    assert onto["backups"] == [] and len(onto["restores"]) == 3, "a restore is found by its target too"


def test_a_name_nothing_carries_is_answered_with_the_names_that_are_close():
    answer = _build(backup_id="ACME_APPDB_FUL")
    assert answer["backups"] == [] and "ACME_APPDB_FULL" in answer["not_found"]["backup_id"]["close"]
    assert _build(backup_id="acme_appdb_full")["backups"][0]["backup_id"] == "ACME_APPDB_FULL"
    assert "no backup or restore entry names" in _build(server_id="ACME-192-0-2-99")["not_found"]["server_id"]["note"]
    assert R.build({}, node=WORKER, twins={}, environments={}) == {
        "asked": "nothing named", "backups": [], "restores": [],
        "next": "name one backup_id, one restore_id, or a server_id for all of its entries."}


# --------------------------------------------------------------------------- #
# The tool the model calls
# --------------------------------------------------------------------------- #
def _context(tmp_path: Path, *, level: int = 100, operator: bool = False, node_role: str = "worker",
             **settings) -> T.ToolContext:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "restore_config.json").write_text(json.dumps(ESTATE), encoding="utf-8")
    (data / "telegram_support_commands.json").write_text(json.dumps(SHIPPED_COMMANDS), encoding="utf-8")
    (data / "telegram_users.json").write_text(json.dumps({"telegram_users": [
        {"user_id": "1", "user_type": level, "active": True}]}), encoding="utf-8")
    (data / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": server_id, "environment": environment} for server_id, environment in ENVIRONMENTS.items()]}),
        encoding="utf-8")
    (data / "reports_config.json").write_text(json.dumps({"report_base_url": "http://192.0.2.115:8080/report_dba"}),
                                              encoding="utf-8")
    parsed = A.parse_ai_assist({**A.shipped_document(), **settings})
    return T.ToolContext(
        settings=parsed, store_target=StoreTarget.coerce(str(tmp_path / "store.sqlite")), tool_root=tmp_path,
        commands_path=data / "telegram_support_commands.json", users_path=data / "telegram_users.json",
        asker_user_id="1", data_dir=data, node_role=node_role, now_local=NOW, asker_is_operator=operator,
        environment={"install": "pip", "shell": "bash", "runtime": "docker", "python": "/usr/local/bin/python"},
        redact=T.Redactor({"STORE_REF": "stored-value-kept-out-41"}))


def test_the_tool_reads_this_nodes_files_and_answers_the_runbook(tmp_path):
    context = _context(tmp_path)
    assert "backup_runbook" in [tool["name"] for tool in T.tool_definitions(context.settings)]
    output = T.run_tool(context, "backup_runbook", {"backup_id": "ACME_STORE_PG"})
    assert not output.is_error and output.row_count == 1
    answer = json.loads(output.content)
    runbook = answer["backups"][0]
    assert runbook["server_environment"] == "production", "the inventory's word for the server"
    assert answer["asker"]["telegram_level"] == 100
    assert _step(runbook, "Run job database now")["cli"].startswith("dbabrain backup-restore backup")
    assert "http://192.0.2.115:8080/report_dba/backup-restore.html" in " ".join(runbook["watch"])
    index = json.loads(T.run_tool(context, "backup_runbook", {}).content)
    assert len(index["backups"]) == 5 and len(index["restores"]) == 5


def test_the_tool_is_switched_off_by_its_own_field(tmp_path):
    context = _context(tmp_path, backup_runbook_enabled=False)
    assert "backup_runbook" not in [tool["name"] for tool in T.tool_definitions(context.settings)]
    assert T.run_tool(context, "backup_runbook", {"backup_id": "ACME_STORE_PG"}).is_error


def test_a_node_with_nothing_configured_answers_that_and_does_not_fail(tmp_path):
    context = _context(tmp_path)
    (tmp_path / "data" / "restore_config.json").unlink()
    (tmp_path / "data" / "telegram_support_commands.json").unlink()
    answer = json.loads(T.run_tool(context, "backup_runbook", {}).content)
    assert answer["backups"] == [] and answer["restores"] == []
    missing = json.loads(T.run_tool(context, "backup_runbook", {"backup_id": "ACME_STORE_PG"}).content)
    assert missing["backups"] == [] and "backup_id" in missing["not_found"]


def test_a_server_with_more_entries_than_one_answer_holds_is_listed_by_id(tmp_path):
    """Cut in the middle, a runbook is a line without its warning."""
    context = _context(tmp_path, tool_result_max_chars=4000)
    answer = json.loads(T.run_tool(context, "backup_runbook", {"server_id": WINDOWS}).content)
    assert [item["backup_id"] for item in answer["backups"]] == ["ACME_APPDB_FULL", "ACME_APPDB_DIFF", "ACME_APPDB_LOG"]
    assert "steps" not in json.dumps(answer) and "name one backup_id or restore_id" in answer["next"]
    # One entry is given whole whatever its size: there is nothing shorter to say.
    assert "steps" in T.run_tool(context, "backup_runbook", {"backup_id": "ACME_APPDB_FULL"}).content


def test_the_master_is_told_the_worker_form_when_its_config_names_a_worker(tmp_path):
    context = _context(tmp_path, node_role="master")
    single = json.loads(T.run_tool(context, "backup_runbook", {"backup_id": "ACME_APPDB_LOG"}).content)
    assert "from_master" not in single["node"], "no worker in config.json: one machine does everything"
    (tmp_path / "config.json").write_text(json.dumps({"worker": [{"host": "192.0.2.115", "user": "dba_user"}]}),
                                          encoding="utf-8")
    master = json.loads(T.run_tool(context, "backup_runbook", {"backup_id": "ACME_APPDB_LOG"}).content)
    assert "from_master" in master["node"] and "from_master" in master["backups"][0]["steps"][0]


def test_the_guide_tells_the_model_to_answer_from_the_runbook():
    from dbabrain.telegram import ai_assist as job

    guide = job.GUIDE_PATH.read_text(encoding="utf-8")
    section = guide[guide.index("## Running a backup or a restore by hand"):guide.index("## A new install")]
    for needed in ("call `backup_runbook`", "a line written from memory is a guess", "the dry run, then",
                   "backup, copy to the repository,", "verify, encrypt, verify, delete at the source",
                   "Copy a line character for character", "Never write the", "where to watch it"):
        assert needed in section, needed
