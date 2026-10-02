"""A restore onto another machine checks the room before it copies - the tool does, not the script.

A restore entry's ``space_check`` has stopped a copy that will not fit since 2026-09-19, and its
reference says *absent means on*. That was true of the share-driven SQL Server restore only. The
entries that hand their backup to another host and run the engine's own restore there - PostgreSQL,
Oracle, SQL Server in a container - copied whatever the chain held and asked nothing: the field was
read by nobody on that path (found 2026-10-02, review notes R8).

The operator's rule for it (2026-10-02): **the restore script checks nothing itself; the tool has
checked before the script runs.** So the check is in the tool's own copy, ``copy-backup-dir``, between
knowing which files it will write and writing the first:

    free on the target >= bytes still to copy x factor

with the entry's ``space_check`` - the same object, the same defaults (on, x2), the same two answers
to a target that cannot be measured.
"""

from __future__ import annotations

import json
import stat
import types

import pytest

from db_ops.backup_restore import restore_script
from db_ops.common import backup_copy, cli_backup_copy
from db_ops.lib import restore_space
from db_ops.lib.time_window import TimeWindow

GIB = 1024 ** 3
KIB_PER_GIB = 1024 * 1024


class _Entry:
    def __init__(self, name: str, size: int) -> None:
        self.filename, self.st_size, self.st_mode = name, size, stat.S_IFREG | 0o644


class _Sftp:
    def __init__(self, files: dict[str, int]) -> None:
        self.files = dict(files)
        self.put: list[str] = []

    def listdir_attr(self, path):
        return [_Entry(name, size) for name, size in self.files.items()]

    def stat(self, path):
        return object()

    def mkdir(self, path):
        pass

    def remove(self, path):
        pass

    def open(self, path, mode="r"):
        class _File:
            def write(self, _data):
                pass

            def prefetch(self, _size):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        return _File()

    def putfo(self, _reader, remote_path, file_size=0):
        self.put.append(remote_path)


class _Answer:
    def __init__(self, text: str) -> None:
        self._text, self.channel = text, self

    def read(self) -> bytes:
        return self._text.encode("utf-8")

    def recv_exit_status(self) -> int:
        return 0


class _Session:
    """A host: its files over SFTP, and - for a target - what ``df`` answers, in GiB free."""

    def __init__(self, files: dict[str, int], *, free_gib: float | None = None) -> None:
        self._sftp, self.free_gib, self.commands = _Sftp(files), free_gib, []

    def sftp(self):
        return self._sftp

    def open_stream(self, command: str):
        self.commands.append(command)
        if self.free_gib is None:
            raise OSError("the channel closed")
        free = int(self.free_gib * KIB_PER_GIB)
        return None, _Answer("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                             f"/dev/sda1 {free * 4} {free * 3} {free} 75% /\n"), None


def _copy(monkeypatch, *, files: dict[str, int], free_gib: float | None, rule=None, staged=None,
          said: list[str] | None = None):
    source = _Session(files)
    target = _Session({backup_copy.STAGING_MARKER: 1, **(staged or {})}, free_gib=free_gib)
    streamed: list[int] = []
    monkeypatch.setattr(backup_copy, "_stream_files",
                        lambda **kwargs: streamed.append(len(kwargs["files"])) or True)
    run = lambda: backup_copy.sync_backup_dir(  # noqa: E731 - one call, made by each test
        source_session=source, source_dir="/src", target_session=target, target_dir="/stage/PG",
        space_check=restore_space.SpaceCheck() if rule is None else rule,
        log=(said.append if said is not None else None))
    return run, target, streamed


# --------------------------------------------------------------------------- #
# The check, where the copy knows what it will write
# --------------------------------------------------------------------------- #
def test_a_copy_that_fits_says_what_it_measured(monkeypatch):
    said: list[str] = []
    run, target, streamed = _copy(monkeypatch, files={"full.bak": 30 * GIB}, free_gib=92, said=said)

    answer = run().as_dict()["space_check"]

    assert answer["ok"] is True and answer["incoming_bytes"] == 30 * GIB
    assert answer["required_bytes"] == 60 * GIB, "the default factor, 2"
    assert streamed == [1]
    assert any("space check: 30.0 GiB to copy" in line and "fits" in line for line in said)
    assert target.commands and "df -Pk" in target.commands[0], "asked of the target, where the files land"


def test_a_copy_that_would_not_fit_moves_no_file(monkeypatch):
    run, target, streamed = _copy(monkeypatch, files={"full.bak": 50 * GIB}, free_gib=92)

    with pytest.raises(backup_copy.CopySpaceError) as caught:
        run()

    message = str(caught.value)
    assert "/stage/PG will not fit" in message and "SHORT BY" in message
    assert "space_check.factor (now 2)" in message, "what the operator can change"
    assert "No file was copied" in message
    assert streamed == [] and target.sftp().put == []


def test_what_the_target_already_holds_is_not_counted(monkeypatch):
    """A drill run again copies the new increment, not the chain: 1 GiB against 2 GiB free fits."""
    run, _target, _streamed = _copy(
        monkeypatch, files={"full.bak": 300 * GIB, "incr.bak": 1 * GIB}, free_gib=2,
        staged={"full.bak": 300 * GIB})

    assert run().as_dict()["space_check"]["incoming_bytes"] == 1 * GIB


def test_the_entry_s_factor_is_the_margin(monkeypatch):
    files = {"full.bak": 40 * GIB}
    fits, _t, _s = _copy(monkeypatch, files=files, free_gib=50, rule=restore_space.SpaceCheck(factor=1.0))
    short, _t, _s = _copy(monkeypatch, files=files, free_gib=50, rule=restore_space.SpaceCheck(factor=2.0))

    assert fits().as_dict()["space_check"]["ok"] is True
    with pytest.raises(backup_copy.CopySpaceError):
        short()


def test_a_target_that_cannot_be_measured_is_refused(monkeypatch):
    """'Could not read the free space' must not read as 'there is enough'."""
    run, _target, streamed = _copy(monkeypatch, files={"full.bak": 1 * GIB}, free_gib=None)

    with pytest.raises(backup_copy.CopySpaceError) as caught:
        run()

    assert "could not read the free space of /stage/PG" in str(caught.value)
    assert "on_unknown" in str(caught.value) and streamed == []


def test_an_entry_may_accept_an_unmeasured_copy_and_every_run_says_so(monkeypatch):
    said: list[str] = []
    run, _target, streamed = _copy(monkeypatch, files={"full.bak": 1 * GIB}, free_gib=None,
                                   rule=restore_space.SpaceCheck(on_unknown="proceed"), said=said)

    answer = run().as_dict()["space_check"]

    assert answer == {"checked": False, "reason": "unknown", "incoming_bytes": 1 * GIB}
    assert streamed == [1]
    assert any("on_unknown=proceed" in line for line in said)


def test_a_check_turned_off_asks_the_target_nothing(monkeypatch):
    run, target, streamed = _copy(monkeypatch, files={"full.bak": 500 * GIB}, free_gib=1,
                                  rule=restore_space.SpaceCheck(enabled=False))

    assert run().as_dict()["space_check"] == {"checked": False, "reason": "disabled"}
    assert target.commands == [] and streamed == [1]


def test_a_copy_with_nothing_to_move_measures_nothing(monkeypatch):
    run, target, _streamed = _copy(monkeypatch, files={"full.bak": 30 * GIB}, free_gib=0,
                                   staged={"full.bak": 30 * GIB})

    assert "space_check" not in run().as_dict()
    assert target.commands == []


def test_a_caller_that_gives_no_rule_is_asked_nothing(monkeypatch):
    """The library does what it is told; the command is where *absent means on* is decided."""
    source, target = _Session({"full.bak": 500 * GIB}), _Session({backup_copy.STAGING_MARKER: 1}, free_gib=1)
    monkeypatch.setattr(backup_copy, "_stream_files", lambda **kwargs: True)

    result = backup_copy.sync_backup_dir(source_session=source, source_dir="/src",
                                         target_session=target, target_dir="/stage/PG")

    assert result.space is None and target.commands == []


# --------------------------------------------------------------------------- #
# The command: absent means on, and a refusal is a sentence
# --------------------------------------------------------------------------- #
def _command(monkeypatch, request: dict, *, raises: Exception | None = None) -> tuple[int, dict, dict]:
    import io
    import sys

    given: dict = {}

    def sync(**kwargs):
        given.update(kwargs)
        if raises is not None:
            raise raises
        return backup_copy.TransferResult()

    session = types.SimpleNamespace(run=lambda command: None, close=lambda: None)
    monkeypatch.setattr(cli_backup_copy, "_open", lambda login, *, role: session)
    monkeypatch.setattr(backup_copy, "sync_backup_dir", sync)
    monkeypatch.setattr(backup_copy, "open_for_the_engine", lambda *args, **kwargs: True)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = cli_backup_copy.run("copy-backup-dir", ["-"], read_request=lambda source, usage: (
        {"source": {"host": "192.0.2.49"}, "source_dir": "/src",
         "target": {"host": "192.0.2.50"}, "target_dir": "/stage/PG", **request}, 0))
    return code, json.loads(out.getvalue()), given


def test_a_request_that_says_nothing_is_checked_at_the_default(monkeypatch):
    _code, answer, given = _command(monkeypatch, {})

    assert answer["success"] is True
    assert given["space_check"] == restore_space.SpaceCheck(enabled=True, factor=2.0, on_unknown="refuse")


def test_the_request_s_own_rule_is_the_one_used(monkeypatch):
    _code, _answer, given = _command(
        monkeypatch, {"space_check": {"factor": 3.0, "on_unknown": "proceed"}})

    assert given["space_check"] == restore_space.SpaceCheck(factor=3.0, on_unknown="proceed")


def test_a_rule_that_cannot_be_obeyed_fails_the_command_before_any_host_is_reached(monkeypatch):
    code, answer, given = _command(monkeypatch, {"space_check": {"factor": 0.5}})

    assert code != 0 and answer["success"] is False
    assert "space_check.factor must be >= 1" in answer["message"]
    assert given == {}, "nothing was opened or copied"


def test_a_refusal_is_answered_in_its_own_words(monkeypatch):
    code, answer, _given = _command(monkeypatch, {}, raises=backup_copy.CopySpaceError(
        "/stage/PG will not fit: 50.0 GiB to copy, x2 = 100.0 GiB needed, 92.0 GiB free - "
        "SHORT BY 8.0 GiB. No file was copied."))

    assert code != 0 and answer["success"] is False
    assert answer["message"].startswith("/stage/PG will not fit"), "a sentence, not a class name"


# --------------------------------------------------------------------------- #
# The entry: its space_check reaches the copy
# --------------------------------------------------------------------------- #
def _sent_by(monkeypatch, **entry_fields) -> dict:
    job = restore_script.ScriptRestore(
        restore_id="PG_DRILL", db_type="postgresql", server_id="SRC", backup_dir="/backup",
        script="restore.sh", time_window=TimeWindow(), target_server_id="DST",
        target_backup_dir="/opt/db_ops/backup/pg_restore", source_backup_host_dir="/opt/backup/pg",
        **entry_fields)
    sent: dict[str, dict] = {}

    def run(command, request, **_kwargs):
        sent[command] = request
        return {"include": []} if command == "backup-chain" else {"copied": 0, "skipped": 0}

    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_kwargs: {"host": "192.0.2.49"})
    monkeypatch.setattr(restore_script.common_cli, "run", run)
    endpoint = types.SimpleNamespace(container_name="")
    restore_script.transfer_backup_to_target(job, source=endpoint, target=endpoint, prune=False)
    return sent["copy-backup-dir"]


def test_an_entry_that_says_nothing_is_checked_at_the_default(monkeypatch):
    assert _sent_by(monkeypatch)["space_check"] == {"enabled": True, "factor": 2.0, "on_unknown": "refuse"}


def test_an_entry_s_own_space_check_is_what_the_copy_is_held_to(monkeypatch):
    sent = _sent_by(monkeypatch, space_check=restore_space.SpaceCheck(factor=3.0))

    assert sent["space_check"] == {"enabled": True, "factor": 3.0, "on_unknown": "refuse"}


def _entry_file(tmp_path, **fields) -> str:
    config = tmp_path / "restore_config.json"
    config.write_text(json.dumps({"backup_restore": {"restores": [{
        "restore_id": "PG_DRILL", "db_type": "postgresql", "server_id": "SRC",
        "target_server_id": "DST", "backup_dir": "/backup", "script": "restore.sh",
        "target_backup_dir": "/opt/db_ops/backup/pg_restore/stage",
        "source_backup_host_dir": "/opt/backup/pg", "cleanup_retention": 691200,
        **fields}]}}), encoding="utf-8")
    return str(config)


def test_the_entry_s_space_check_is_read_from_restore_config(tmp_path):
    (job,) = restore_script.load_script_restores(
        _entry_file(tmp_path, space_check={"factor": 2.0, "on_unknown": "proceed"}))

    assert job.space_check == restore_space.SpaceCheck(factor=2.0, on_unknown="proceed")


def test_an_entry_with_a_misspelled_space_check_is_refused_by_name(tmp_path):
    """`{"factory": 3.0}` accepted silently is a copy held to 2 while its entry says 3."""
    with pytest.raises(ValueError, match="PG_DRILL: Unknown space_check field"):
        restore_script.load_script_restores(_entry_file(tmp_path, space_check={"factory": 2.0}))


def test_an_entry_that_asks_for_its_restore_to_be_measured_is_refused_until_that_exists(tmp_path):
    """The operator, 2026-10-02: the restore is measured only when the entry says so, on every
    engine - and PostgreSQL and Oracle cannot be measured yet. So a script-driven entry that says so
    is refused when it is read: accepted, it would say it is measured and not be."""
    with pytest.raises(ValueError, match="PG_DRILL: space_check.measure_restore is not supported"):
        restore_script.load_script_restores(
            _entry_file(tmp_path, space_check={"measure_restore": True}))

    (job,) = restore_script.load_script_restores(
        _entry_file(tmp_path, space_check={"measure_restore": False}))
    assert job.space_check.measure_restore is False


def test_the_copy_command_is_not_asked_to_measure_a_restore(monkeypatch):
    code, answer, given = _command(monkeypatch, {"space_check": {"measure_restore": True}})

    assert code != 0 and "belongs to a restore entry" in answer["message"]
    assert given == {}


# --------------------------------------------------------------------------- #
# One command and one reading, for both restore paths
# --------------------------------------------------------------------------- #
def test_the_free_space_is_read_from_the_last_line_of_df():
    answer = ("Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
              "/dev/mapper/ubuntu--vg-ubuntu--lv 514937088 100 96468992 96% /\n")

    assert restore_space.parse_df_free_bytes(answer) == 96468992 * 1024
    assert restore_space.parse_df_free_bytes("df: /nowhere: No such file or directory\n") is None
    assert restore_space.parse_df_free_bytes("") is None


def test_both_paths_ask_the_target_with_the_same_command():
    from db_ops.backup_restore import space

    assert space.linux_free_space_command is restore_space.free_space_command
    assert "while [ ! -e" in restore_space.free_space_command("/stage/it's here")
    assert "'/stage/it'\\''s here'" in restore_space.free_space_command("/stage/it's here")
