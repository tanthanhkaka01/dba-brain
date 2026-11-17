"""Every field that decides what a backup or restore run does is stated on its record (0.39.0, 1.366).

The operator, 2026-11-14 (translated): *time_window and active must be required; backup and restore
are very important in production, so nearly every field must be required, and there is no fallback
anywhere.* Until then a backup job with no ``timeout``, a restore entry with no ``notify``, a
share-driven target with no ``sql_instance`` loaded and ran on a value in the code - 300 seconds,
both rules on at the event's severity, ``localhost`` - and nothing in the file said so. A reader of
the file could not tell a decision from an omission, and an omission on a restore decides which
instance is written over.

So, for each of the four kinds of record - a script-driven restore entry, a share-driven one, a
backup entry, a backup job - ``lib.restore.stated`` lists the fields it states. A record that leaves
one out still **loads**: one such record must not stop every other of its file. It is refused
**inside its own run**, by name, saying what the field decides. ``upgrade-config`` writes into it the
value it had been running on - so an upgraded node runs exactly as before - and ``restore-add`` and
``backup-add`` write them into every new record. What these tests hold:

* each kind's fields, and what an unstated one ran on - the window's three intervals as the due rule
  read them, the notify rules as the parser resolved them (a job's through its entry's);
* the record loads, says which it left out, and its run refuses it - the restore before anything is
  copied, the backup before anything reaches the host, the rest of the pass running;
* the upgrade writes what the record ran on, keeps what it states, and a second run writes nothing;
  the due verdicts of an upgraded record are the ones it had;
* the registrars write every field; the reference calls them required; the shipped example states
  them; and the few fields that stay optional are optional for a reason the module says.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import backup as backup_module

from dbabrain.backup_restore import workflow as workflow_module
from dbabrain.backup_restore import config as restore_config
from dbabrain.backup_restore import restore_by_id, schedule, workflow
from dbabrain.backup_restore.restore_script import load_script_restores
from dbabrain.lib.restore import stated as S
from dbabrain.lib.time_window import parse_time_window_config

from conftest import stated, stated_document

REPO = Path(__file__).resolve().parents[1]
WINDOW = {"from_hour": 2, "to_hour": 5, "repeat_interval": 72000, "retry_interval": 600, "timeout": 7200}
NOTIFY = {"logging_on_run": {"enabled": True, "telegram_chat": "", "chat_id": ""},
          "alert_on_error": {"enabled": True, "telegram_chat": "", "chat_id": ""}}


# --------------------------------------------------------------------------- #
# The four tables
# --------------------------------------------------------------------------- #
def test_each_kind_of_record_states_its_switch_its_window_and_its_notify_rules():
    window = ("time_window.repeat_interval", "time_window.retry_interval", "time_window.timeout")
    rules = ("notify.logging_on_run.enabled", "notify.logging_on_run.telegram_chat",
             "notify.alert_on_error.enabled", "notify.alert_on_error.telegram_chat")
    for kind in (S.SCRIPT_RESTORE, S.SHARE_RESTORE, S.BACKUP_JOB):
        names = {field.name for field in S.FIELDS[kind]}
        assert {"active", *window, *rules} <= names, kind
    assert {field.name for field in S.BACKUP_ENTRY_FIELDS} == {
        "active", "env_secrets", "scrub_files_per_run", "server_metadata.enabled",
        # Where the offload carries the metadata bundle (0.40.0, 1.376).
        "server_metadata.repository_path", "record_chain", "oracle_sid"}


def test_every_field_says_what_it_decides_and_one_name_means_one_thing():
    seen: dict[str, str] = {}
    for kind, table in S.FIELDS.items():
        names = [field.name for field in table]
        assert len(names) == len(set(names)), kind
        for field in table:
            assert field.decides.strip(), (kind, field.name)
            assert seen.setdefault(field.name, field.decides) == field.decides, field.name


def test_the_notify_rules_resolve_as_the_parser_resolves_them():
    """lib.restore.stated reads a rule without lib.notify - which checks a level against the node's
    config, and upgrade-config works from its request alone (R09) - so it is held to the parser here."""
    from dbabrain.lib.notify import NOTIFY_RULE_NAMES, parse_notify_config

    assert S.NOTIFY_RULES == NOTIFY_RULE_NAMES
    defaults = restore_config.BACKUP_RESTORE_NOTIFY_DEFAULTS
    assert all(dataclasses.asdict(getattr(defaults, rule)) == S.NOTIFY_RAN_ON for rule in S.NOTIFY_RULES)
    entry = {"notify": {"logging_on_run": {"enabled": False, "telegram_chat": "Backup", "chat_id": "-1"},
                        "alert_on_error": True}}
    for record in ({}, {"notify": {}}, {"notify": {"logging_on_run": False}}, {"logging_on_run": False},
                   {"notify": {"alert_on_error": {"telegram_chat": "backup"}}},
                   {"notify": {"logging_on_run": {"enabled": True, "telegram_chat": None}}}):
        for parent in (None, entry):
            inherit = parse_notify_config(parent, defaults=defaults) if parent else None
            parsed = parse_notify_config(record, defaults=defaults, inherit=inherit)
            for rule in S.NOTIFY_RULES:
                for part in ("enabled", "telegram_chat"):
                    kind = S.BACKUP_JOB if parent else S.SCRIPT_RESTORE
                    assert S.ran_on(f"notify.{rule}.{part}", {"script": "s", **record}, kind=kind, parent=parent)                         == getattr(getattr(parsed, rule), part), (record, parent, rule, part)


def test_what_a_record_that_said_nothing_ran_on():
    # The due rule's own fallbacks for backup_restore: 300 s to repeat (schedule.DEFAULT_REPEAT_SECONDS),
    # the repeat interval to retry - 60 s with none - and no timeout, which nothing reaps or bounds.
    assert schedule.DEFAULT_REPEAT_SECONDS == S.ran_on("time_window.repeat_interval", {}, kind=S.BACKUP_JOB) == 300
    assert S.ran_on("time_window.retry_interval", {}, kind=S.BACKUP_JOB) == 60
    assert S.ran_on("time_window.retry_interval", {"time_window": {"repeat_interval": 900}}, kind=S.BACKUP_JOB) == 900
    assert S.ran_on("time_window.retry_interval", {"interval_seconds": 120}, kind=S.BACKUP_JOB) == 120, "legacy spelling"
    assert S.ran_on("time_window.retry_interval", {"time_window": {"repeat_interval": 0}}, kind=S.BACKUP_JOB) == 0
    assert S.ran_on("time_window.timeout", {}, kind=S.BACKUP_JOB) == 3600, "the bound its script ran under"
    # The share-driven parser's own table, which the entry loads on.
    share = {name: S.ran_on(name, {}, kind=S.SHARE_RESTORE) for name in (
        "target.sql_instance", "target.restore_data_dir", "target.vm_platform", "copy_file_patterns",
        "copy_recent_hours", "full_backup_subdir", "execution_mode", "target.sql_container")}
    assert share == {"target.sql_instance": restore_config.RESTORE_PARSER_DEFAULTS["restore_sql_instance_on_vm"],
                     "target.restore_data_dir": restore_config.RESTORE_PARSER_DEFAULTS["restore_data_dir_on_vm"],
                     "target.vm_platform": "windows", "copy_file_patterns": ["*.bak", "*.trn"],
                     "copy_recent_hours": 24, "full_backup_subdir": "FULL", "execution_mode": "sync",
                     "target.sql_container": ""}
    assert S.ran_on("copy_file_patterns", {}, kind=S.SHARE_RESTORE) is not \
        S.ran_on("copy_file_patterns", {}, kind=S.SHARE_RESTORE), "each record gets an object of its own"


def test_a_field_applies_only_where_its_loader_reads_it():
    in_place = {"script": "s", "db_type": "postgresql", "server_id": "A", "target_server_id": "A"}
    remote = {"script": "s", "db_type": "sqlserver", "server_id": "A", "target_server_id": "B",
              "target_backup_dir": "/opt/db_ops/staging/x"}
    assert not {"copy_mode", "staging_marker", "copy_recent_hours"} & set(S.unstated(in_place))
    # Every engine states whether its metadata is restored since 0.40.0 (1.376) - PostgreSQL and Oracle only false.
    assert "server_metadata.enabled" in set(S.unstated(in_place))
    assert {"copy_mode", "staging_marker", "target_visible_dir", "server_metadata.enabled"} <= set(S.unstated(remote))
    assert "copy_recent_hours" in S.unstated({**remote, "copy_selection": "window"}), "a window copy reads its hours"
    assert "server_metadata.enabled" not in S.unstated({"backup_id": "PG", "db_type": "postgresql"}, kind=S.BACKUP_ENTRY)
    assert "source.certificate_api_verify_tls" in S.unstated({"source": {"certificate_api_url": "https://vault"}})
    assert "source.certificate_api_verify_tls" not in S.unstated({"source": {}})


def test_a_stated_null_or_empty_is_an_answer_only_where_it_means_one():
    entry = stated({"script": "s", "db_type": "postgresql", "server_id": "A", "target_server_id": "A"})
    assert S.unstated(entry) == ()
    assert S.unstated({**entry, "tier": None}) == (), "null is a drill nobody tiered"
    assert S.unstated({**entry, "target_container": ""}) == (), "\"\" is the engine on the host itself"
    assert "checkdb_timeout_seconds" in S.unstated({**entry, "checkdb_timeout_seconds": None})
    timeout_null = copy.deepcopy(entry)
    timeout_null["time_window"]["timeout"] = None
    assert S.unstated(timeout_null) == ("time_window.timeout",), "null there is the caller's default"


# --------------------------------------------------------------------------- #
# A script-driven restore entry: loads, says which, refused inside its run
# --------------------------------------------------------------------------- #
def _script_entry(**over) -> dict:
    entry = {"restore_id": "PG_TO_LAB", "db_type": "postgresql", "server_id": "SRC", "target_server_id": "TGT",
             "backup_dir": "/out", "script": "postgresql", "target_backup_dir": "/opt/db_ops/staging/pg",
             "source_backup_host_dir": "/opt/out", "cleanup_retention": 86400, "time_window": dict(WINDOW),
             "overwrite_existing": False, "action": "restore", "restore_from": "server"}
    entry.update(over)
    return stated(entry)


def _load_script(tmp_path, entry: dict):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": [entry]}}), encoding="utf-8")
    return load_script_restores(path)[0]


def test_a_script_driven_entry_that_leaves_fields_out_loads_on_what_it_ran_on_and_says_which(tmp_path):
    entry = _script_entry()
    for name in ("active", "notify", "env", "copy_mode"):
        entry.pop(name)
    del entry["time_window"]["timeout"]

    job = _load_script(tmp_path, entry)

    assert job.unstated == ("active", "time_window.timeout", "notify.logging_on_run.enabled",
                            "notify.logging_on_run.telegram_chat", "notify.alert_on_error.enabled",
                            "notify.alert_on_error.telegram_chat", "env", "copy_mode")
    assert job.active is True and job.time_window.timeout is None and job.copy_mode == "auto" and job.env == {}


def test_the_workflow_refuses_it_inside_that_entrys_run_naming_what_each_decides(tmp_path):
    entry = _script_entry()
    entry.pop("active")
    del entry["time_window"]["repeat_interval"]

    action, refused = workflow._entry_action(_load_script(tmp_path, entry), copy_only=False)

    assert action is None
    assert "states no active, time_window.repeat_interval" in str(refused)
    assert "active - whether the scheduler runs it at all" in str(refused)
    assert "how long after a run's start the next one is due" in str(refused)
    assert "upgrade-config" in str(refused)
    assert workflow._entry_action(_load_script(tmp_path, _script_entry()), copy_only=False) == ("restore", None)


def test_restore_by_id_refuses_it_before_anything_is_copied(monkeypatch, tmp_path):
    from dbabrain.backup_restore import restore_script

    entry = _script_entry()
    del entry["notify"]["alert_on_error"]
    job = _load_script(tmp_path, entry)
    monkeypatch.setattr(restore_script, "load_script_restores", lambda _path=None: [job])
    monkeypatch.setattr(restore_script, "transfer_backup_to_target",
                        lambda *a, **k: pytest.fail("copied for an entry whose run a default would decide"))

    with pytest.raises(restore_by_id.RestoreByIdError, match="states no notify.alert_on_error.enabled"):
        restore_by_id.restore_by_id({"restore_id": "PG_TO_LAB"})


# --------------------------------------------------------------------------- #
# A share-driven restore entry: the block above it states for it, the parser's table never does
# --------------------------------------------------------------------------- #
def _share_entry(**over) -> dict:
    from tests.test_registering_a_backup_a_restore_and_an_os_login import _restore

    entry = _restore(**over)
    entry.update({"server_id": entry["source"].pop("id"), "target_server_id": entry["target"].pop("id"),
                  "overwrite_existing": False, "action": "restore", "restore_from": "server",
                  "integrity": {"copy_max_retry": 3}, "notify": copy.deepcopy(NOTIFY)})
    return entry


def _load_share(tmp_path, entry: dict, **block):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {**block, "restores": [entry]}}), encoding="utf-8")
    return restore_config.load_restore_configs(path)[0]


def test_a_share_driven_entry_is_read_as_written_not_with_the_parsers_table_under_it(tmp_path):
    config = _load_share(tmp_path, _share_entry())

    # It loads on the table - the instance, the timeouts - and says it stated none of it.
    assert config.restore_sql_instance_on_vm == "localhost,1433" and config.sql_login_timeout_seconds == 60
    assert {"target.sql_login_timeout_seconds", "copy_file_patterns", "copy_recent_hours", "full_backup_subdir",
            "execution_mode", "checkdb_timeout_seconds", "tier"} <= set(config.unstated)
    assert "target.sql_instance" not in config.unstated, "the target states it"


def test_what_the_block_above_states_is_stated_for_every_entry_under_it(tmp_path):
    entry = stated(_share_entry(), above={})
    for name in ("copy_file_patterns", "copy_recent_hours", "full_backup_subdir"):
        entry.pop(name)

    assert _load_share(tmp_path, entry).unstated == ("copy_file_patterns", "copy_recent_hours", "full_backup_subdir")
    assert _load_share(tmp_path, entry, copy_file_patterns=["*.bak"], copy_recent_hours=192,
                       full_backup_subdir="FULL").unstated == ()


def test_a_flat_or_legacy_spelling_states_it_too(tmp_path):
    entry = stated(_share_entry(), above={})
    del entry["target"]["sql_instance"]
    entry["restore_sql_instance_on_vm"] = "10.0.0.5,1433"

    config = _load_share(tmp_path, entry)

    assert config.unstated == () and config.restore_sql_instance_on_vm == "10.0.0.5,1433"


def test_the_workflow_refuses_a_share_driven_entry_by_name(tmp_path):
    action, refused = workflow._entry_action(_load_share(tmp_path, _share_entry()), copy_only=False)

    assert action is None and "states no " in str(refused)
    assert "target.sql_login_timeout_seconds - how long sqlcmd waits to log in" in str(refused)


# --------------------------------------------------------------------------- #
# A backup entry and its jobs: loads, says which, refused inside the job's own run
# --------------------------------------------------------------------------- #
def _backup_document(**job_over) -> dict:
    job = {"job": "database", "script": "assets/backup/postgresql/pg_basebackup_database.sh",
           "databases": [], "all_databases": True, "time_window": dict(WINDOW),
           "notify": copy.deepcopy(NOTIFY), "active": True, "env": {}}
    job.update(job_over)
    entry = {"backup_id": "PG_BASE", "db_type": "postgresql", "server_id": "PG-HOST", "backup_dir": "/backup/pg",
             "cleanup_retention": 691200, "active": True, "env_secrets": {}, "scrub_files_per_run": 20,
             "jobs": [job]}
    return {"backup_restore": {"backups": [entry]}}


def _jobs(tmp_path, document: dict):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return backup_module.load_backup_jobs(path)


def test_a_backup_job_and_its_entry_say_what_they_left_out(tmp_path):
    document = _backup_document()
    job_record = document["backup_restore"]["backups"][0]["jobs"][0]
    del job_record["time_window"]["timeout"], job_record["env"]
    del document["backup_restore"]["backups"][0]["scrub_files_per_run"]

    [job] = _jobs(tmp_path, document)

    assert job.unstated == ("time_window.timeout", "env")
    assert job.entry_unstated == ("scrub_files_per_run",)
    assert job.time_window.timeout is None and job.scrub_files_per_run == 20, "it loads on what it ran on"
    assert _jobs(tmp_path, _backup_document())[0].unstated == ()


def test_the_backup_run_refuses_that_job_before_anything_reaches_the_host_and_runs_the_rest(monkeypatch):
    from tests.test_an_async_run_never_repeats_a_job_another_run_finished import _backup_job, _Store

    silent = dataclasses.replace(_backup_job("SILENT"), unstated=("time_window.timeout",))
    whole_entry = dataclasses.replace(_backup_job("ENTRY"), entry_unstated=("active",))
    fine = _backup_job("FINE")
    executed: list[str] = []
    said: list[str] = []
    _Store.rows = []
    monkeypatch.setattr(backup_module, "DbaBrainStore", _Store)
    _emit = lambda **k: said.append(str(k.get("message")))
    monkeypatch.setattr(backup_module, "emit_backup_restore_event", _emit)
    monkeypatch.setattr(workflow_module, "emit_backup_restore_event", _emit)
    monkeypatch.setattr(backup_module.schedule, "reap_stale_runs", lambda **k: [])
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [silent, whole_entry, fine])
    monkeypatch.setattr(backup_module, "export_for_backup", lambda *a, **k: None)
    monkeypatch.setattr(backup_module, "_open_catalog", lambda _config: SimpleNamespace(
        record_backup=lambda **_k: 1, last_file_name=lambda **_k: ""))

    def resolve(item, **_k):
        executed.append(item.backup_id)

    def execute(item, **_k):
        executed.append(f"{item.backup_id} ran")
        return backup_module.BackupRunResult(job=item, status="done", exit_code=0, duration_ms=1,
                                             stdout="RESULT=ok", stderr="", error_text=None)

    monkeypatch.setattr(backup_module, "resolve_backup_target", resolve)
    monkeypatch.setattr(backup_module, "execute_backup_job", execute)
    summary = workflow_module.run_backup(app_config=SimpleNamespace(sqlite_path=":memory:"), config_path="x",
                                       force=True)

    assert executed[:2] == ["FINE", "FINE ran"], "neither refused job reached its host"
    assert not [step for step in executed if step.startswith(("SILENT", "ENTRY"))]
    assert summary["failed"] == 2 and summary["succeeded"] == 1
    # The reason is in the run's own message - the one backup.log and the alert carry.
    assert any("SILENT/full finished: error - backup job SILENT/full states no time_window.timeout" in line
               for line in said), said
    assert any("backup entry ENTRY states no active, so a default nobody can read in the entry would decide: "
               "active - whether the scheduler runs it at all" in line for line in said), said


def test_the_entrys_repository_copy_is_refused_with_it():
    """The entry's switch decides its offload pass as much as its backups."""
    from dbabrain.backup_restore import offload
    from tests.test_an_async_run_never_repeats_a_job_another_run_finished import _backup_job

    entry = offload._Entry(backup_id="PG", server_id="S", db_type="postgresql", backup_dir="/b",
                           offload=SimpleNamespace(scan="none", server_id="REPO", folder="/repo"),
                           jobs=(dataclasses.replace(_backup_job("PG"), entry_unstated=("active",)),))

    assert offload.refusal(entry).startswith("backup entry PG states no active")
    assert offload.refusal(dataclasses.replace(entry, jobs=(_backup_job("PG"),))) == ""


def test_a_dry_run_says_which_job_its_run_would_refuse(monkeypatch):
    from tests.test_an_async_run_never_repeats_a_job_another_run_finished import _backup_job, _Store

    silent = dataclasses.replace(_backup_job("SILENT"), unstated=("env",))
    _Store.rows = []
    monkeypatch.setattr(backup_module, "DbaBrainStore", _Store)
    monkeypatch.setattr(backup_module.schedule, "reap_stale_runs", lambda **k: [])
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [silent, _backup_job("FINE")])
    monkeypatch.setattr(backup_module, "_open_catalog", lambda _config: None)

    summary = workflow_module.run_backup(app_config=SimpleNamespace(sqlite_path=":memory:"), config_path="x",
                                       dry_run=True)

    assert [(job["job"], job["status"]) for job in summary["jobs"]] == [("SILENT/full", "refused"),
                                                                       ("FINE/full", "dry-run")]
    assert "states no env" in summary["jobs"][0]["error"]


# --------------------------------------------------------------------------- #
# upgrade-config: what each record had been running on, and nothing else
# --------------------------------------------------------------------------- #
def _silent_document() -> dict:
    return {"backup_restore": {
        "copy_recent_hours": 192,
        "time_window": {"from_hour": 1, "to_hour": 4},
        "backups": [{"backup_id": "MSSQL", "db_type": "sqlserver", "server_id": "S", "backup_dir": "/b",
                     "notify": {"logging_on_run": {"enabled": False, "telegram_chat": "backup", "chat_id": "-100"}},
                     "jobs": [{"job": "full", "time_window": {"repeat_interval": 86400}},
                              {"job": "log", "notify": {"logging_on_run": True}}]}],
        "restores": [{"restore_id": "SCRIPT", "script": "s", "db_type": "postgresql", "server_id": "A",
                      "target_server_id": "B", "target_backup_dir": "/opt/db_ops/staging/s"},
                     {"restore_id": "SHARE", "source": {}, "target": {"sql_instance": "10.0.0.5"}}]}}


def test_upgrade_config_writes_what_each_record_had_been_running_on():
    document = _silent_document()
    S.state_restore_fields(document)
    section = document["backup_restore"]
    (backup,), (script, share) = section["backups"], section["restores"]
    full, log = backup["jobs"]

    assert full["time_window"] == {"repeat_interval": 86400, "retry_interval": 86400, "timeout": 3600}
    assert log["time_window"] == {"repeat_interval": 300, "retry_interval": 60, "timeout": 3600}
    # A job's notify rule is its entry's where it stated none - the chat too; a bare word keeps the rest.
    assert full["notify"]["logging_on_run"] == {"enabled": False, "telegram_chat": "backup", "chat_id": "-100"}
    assert full["notify"]["alert_on_error"] == {"enabled": True, "telegram_chat": "", "chat_id": ""}
    assert log["notify"]["logging_on_run"] == {"enabled": True, "telegram_chat": "backup", "chat_id": "-100"}
    assert full["active"] is True and full["env"] == {}
    assert backup["active"] is True and backup["env_secrets"] == {} and backup["scrub_files_per_run"] == 20
    assert backup["server_metadata"] == {"enabled": False}
    assert script["target_visible_dir"] == "/opt/db_ops/staging/s" and script["copy_mode"] == "auto"
    assert script["target_container"] == "" and script["tier"] is None and script["checkdb_timeout_seconds"] == 3600
    # The block states the hours and a window for the share-driven entry: neither is written on it,
    # and the window it reads is completed from the block's, not replaced by three numbers.
    assert "copy_recent_hours" not in share
    assert share["time_window"] == {"from_hour": 1, "to_hour": 4, "repeat_interval": 300, "retry_interval": 60,
                                    "timeout": 0}
    assert share["target"]["sql_instance"] == "10.0.0.5" and share["target"]["sql_login_timeout_seconds"] == 60


def test_after_the_upgrade_every_record_states_every_field_and_a_second_run_writes_nothing():
    document = _silent_document()
    changes = S.state_restore_fields(document)
    section = document["backup_restore"]
    above = S.section_above(section)

    assert changes and all("had been running on" in change["value"] for change in changes)
    assert {change["where"] for change in changes} == {
        "backup_restore.backups[MSSQL]", "backup_restore.backups[MSSQL].jobs[full]",
        "backup_restore.backups[MSSQL].jobs[log]", "backup_restore.restores[SCRIPT]", "backup_restore.restores[SHARE]"}
    for entry in section["restores"]:
        assert S.unstated(entry, above=above) == (), entry["restore_id"]
    for entry in section["backups"]:
        assert S.unstated(entry, kind=S.BACKUP_ENTRY) == ()
        for job in entry["jobs"]:
            assert S.unstated(job, kind=S.BACKUP_JOB) == (), job["job"]
    assert S.state_restore_fields(document) == [], "run again, it changes nothing"


def test_what_a_record_states_is_kept_whatever_it_says():
    document = stated_document(copy.deepcopy(_silent_document()))
    before = copy.deepcopy(document)
    document["backup_restore"]["restores"][0]["copy_mode"] = "sftp"
    document["backup_restore"]["backups"][0]["jobs"][0]["active"] = False
    expected = copy.deepcopy(document)

    assert S.state_restore_fields(document) == [] and document == expected and document != before


NOW = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)


def _row(status: str, seconds_ago: int) -> dict:
    return {"status": status, "started_at": (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")}


@pytest.mark.parametrize("record", [
    {},
    {"time_window": {"repeat_interval": 3600}},
    {"time_window": {"repeat_interval": 0}},
    {"time_window": {"retry_interval": 120}},
    {"time_window": {"repeat_interval": 900, "timeout": 600}},
    {"interval_seconds": 1800},
], ids=["nothing", "repeat-only", "run-once", "retry-only", "repeat-and-timeout", "legacy-interval"])
def test_an_upgraded_record_is_due_exactly_when_it_was(record):
    upgraded = copy.deepcopy(record)
    S.state_record(upgraded, kind=S.BACKUP_JOB)
    rows = [None] + [_row(status, ago) for status in ("DONE", "ERROR")
                     for ago in (30, 59, 61, 119, 121, 299, 301, 899, 901, 1799, 1801, 3599, 3601, 90000)]

    def verdicts(item):
        window = parse_time_window_config(item, context="t").time_window
        return [schedule.is_due(job_code="j", time_window=window, latest_runs={"j": row} if row else {},
                                now=NOW, local_now=NOW) for row in rows]

    assert verdicts(upgraded) == verdicts(record)


def test_a_record_with_no_timeout_is_written_what_it_ran_under_and_a_stated_zero_means_no_limit(tmp_path):
    """A restore entry with no timeout is written 0: the reaper skips None and 0 alike, and a restore
    script's SSH run is unbounded under both. A backup job is written 3600 - its script ran under the
    request builder's `timeout or 3600` until the operator removed that fallback (2026-11-14: *remove
    it*): a stated 0 is now no limit, as it says, and an unstated timeout is refused, never filled."""
    from dbabrain.backup_restore import spec_builder

    class Store:
        def fetch_running_job_runs(self, _prefix=""):
            return [{"job_code": "j", "status": "RUNNING", "started_at": "2026-01-01T00:00:00Z", "log_id": 1,
                     "metadata_json": "{}", "host_name": "elsewhere"}]

        def update_job_run(self, **_k):
            pytest.fail("a run with no timeout was reaped")

    for timeout in (None, 0):
        assert schedule.reap_stale_runs(store=Store(), timeouts={"j": timeout}) == []

    [job] = _jobs(tmp_path, _backup_document())
    target = SimpleNamespace(platform="linux", host="h", port=22, username="u", key_file="", password_ref="",
                             access="ssh", ssl=False, container_name="")

    def request(timeout):
        return spec_builder.backup_request_from_job(
            dataclasses.replace(job, time_window=dataclasses.replace(job.time_window, timeout=timeout)),
            target=target, secrets={})["timeout"]

    assert request(0) == 0 and request(7200) == 7200
    with pytest.raises(spec_builder.SpecBuildError, match="time_window.timeout is not stated"):
        request(None)
    assert S.ran_on("time_window.timeout", {}, kind=S.BACKUP_JOB) == 3600
    assert S.ran_on("time_window.timeout", {}, kind=S.SCRIPT_RESTORE) == 0


def test_backup_database_takes_its_timeout_from_the_request_alone_and_zero_is_no_limit():
    from dbabrain.common.backup.spec import parse_backup_spec
    from dbabrain.lib.backup_level import BackupSpecError

    base = {"db_type": "postgresql", "script": "echo ok", "host": {"runtime": "linux", "access": "local"}}
    assert parse_backup_spec({**base, "timeout": 0}).timeout is None, "0: no limit"
    assert parse_backup_spec({**base, "timeout": 900}).timeout == 900
    for missing in ({}, {"timeout": None}, {"timeout": ""}):
        with pytest.raises(BackupSpecError, match="timeout is required"):
            parse_backup_spec({**base, **missing})
    with pytest.raises(BackupSpecError, match="0 \\(no limit\\) or more"):
        parse_backup_spec({**base, "timeout": -1})


# --------------------------------------------------------------------------- #
# The registrars, the reference, the shipped example
# --------------------------------------------------------------------------- #
def test_restore_add_writes_every_field_into_a_new_entry_of_either_kind(tmp_path):
    from dbabrain.backup_restore import registration
    from tests.test_registering_a_backup_a_restore_and_an_os_login import KEY, _restore, _script_restore

    registration.add_restore(_restore(), data_dir=tmp_path, key=KEY)
    registration.add_restore(_script_restore(env={"PG_PORT": "5433"}), data_dir=tmp_path, key=KEY)
    document = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))
    share, script = document["backup_restore"]["restores"]

    assert S.unstated(share) == () and S.unstated(script) == ()
    assert share["target"]["sql_login_timeout_seconds"] == 60 and share["execution_mode"] == "sync"
    assert script["env"] == {"PG_PORT": "5433"}, "what it states is kept"
    assert script["time_window"]["timeout"] == 7200 and script["tier"] is None


def test_a_new_staging_folder_is_marked_with_this_versions_name(tmp_path):
    from dbabrain.backup_restore import registration
    from dbabrain.backup_restore.restore_script import DEFAULT_STAGING_MARKER
    from tests.test_registering_a_backup_a_restore_and_an_os_login import KEY, _script_restore

    registration.add_restore(_script_restore(target_server_id="ACME-192-0-2-40-HOST", target_visible_dir=None,
                                             target_backup_dir="/opt/db_ops/restore_staging/pg",
                                             source_backup_host_dir="/backup/pg"), data_dir=tmp_path, key=KEY)
    [written] = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))["backup_restore"]["restores"]

    assert written["staging_marker"] == DEFAULT_STAGING_MARKER
    assert written["target_visible_dir"] == "/opt/db_ops/restore_staging/pg" and written["copy_mode"] == "auto"


def test_backup_add_writes_every_field_and_a_jobs_notify_through_its_entry(tmp_path):
    from dbabrain.backup_restore import registration
    from tests.test_registering_a_backup_a_restore_and_an_os_login import KEY, NOTIFY as GIVEN, _backup

    request = _backup(notify=copy.deepcopy(GIVEN))
    del request["jobs"][0]["notify"], request["jobs"][0]["time_window"]["timeout"]
    registration.add_backup(request, data_dir=tmp_path, key=KEY)
    [entry] = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))["backup_restore"]["backups"]
    [job] = entry["jobs"]

    assert S.unstated(entry, kind=S.BACKUP_ENTRY) == () and S.unstated(job, kind=S.BACKUP_JOB) == ()
    assert entry["env_secrets"] == {} and entry["scrub_files_per_run"] == 20 and entry["server_metadata"] == {"enabled": False}
    assert job["notify"]["alert_on_error"] == {"enabled": True, "telegram_chat": "backup", "chat_id": ""}
    assert job["time_window"]["timeout"] == 3600 and job["env"] == {}


def _reference() -> dict[str, dict]:
    document = json.loads((REPO / "dbabrain/common/catalogue/shared_config_objects.json").read_text(encoding="utf-8-sig"))
    return {item["object"]: {field["field"]: field for field in item["fields"]}
            for item in document["shared_config_objects"]}


def test_the_reference_calls_every_unconditional_one_required_and_says_why():
    reference = _reference()
    for obj, names in {
        "restore_entry": ("active", "time_window", "notify", "checkdb_timeout_seconds", "checkdb_jobs", "tier"),
        "backup_entry": ("active", "env_secrets", "scrub_files_per_run"),
        "backup_job": ("active", "env", "time_window", "notify"),
        "restore_target": ("vm_platform", "sql_instance", "sql_container", "sql_login_timeout_seconds",
                           "sql_query_timeout_seconds", "remote_command_timeout_seconds",
                           "restore_command_timeout_seconds"),
        "restore_space_check": ("enabled",),
        "integrity": ("algorithm", "copy_retry_interval_seconds", "verify_source_before_copy", "uncatalogued",
                      "engine_verify_before_restore"),
    }.items():
        for name in names:
            field = reference[obj][name]
            assert field["required"] is True, (obj, name)
            assert field["default"].startswith("none - required"), (obj, name)
            assert "refused inside its own run" in field["rule"], (obj, name)


def test_the_reference_says_where_a_conditional_one_is_required():
    reference = _reference()
    for obj, name in (("restore_entry", "env"), ("restore_entry", "copy_mode"), ("restore_entry", "staging_marker"),
                      ("restore_entry", "target_visible_dir"), ("restore_entry", "server_metadata"),
                      ("restore_entry", "copy_recent_hours"), ("restore_entry", "execution_mode"),
                      ("restore_space_check", "factor"), ("restore_source", "certificate_api_verify_tls"),
                      ("backup_entry", "server_metadata")):
        field = reference[obj][name]
        assert field["required"] is False, (obj, name)
        assert "Required" in field["rule"] and "refused inside its own run" in field["rule"], (obj, name)


def test_note_stays_optional_on_every_record():
    reference = _reference()
    for obj in ("restore_entry", "backup_entry", "backup_job"):
        assert reference[obj]["note"]["required"] is False, obj
    for table in S.FIELDS.values():
        assert "note" not in {field.name for field in table}


def test_the_shipped_example_states_every_field_on_every_record():
    example = json.loads((REPO / "data/restore_config.example.json").read_text(encoding="utf-8-sig"))
    section = example["backup_restore"]
    above = S.section_above(section)
    for entry in section["restores"]:
        assert S.unstated(entry, above=above) == (), entry["restore_id"]
    for entry in section["backups"]:
        assert S.unstated(entry, kind=S.BACKUP_ENTRY) == (), entry["backup_id"]
        for job in entry["jobs"]:
            assert S.unstated(job, kind=S.BACKUP_JOB) == (), (entry["backup_id"], job["job"])


def test_the_module_says_what_stays_optional_and_why():
    said = " ".join((S.__doc__ or "").split())
    for optional in ("``note`` (free text)", "``env_secrets``, ``backup_dir``, ``server_metadata``",
                     "a backup entry's ``notify``", "the logins of a share-driven entry",
                     "``sqlcmd_path`` / ``robocopy_path``", "``space_check.measure_restore``",
                     "The ten bounds and ``weekdays`` stay optional"):
        assert optional in said, optional
