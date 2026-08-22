"""Proving a restore is usable, and replaying the machinery a backup does not carry.

**Verify** exists because every engine has a way of looking finished and being unusable, and all
three report success at the command that put them there: SQL Server leaves a database `RESTORING`
when a chain was never recovered, Oracle mounts without opening, PostgreSQL starts in recovery and
never promotes. A state column is not enough either - a database can read ONLINE and still refuse a
query while it finishes an upgrade step - so a real statement is run.

**Metadata replay** exists because a SQL Server backup covers user databases only: `master`,
`msdb` and `model` are excluded, so every login, role, permission and Agent job is absent after a
restore. The database comes back and nobody can log into it.
"""

from __future__ import annotations

import pytest

from db_ops.common import verifyrestore
from db_ops.common.restoremetadata import MetadataReplayError, replay, split_batches


# --------------------------------------------------------------------------- #
# Verify.
# --------------------------------------------------------------------------- #

def test_an_unknown_engine_is_refused_by_name():
    with pytest.raises(verifyrestore.VerifyError, match="db_type must be"):
        verifyrestore.verify({"db_type": "mysql"})


def test_a_verdict_is_only_ok_when_every_database_is():
    rows = [{"database": "a", "ok": True}, {"database": "b", "ok": False}]
    verdict = verifyrestore._verdict(rows)
    assert verdict["ok"] is False
    assert verdict["failed"] == 1
    assert verdict["checked"] == 2


def test_an_empty_check_is_not_a_pass():
    """Nothing checked is not the same as everything healthy, and reporting ok for it would let a
    restore that created no databases at all report success."""
    assert verifyrestore._verdict([])["ok"] is False


def test_oracle_mounted_is_not_usable(monkeypatch):
    """The trap: RMAN finished, the instance is up, and the database was never opened."""
    monkeypatch.setattr(verifyrestore, "run",
                        lambda *a, **k: {"exit_code": 0, "stdout": "MOUNTED", "stderr": ""})

    result = verifyrestore.verify({"db_type": "oracle",
                                   "host": {"runtime": "linux", "host": "h", "username": "u"}})

    assert result["ok"] is False
    assert result["databases"][0]["state"] == "MOUNTED"


def test_oracle_read_write_is_usable(monkeypatch):
    monkeypatch.setattr(verifyrestore, "run",
                        lambda *a, **k: {"exit_code": 0, "stdout": "READ WRITE 1", "stderr": ""})

    assert verifyrestore.verify({"db_type": "oracle",
                                 "host": {"runtime": "linux", "host": "h",
                                          "username": "u"}})["ok"] is True


def test_postgresql_still_in_recovery_is_not_usable(monkeypatch):
    """The server is up and refuses writes, and nothing else says so."""
    monkeypatch.setattr(verifyrestore, "run",
                        lambda *a, **k: {"exit_code": 0, "stdout": "t\n5\n", "stderr": ""})

    result = verifyrestore.verify({"db_type": "postgresql",
                                   "host": {"runtime": "linux", "host": "h", "username": "u"}})

    assert result["ok"] is False
    assert result["databases"][0]["state"] == "IN RECOVERY"


def test_postgresql_promoted_is_usable(monkeypatch):
    monkeypatch.setattr(verifyrestore, "run",
                        lambda *a, **k: {"exit_code": 0, "stdout": "f\n5\n", "stderr": ""})

    assert verifyrestore.verify({"db_type": "postgresql",
                                 "host": {"runtime": "linux", "host": "h",
                                          "username": "u"}})["ok"] is True


# --------------------------------------------------------------------------- #
# Metadata replay.
# --------------------------------------------------------------------------- #

def test_a_script_is_split_on_its_go_separators():
    """GO is a batch separator the TDS protocol knows nothing about. Sending a file whole makes
    the server reject everything after the first CREATE ... AS."""
    script = "CREATE LOGIN a FROM WINDOWS;\nGO\nCREATE LOGIN b FROM WINDOWS;\nGO\n"
    assert len(split_batches(script)) == 2


def test_go_is_matched_case_insensitively_and_alone_on_its_line():
    """A `GO` inside a string or a name is not a separator; only a line that is one."""
    script = "CREATE LOGIN [GO_USER] FROM WINDOWS;\ngo\nSELECT 1;\n"
    batches = split_batches(script)
    assert len(batches) == 2
    assert "GO_USER" in batches[0]


def test_an_empty_script_produces_no_batches():
    assert split_batches("\nGO\n\nGO\n") == []


def test_files_must_be_named_and_ordered_by_the_caller():
    """Order is the caller's: logins before the databases are restored so their users are not
    orphaned, agent_jobs after, because job steps name databases that must exist."""
    with pytest.raises(MetadataReplayError, match="files is required"):
        replay({"target": {"host": "h"}})


def test_a_bare_string_where_the_file_list_belongs_is_refused():
    with pytest.raises(MetadataReplayError, match="must be an array"):
        replay({"target": {"host": "h"}, "files": "/bundle/logins.sql"})


def test_a_target_must_be_named():
    with pytest.raises(MetadataReplayError, match="target.host is required"):
        replay({"files": ["/bundle/logins.sql"]})


def test_a_missing_local_file_is_reported_by_path(tmp_path):
    with pytest.raises(MetadataReplayError, match="file not found"):
        replay({"target": {"host": "h"}, "files": [str(tmp_path / "nope.sql")]})


def test_a_dry_run_counts_batches_without_connecting(tmp_path):
    """A rehearsal that had to reach the instance would be useless for checking a bundle before a
    maintenance window."""
    script = tmp_path / "logins.sql"
    script.write_text("CREATE LOGIN a FROM WINDOWS;\nGO\nCREATE LOGIN b FROM WINDOWS;\nGO\n",
                      encoding="utf-8")

    result = replay({"target": {"host": "h"}, "files": [str(script)], "dry_run": True})

    assert result["dry_run"] is True
    assert result["files"][0]["batches"] == 2


def test_postgresql_verify_names_the_login_it_connects_as(monkeypatch):
    """psql defaults to the OS user and `docker exec` runs as root, so leaving -U off reported
    `FATAL: role "root" does not exist` against a cluster that was perfectly healthy - the
    verifier's own login problem presented as the database's."""
    seen = {}

    def _run(host, command, **kwargs):
        seen["cmd"] = command
        return {"exit_code": 0, "stdout": "f\n5\n", "stderr": ""}

    monkeypatch.setattr(verifyrestore, "run", _run)

    verifyrestore.verify({"db_type": "postgresql",
                          "host": {"runtime": "linux", "host": "h", "username": "u"}})

    assert "-U postgres" in seen["cmd"]
