"""A restore's copy opens its source folder only when it must, and never asks sudo for a password (0.39.0, 1.369).

Found reading a report: the backup repository's own VM showed twenty errors a day in its journal -
``pam_unix(sudo:auth): conversation failed`` and ``auth could not identify password for [<login>]``,
always in pairs, each pair at the minute a restore started. They were dbabrain's: before every copy,
``prepare-staging`` ran a bare ``sudo chmod -R a+rX`` on the source folder, so that a file a live
archivelog job had just written 0640 could be read. On a host whose sudo wants a password there is
nobody to type one; sudo's conversation failed, PAM logged it twice, and the event-log metric then
reported the monitor's own noise as the host's errors.

Two things were wrong, and both are held here:

* the folder was chmod-ed whether or not anything in it was unreadable, and with a sudo that asks -
  now the folder is asked first, and ``sudo -n`` refuses at once where a password is needed, with
  no conversation and nothing in the journal (read on that host, 2026-11-13);
* a restore **from the repository** chmod-ed the repository's own folder - files the offload proved
  and its login reads. A restore never changes their modes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import restore_script
from dbabrain.common import cli_backup_copy
from dbabrain.lib import restore_space
from dbabrain.lib.integrity import RestoreIntegrity


def test_the_folder_is_asked_before_anything_is_changed():
    command = cli_backup_copy.make_readable_command("/opt/backup/PG LAB")

    assert command.startswith("if [ -n \"$(find '/opt/backup/PG LAB' ! -readable -print -quit 2>/dev/null)\" ]; then ")
    assert command.endswith("; fi; true"), "never the reason a copy fails: the listing names what stays unreadable"


def test_sudo_is_never_asked_in_a_way_that_wants_a_password_typed():
    command = cli_backup_copy.make_readable_command("/opt/backup")

    assert "sudo -n chmod -R a+rX /opt/backup" in command
    assert command.count("sudo") == 1, "one call, and it is the non-interactive one"


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="runs the shell text on a POSIX host")
def test_a_folder_that_is_readable_never_reaches_sudo(tmp_path):
    folder = tmp_path / "backup"
    folder.mkdir()
    (folder / "a.bak").write_text("x")
    marker = tmp_path / "sudo_was_called"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "sudo").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (fake / "sudo").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}

    done = subprocess.run(["sh", "-c", cli_backup_copy.make_readable_command(str(folder))], env=env)

    assert done.returncode == 0 and not marker.exists()


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh") or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs a POSIX host and a login that is not root (root reads everything)")
def test_a_file_the_login_cannot_read_is_what_brings_sudo_in(tmp_path):
    folder = tmp_path / "backup"
    folder.mkdir()
    locked = folder / "arch_0640.dbf"
    locked.write_text("x")
    locked.chmod(0o000)
    marker = tmp_path / "sudo_was_called"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "sudo").write_text(f"#!/bin/sh\necho \"$@\" > {marker}\n")
    (fake / "sudo").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}

    try:
        done = subprocess.run(["sh", "-c", cli_backup_copy.make_readable_command(str(folder))], env=env)
    finally:
        locked.chmod(0o600)

    assert done.returncode == 0
    assert marker.read_text().split()[:4] == ["-n", "chmod", "-R", "a+rX"]


# --------------------------------------------------------------------------- #
# The repository's files are never chmod-ed by a restore
# --------------------------------------------------------------------------- #
class _Stop(Exception):
    """The first request is all this test reads."""


def _first_request(monkeypatch, *, restore_from):
    asked = []

    def run(op, request, **_kw):
        asked.append((op, request))
        raise _Stop

    monkeypatch.setattr(restore_script.common_cli, "run", run)
    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_k: {"host": "h"})
    monkeypatch.setattr(restore_script, "_repository_source", lambda job, **_k: (
        SimpleNamespace(container_name="", server_id="REPO"), "/mnt/nas/SRC", {}))
    job = SimpleNamespace(restore_id="R", label="R (sqlserver)", db_type="sqlserver", backup_dir="C:\\SQLBK",
                          restore_from=restore_from, source_backup_host_dir="/b", target_backup_dir="/s/R",
                          target_server_id="TGT", server_id="SRC", copy_selection="chain", copy_recent_hours=24,
                          staging_marker="", landing=None, space_check=restore_space.SpaceCheck(),
                          integrity=RestoreIntegrity(copy_retry_interval_seconds=0))
    with pytest.raises(_Stop):
        restore_script.transfer_backup_to_target(job, source=SimpleNamespace(container_name="", server_id="SRC"),
                                                 target=SimpleNamespace())
    return asked[0][1]


def test_a_restore_from_the_repository_leaves_the_repositorys_modes_alone(monkeypatch):
    request = _first_request(monkeypatch, restore_from="repository")

    assert request["source_dir"] == "/mnt/nas/SRC" and request["make_readable"] is False


def test_a_restore_from_the_server_may_still_open_a_file_just_written(monkeypatch):
    assert _first_request(monkeypatch, restore_from="server")["make_readable"] is True


def test_a_dry_run_changes_no_mode_anywhere(monkeypatch):
    asked = []

    def run(op, request, **_kw):
        asked.append(request)
        return {"files": [], "include": []}

    monkeypatch.setattr(restore_script.common_cli, "run", run)
    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_k: {"host": "h"})
    job = SimpleNamespace(restore_id="R", db_type="sqlserver", backup_dir="C:\\SQLBK", restore_from="server",
                          source_backup_host_dir="/b", target_backup_dir="/s/R", target_server_id="TGT",
                          copy_selection="chain", copy_recent_hours=24, staging_marker="", landing=None)

    restore_script.plan_transfer(job, source=SimpleNamespace(container_name="", server_id="SRC"),
                                 target=SimpleNamespace())

    assert asked[0]["make_readable"] is False
