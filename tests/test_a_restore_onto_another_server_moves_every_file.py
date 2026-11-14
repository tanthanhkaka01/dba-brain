"""A SQL Server restore onto another server moves every file to that server's folders (0.39.0, 1.354).

Read on 2026-11-13, mapping the restore half of the new flow: the script restore sent `restore-full`
with no file placement at all. Every SQL Server on the new flow is a Windows server whose backups
name `D:\\` and `F:\\` paths, and the restore from the repository lands on a Linux SQL Server - where
a RESTORE that keeps the source's paths fails before it writes a page.

The two forms that existed did not fit: `move` needs the logical names in advance, and `move_files`
moves only the first data and the first log file, so a database with an `.ndf` was refused. So
`relocate` reads the backup's own file list on the target and moves **every** file, to the folders
the target itself names (or to the two stated), each as `<database>_<its own file name>`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import landing_of
from dbabrain.common.restorestep import RestoreStepError
from dbabrain.common.restorestep import sqlserver as mssql


def _full(**request) -> str:
    return mssql.build_statements("full", {"database_name": "APP", **request}, ["/stage/APP_FULL.bak"])[0]


def test_every_file_the_backup_names_goes_to_the_targets_default_folders():
    text = _full(relocate={})
    assert "INSERT INTO @filelist\nEXEC('RESTORE FILELISTONLY FROM DISK = N''/stage/APP_FULL.bak''');" in text
    assert "CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(512))" in text
    assert "CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(512))" in text
    # One MOVE per row of the file list - not the first data and log file only.
    assert "TOP (1)" not in text and "FROM @filelist\nORDER BY FileId;" in text
    assert "CASE WHEN [Type] = 'L' THEN @logDir ELSE @dataDir END + N'APP_'" in text
    assert "EXEC sys.sp_executesql @restoreSql;" in text


def test_a_folder_the_target_does_not_name_stops_the_restore_with_a_sentence():
    assert "THROW 51001, 'The target names no default data or log folder" in _full(relocate={})


def test_stated_folders_are_used_and_a_quote_in_one_is_doubled():
    text = _full(relocate={"data_dir": "/var/opt/mssql/data", "log_dir": "/mnt/log's"})
    assert "DECLARE @dataDir nvarchar(512) = N'/var/opt/mssql/data';" in text
    assert "DECLARE @logDir nvarchar(512) = N'/mnt/log''s';" in text
    # A folder without its separator gets the one its own path uses.
    assert "SET @dataDir = @dataDir + CASE WHEN CHARINDEX(N'\\', @dataDir) > 0 THEN N'\\' ELSE N'/' END;" in text


def test_a_quote_in_the_database_name_is_doubled_once_per_literal_it_sits_in():
    text = mssql.build_statements("full", {"database_name": "O'Brien", "relocate": {},
                                           "overwrite_existing": True}, ["/stage/x.bak"])[0]
    assert "ALTER DATABASE [O'Brien] SET SINGLE_USER" in text                  # an identifier
    assert "+ N'O''Brien_'" in text                                             # one literal deep
    assert "N'RESTORE DATABASE [O''Brien] FROM DISK = N''/stage/x.bak'' WITH '" in text  # inside @restoreSql


def test_norecovery_until_the_last_step_and_multi_user_after_recovery():
    # No REPLACE unless the entry says the target's database is replaced (0.39.0, 1.362).
    assert "N', NORECOVERY, CHECKSUM, STATS = 10;'" in _full(relocate={})
    assert "N', REPLACE, NORECOVERY, CHECKSUM, STATS = 10;'" in _full(relocate={}, overwrite_existing=True)
    last = _full(relocate={}, with_recovery=True)
    assert "N', RECOVERY, CHECKSUM, STATS = 10;'" in last and last.endswith("SET MULTI_USER;")


def test_a_database_the_target_holds_is_still_guarded_first():
    text = _full(relocate={})
    assert text.index("exists on this server (state %s)") < text.index("SET SINGLE_USER")


@pytest.mark.parametrize("request_, match", [
    ({"relocate": {}, "move_files": {"data": "/d", "log": "/l"}}, "not both"),
    ({"relocate": {}, "move": {"APP": "/a.mdf"}}, "not both"),
    ({"relocate": "yes"}, "relocate is {}"),
    ({"relocate": True}, "relocate is {}"),
    ({"relocate": {"data": "/d"}}, "relocate is {}"),
])
def test_relocate_is_one_way_of_placing_the_files(request_, match):
    with pytest.raises(RestoreStepError, match=match):
        _full(**request_)


def test_relocate_belongs_to_the_full():
    with pytest.raises(RestoreStepError, match="full restore only"):
        mssql.build_statements("log", {"database_name": "APP", "relocate": {}}, ["/stage/a.trn"])


def test_relocate_absent_is_the_restore_as_before():
    assert _full(relocate=None) == _full()


def test_the_restore_from_the_repository_asks_for_it(monkeypatch):
    from dbabrain.backup_restore import restore_by_id

    full = {"path": "/stage/APP/FULL/APP_FULL_1.bak", "first_lsn": 900, "last_lsn": 1000, "checkpoint_lsn": 950}

    def listed(request):
        files = [full] if request["kinds"][0] == "full" else []
        return {"files": files, "newest_finished_at": "2026-11-01 01:00:00" if files else None}

    monkeypatch.setattr(restore_by_id, "_list_backup_files", listed)
    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/stage")
    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, data_dir=None: 1433)
    job = SimpleNamespace(restore_id="APP_TO_LAB", env={"MSSQL_USER": "dba"}, landing=landing_of("APP"),
                          env_secrets={}, overwrite_existing=False)
    steps = restore_by_id._plan_sqlserver(job, {}, point_in_time="", host={"host": "192.0.2.40"}, data_dir=None)
    first = next(step for step in steps if step["op"] == "restore-full")
    assert first["request"]["relocate"] == {}
    # Held to the request's reference as the restore step holds it - the bool first sent was refused there.
    from dbabrain.lib import request_check
    assert request_check.refusal(request_check.check("restore-full", first["request"])) == ""
