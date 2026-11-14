"""A SQL Server restore replaces a database its target holds only when the entry says so.

``REPLACE`` overwrites whatever holds the name, and the full template first sets the database
``SINGLE_USER WITH ROLLBACK IMMEDIATE``: an entry aimed at the wrong target threw out its users and
then overwrote their data, and nothing asked (owner decision G2.10, 2026-10-01). The guard written
then stopped for a database ONLINE and for no other: one OFFLINE, a log-shipping standby left
RESTORING, or the half of an unfinished restore was replaced without a word.

0.39.0 (1.362), the operator, 2026-11-13 (translated): *restore must be guarded very carefully* and
*overwriting a DB must be set explicitly, off by default*. So without ``overwrite_existing: true``:

* the batch starts with a guard that raises when the target holds the name **in any state**, naming
  the state it found - before the database is set single-user, before anything is restored;
* the RESTORE statement carries no ``REPLACE``, so SQL Server itself refuses a name that came into
  being between the guard and the statement.

``overwrite_existing: true`` is the restore as it was: no guard, ``REPLACE`` - what a drill that
runs again over its own last restore states.

G2.11: a script entry that names no ``env.MSSQL_USER`` is refused, and never runs as ``sa``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from conftest import landing_of, patch_restore

from dbabrain.backup_restore import restore_by_id
from dbabrain.backup_restore.restore_script import load_script_restores
from dbabrain.common.restorestep import sqlserver as mssql

FILES = {"data": "/var/opt/mssql/data/HR.mdf", "log": "/var/opt/mssql/data/HR.ldf"}


def _full(**request) -> str:
    return mssql.build_statements("full", {"database_name": "HR", "move_files": FILES, **request},
                                  ["/in/HR/FULL/hr.bak"])[0]


def test_the_guard_comes_before_anything_the_restore_does():
    text = _full()

    guard = text.index("IF DB_ID(N'HR') IS NOT NULL\nBEGIN\n    DECLARE @dbabrainFoundState")
    assert guard < text.index("SET SINGLE_USER") < text.index("RESTORE DATABASE")
    assert "RAISERROR" in text[guard:text.index("SET SINGLE_USER")]
    assert "RETURN;" in text[guard:text.index("SET SINGLE_USER")]
    assert "overwrite_existing: true" in text


def test_it_stops_for_a_database_in_any_state_and_names_the_state():
    guard = mssql.existing_guard("HR")

    # No state is excepted: the old guard asked `= N'ONLINE'`, and an OFFLINE database, a standby
    # left RESTORING and a half-restored one all went through it.
    assert "N'ONLINE'" not in guard and "!= N'RESTORING'" not in guard
    assert "DATABASEPROPERTYEX(N'HR', N'Status')" in guard
    assert "(state %s)" in guard and "16, 1, @dbabrainFoundState);" in guard
    assert "Nothing was restored" in guard and "database_mappings" in guard


def test_a_percent_or_a_quote_in_the_name_cannot_break_the_message():
    guard = mssql.existing_guard("50%'s")

    assert "DB_ID(N'50%''s')" in guard
    # RAISERROR reads % as a format: the name's own is doubled, the one %s is the state's.
    assert "[50%%''s] exists on this server (state %s)" in guard


def test_without_the_entry_s_word_the_restore_carries_no_replace():
    assert "REPLACE," not in _full() and "REPLACE," not in _full(overwrite_existing=False)
    plain = mssql.build_statements("full", {"database_name": "HR"}, ["/in/HR/FULL/hr.bak"])[0]
    assert "REPLACE" not in plain.split("RESTORE DATABASE", 1)[1]


def test_a_stated_overwrite_has_no_guard_and_replaces():
    text = _full(overwrite_existing=True)

    assert "@dbabrainFoundState" not in text
    assert "REPLACE," in text


@pytest.mark.parametrize("value", [1, "true", "yes", None])
def test_only_true_is_the_word(value):
    """`overwrite_existing = 1` is not `true`: anything but the boolean leaves the guard in."""
    assert "@dbabrainFoundState" in _full(overwrite_existing=value)


def test_only_a_full_is_guarded_and_whatever_replace_says():
    diff = mssql.build_statements("diff", {"database_name": "HR"}, ["/in/HR/DIFF/d.bak"])[0]
    log = mssql.build_statements("log", {"database_name": "HR"}, ["/in/HR/LOG/l.trn"])[0]

    assert "@dbabrainFoundState" not in diff and "@dbabrainFoundState" not in log
    # `replace: false` used to switch the guard off with the REPLACE; the guard is the entry's word alone.
    assert "@dbabrainFoundState" in _full(replace=False)


def test_the_drill_entry_s_word_reaches_the_restore(monkeypatch):
    from dbabrain.backup_restore import restore_database

    config = SimpleNamespace(overwrite_existing=True)
    candidate = SimpleNamespace(restore_database_name="HR", restore_data_file_on_vm="d", restore_log_file_on_vm="l")
    patch_restore(monkeypatch, "_target_path_str", lambda path, cfg: str(path))

    fields = restore_database._restore_step("full", candidate, config, path="/in/hr.bak")

    assert fields["overwrite_existing"] is True


def _entry(**over) -> dict:
    entry = {"restore_id": "LAB_TO_LAB", "db_type": "sqlserver", "server_id": "SRC",
             "target_server_id": "TGT", "backup_dir": "/out", "script": "sqlserver",
             "target_backup_dir": "/opt/db_ops/staging/lab", "source_backup_host_dir": "/opt/out",
             "cleanup_retention": 86400, "time_window": {"from_hour": 0, "to_hour": 23},
             "env": {"MSSQL_USER": "sa"}}   # stated: an entry without it is refused (rules R50)
    entry.update(over)
    return entry


def _load(tmp_path, entry: dict):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": [entry]}}), encoding="utf-8")
    return load_script_restores(path)[0]


def test_a_script_entry_reads_the_field_and_defaults_to_refusing(tmp_path):
    assert _load(tmp_path, _entry(overwrite_existing=True)).overwrite_existing is True
    # Stating none - or something that is not true or false - is no statement (0.39.0, 1.362): the
    # entry is refused inside its own run (test_overwriting_a_database_is_stated_on_every_entry.py).
    assert _load(tmp_path, _entry(overwrite_existing=False)).overwrite_existing is False
    assert _load(tmp_path, _entry()).overwrite_existing is None
    assert _load(tmp_path, _entry(overwrite_existing="yes")).overwrite_existing is None


@pytest.fixture
def planned(monkeypatch):
    def plan(**job_fields):
        job = SimpleNamespace(restore_id="R", server_id="SRC", target_server_id="TGT", target_container="",
                              target_backup_dir="/in", target_visible_dir="", backup_dir="/out",
                              env_secrets={"MSSQL_PASSWORD": "REF"}, landing=landing_of("DB"), **job_fields)
        monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda j, **_: 1433)
        monkeypatch.setattr(restore_by_id, "_secret", lambda ref, secrets, where: "pw")
        monkeypatch.setattr(restore_by_id, "_list_backup_files", lambda request: {
            "files": [{"path": "/in/DB/FULL/f.bak", "database_name": "DB", "kind": "full"}]
            if "full" in request.get("kinds", []) else [], "newest_finished_at": "2026-10-01T00:00:00Z"})
        return restore_by_id._plan_sqlserver(job, {}, point_in_time="", host={"host": "h"}, data_dir=None)
    return plan


def test_the_script_path_passes_the_entry_s_word(planned):
    steps = planned(env={"MSSQL_USER": "restorer"}, overwrite_existing=True)

    full = next(step for step in steps if step["op"] == "restore-full")
    assert full["request"]["overwrite_existing"] is True
    assert full["request"]["target"]["username"] == "restorer"
    assert not any(step["op"] == "warning" for step in steps)


def test_the_script_path_never_turns_a_false_into_a_true(planned):
    full = next(step for step in planned(env={"MSSQL_USER": "restorer"}, overwrite_existing=False)
                if step["op"] == "restore-full")

    assert full["request"]["overwrite_existing"] is False


def test_an_unnamed_login_is_refused_and_never_runs_as_sa(planned):
    """0.26.0 ran it as `sa` and said so in the answer (G2.11, phase 1); 0.27.0 refuses it (rules
    R50) - before a single step is planned."""
    with pytest.raises(restore_by_id.RestoreByIdError, match="env.MSSQL_USER is not stated"):
        planned(env={}, overwrite_existing=False)
