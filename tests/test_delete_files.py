"""Deleting backup files is the one operation where being wrong is not recoverable.

Everything else in this layer can be re-run: a listing that returns nothing costs a retry, a
restore that fails leaves the source untouched. A delete that removed the wrong file removed the
thing the restore was going to use. So the tests here are mostly about what the command REFUSES,
and they are the reason the refusals live in front of the work rather than inside it.

The old cleanup (``backup_restore/delete_backup.py``) computed its own set from a retention window
and a ``*.bak`` glob. These pin the shape that replaced it: the caller passes full paths it has
already looked at, and nothing here expands, resolves or infers one.
"""

from __future__ import annotations

import pytest

from db_ops.common import deletefiles
from db_ops.common.deletefiles import DELETED, FAILED, NOT_FOUND, SKIPPED, DeleteFileError


@pytest.fixture
def ran(monkeypatch):
    """Record every command that would have been run, and answer with a scripted token.

    Nothing touches a filesystem: the point of these tests is the decision, and a test that had to
    create real files could not exercise "the path is on a Windows host" from here at all.
    """
    calls = []
    answers = {}

    def fake_run(host, command, *, timeout=300, client=None):
        calls.append({"command": command, "client": client})
        for needle, token in answers.items():
            if needle in command:
                return {"exit_code": 0, "stdout": token, "stderr": ""}
        return {"exit_code": 0, "stdout": "DELETED 100", "stderr": ""}

    class _Client:
        """Stands in for the paramiko client, and records that the batch closed what it opened."""

        closed = False

        def close(self):
            type(self).closed = True

        def __repr__(self):
            return "SHARED-CLIENT"

    monkeypatch.setattr(deletefiles, "run", fake_run)
    monkeypatch.setattr(deletefiles, "open_client", lambda host: _Client())
    return {"calls": calls, "answers": answers, "client": _Client}


# --------------------------------------------------------------------------- #
# What it refuses, before anything is deleted
# --------------------------------------------------------------------------- #
def test_a_wildcard_is_refused_rather_than_expanded(ran):
    """`*.bak` under the wrong root is one typo from the full backup being restored from. The old
    cleanup expanded exactly that pattern; this one will not accept it at all."""
    with pytest.raises(DeleteFileError, match="not a pattern"):
        deletefiles.delete_file({"path": "/backup/*.bak", "host": {"runtime": "linux"}})

    assert ran["calls"] == []


@pytest.mark.parametrize("path", ["backup/a.bkp", "./a.bkp", "a.bkp"])
def test_a_relative_path_is_refused(ran, path):
    """It resolves against whatever directory the shell happened to start in, which is a different
    file on every runtime — and on none of them the one the caller meant."""
    with pytest.raises(DeleteFileError, match="must be absolute"):
        deletefiles.delete_file({"path": path, "host": {"runtime": "linux"}})


def test_a_windows_unc_path_is_absolute(ran):
    """SQL Server backups on this estate live on a share, and refusing `\\\\host\\share\\...` would
    lock the command out of the only place they are."""
    result = deletefiles.delete_file(
        {"path": r"\\192.0.2.250\SQLBK\APPDB\a.bak", "host": {"runtime": "windows"}})

    assert result["file"]["status"] == DELETED


def test_the_optional_fence_refuses_a_path_outside_it(ran):
    with pytest.raises(DeleteFileError, match="not under must_be_under"):
        deletefiles.delete_files({
            "paths": ["/backup/keep/a.bkp"],
            "must_be_under": "/backup/staging",
            "host": {"runtime": "linux"},
        })

    assert ran["calls"] == []


def test_the_fence_is_not_fooled_by_a_shared_prefix(ran):
    """`/backup/staging-old` is not inside `/backup/staging`, and a plain startswith says it is."""
    with pytest.raises(DeleteFileError, match="not under must_be_under"):
        deletefiles.delete_files({
            "paths": ["/backup/staging-old/a.bkp"],
            "must_be_under": "/backup/staging",
            "host": {"runtime": "linux"},
        })


def test_every_path_is_validated_before_the_first_delete(ran):
    """A batch that stopped at the bad path halfway would already have deleted the files before it —
    files the caller may well not have wanted gone on their own."""
    with pytest.raises(DeleteFileError):
        deletefiles.delete_files({
            "paths": ["/backup/a.bkp", "/backup/b.bkp", "relative.bkp"],
            "host": {"runtime": "linux"},
        })

    assert ran["calls"] == []


def test_paths_must_be_a_list_not_a_string(ran):
    """A string would iterate character by character and delete nothing that exists, reporting a
    hundred not_found rows as a success."""
    with pytest.raises(DeleteFileError, match="not a string"):
        deletefiles.delete_files({"paths": "/backup/a.bkp", "host": {"runtime": "linux"}})


# --------------------------------------------------------------------------- #
# What each answer from the host means
# --------------------------------------------------------------------------- #
def test_a_file_that_is_already_gone_is_a_success(ran):
    """Delete states an end condition, not an act. Re-running after a partial failure must not turn
    "the work is already done" into an error the caller has to special-case."""
    ran["answers"]["gone.bkp"] = "MISSING 0"

    result = deletefiles.delete_file({"path": "/backup/gone.bkp", "host": {"runtime": "linux"}})

    assert result["file"]["status"] == NOT_FOUND
    assert result["counts"]["failed"] == 0


def test_a_directory_is_refused_by_the_host_and_reported_as_failed(ran):
    """A caller that meant one file and passed its parent would lose the whole backup set. The
    check is in the same command as the delete, so nothing can turn into a directory in between."""
    ran["answers"]["backup/set"] = "DIR 0"

    result = deletefiles.delete_file({"path": "/backup/set", "host": {"runtime": "linux"}})

    assert result["file"]["status"] == FAILED
    assert "directory" in result["file"]["reason"]


def test_bytes_freed_counts_only_what_was_actually_removed(ran):
    """A dry run frees nothing, and reporting its bytes as freed space is a report that lies about
    a disk the operator is deciding about."""
    ran["answers"]["a.bkp"] = "WOULD 5000"

    result = deletefiles.delete_files(
        {"paths": ["/backup/a.bkp"], "host": {"runtime": "linux"}, "dry_run": True})

    assert result["files"][0]["status"] == SKIPPED
    assert result["files"][0]["size"] == 5000
    assert result["bytes_freed"] == 0


def test_a_dry_run_command_contains_no_delete(ran):
    """The strongest thing that can be asserted about a dry run: the removal is not in the text
    that gets executed, so it cannot happen by any path through the shell."""
    deletefiles.delete_file(
        {"path": "/backup/a.bkp", "host": {"runtime": "linux"}, "dry_run": True})

    assert "rm -f" not in ran["calls"][0]["command"]


def test_a_real_run_does_contain_the_delete(ran):
    deletefiles.delete_file({"path": "/backup/a.bkp", "host": {"runtime": "linux"}})

    assert "rm -f" in ran["calls"][0]["command"]


def test_output_that_makes_no_sense_is_a_failure_not_a_success(ran):
    """An unreadable answer means the command did not do what it was asked. Defaulting to success
    here is how a delete that never ran gets reported as space freed."""
    ran["answers"]["a.bkp"] = "bash: stat: command not found"

    result = deletefiles.delete_file({"path": "/backup/a.bkp", "host": {"runtime": "linux"}})

    assert result["file"]["status"] == FAILED


# --------------------------------------------------------------------------- #
# The batch
# --------------------------------------------------------------------------- #
def test_the_batch_deletes_one_file_per_command(ran):
    """One file per command is what makes a per-file answer possible at all: a single `rm a b c`
    returns one exit code for three files and cannot say which of them is still there."""
    deletefiles.delete_files(
        {"paths": ["/backup/a.bkp", "/backup/b.bkp", "/backup/c.bkp"], "host": {"runtime": "linux"}})

    assert len(ran["calls"]) == 3
    assert all(sum(command in call["command"] for call in ran["calls"]) == 1
               for command in ("/backup/a.bkp", "/backup/b.bkp", "/backup/c.bkp"))


def test_the_batch_opens_one_connection_and_lends_it_to_every_file(ran):
    """Reconnecting per file is the cost this layer already paid once, when per-file SFTP over two
    internet hops measured 10 KB/s. Deleting 200 pieces would open 200 SSH sessions."""
    deletefiles.delete_files({
        "paths": ["/backup/a.bkp", "/backup/b.bkp"],
        "host": {"runtime": "linux", "host": "10.0.0.1", "username": "u"},
    })

    clients = [call["client"] for call in ran["calls"]]
    assert len(set(id(client) for client in clients)) == 1, "one connection, lent to every file"
    # And handed back: a batch that leaks the session leaks one per retention run.
    assert ran["client"].closed is True


def test_a_local_batch_opens_no_connection_at_all(ran, monkeypatch):
    """`host` omitted means this machine. Opening an SSH client to nowhere is how the worker's own
    filesystem became unreachable from a command written for remote hosts."""
    monkeypatch.setattr(deletefiles, "open_client",
                        lambda host: pytest.fail("a local host must not be connected to"))

    result = deletefiles.delete_files({"paths": ["/backup/a.bkp"]})

    assert result["counts"][DELETED] == 1


def test_one_failure_does_not_stop_the_rest_by_default(ran):
    """A locked file in the middle of a retention run must not leave the other 199 in place."""
    ran["answers"]["b.bkp"] = "DIR 0"

    result = deletefiles.delete_files({
        "paths": ["/backup/a.bkp", "/backup/b.bkp", "/backup/c.bkp"], "host": {"runtime": "linux"}})

    assert [row["status"] for row in result["files"]] == [DELETED, FAILED, DELETED]
    assert result["failed"] == ["/backup/b.bkp"]
    assert result["stopped_early"] is False


def test_stop_on_error_stops_and_says_so(ran):
    """Opt-in, and it has to be visible in the answer: a short list that does not say it stopped
    reads as "that was all there was to do"."""
    ran["answers"]["a.bkp"] = "DIR 0"

    result = deletefiles.delete_files({
        "paths": ["/backup/a.bkp", "/backup/b.bkp"], "host": {"runtime": "linux"},
        "stop_on_error": True})

    assert len(result["files"]) == 1
    assert result["stopped_early"] is True


def test_the_failed_list_is_exactly_what_a_retry_needs(ran):
    """Returned as paths, not as indexes into the request: a caller retrying should be able to send
    `failed` straight back as `paths`."""
    ran["answers"]["b.bkp"] = "DIR 0"

    result = deletefiles.delete_files({
        "paths": ["/backup/a.bkp", "/backup/b.bkp"], "host": {"runtime": "linux"}})

    assert result["failed"] == ["/backup/b.bkp"]
