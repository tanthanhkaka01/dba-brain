"""The space check counts the files the copy takes, read the way the copy reads them.

Found reading the code on 2026-10-01 and fixed on 2026-10-02 (review notes R8, S1). The check
walked the source **as a path**, whatever the copy did. A Linux node - the container worker, which
runs every scheduled restore - reads the share through ``smbclient``; to its Python a UNC path is
no folder at all. So the walk found nothing, the check counted **0 bytes**, said *fits*, and the
copy went ahead unmeasured: the rule that exists because an unmeasured copy filled a disk was, on
the one node that runs it nightly, never asked.

Two rules now:

* **one reading** - the check asks the copy's own engine for its selection and its sizes
  (``copy_backup.selected_backup_sizes``), so the number judged is the number moved;
* **"could not look" is not "nothing to copy"** - a source that cannot be read is unmeasured, and an
  unmeasured restore is held to ``space_check.on_unknown`` like any other.

And one check the Linux node did not have at all: what it fetches from the share lands in its own
temp folder before it goes to the target, on the disk a container worker shares with the runtime
store. That folder has to hold it.
"""

from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path

import pytest

from db_ops.backup_restore import copy_backup, space
from db_ops.backup_restore import share as share_module
from db_ops.backup_restore.config import BackupRestoreConfig, DatabaseRestoreMapping
from db_ops.lib import restore_space

GIB = 1024 ** 3

LISTING = (
    "\\PAYROLL_MAIN\\FULL\n"
    "  PAYROLL_MAIN_FULL_20260601_010000.bak      A  9000  Mon Jun 01 01:00:00 2026\n"
    "  PAYROLL_MAIN_FULL_20260608_010000.bak      A  4000  Mon Jun 08 01:00:00 2026\n"
    "\\PAYROLL_MAIN\\LOG\n"
    "  PAYROLL_MAIN_LOG_20260608_043000.trn       A  300  Mon Jun 08 04:30:00 2026\n"
)
FULL = "PAYROLL_MAIN/FULL/PAYROLL_MAIN_FULL_20260608_010000.bak"
LOG = "PAYROLL_MAIN/LOG/PAYROLL_MAIN_LOG_20260608_043000.trn"


def _entry(tmp_path: Path, **overrides) -> BackupRestoreConfig:
    values = dict(
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK"), vm_import_unc=Path("/opt/import/PAYROLL"),
        vm_import_local=Path("/opt/import/PAYROLL"), vm_log_unc=tmp_path / "log",
        vm_log_local=Path("/log"), prod_smb_credential_target="", prod_smb_username="",
        prod_smb_password_env="", vm_credential_target="192.0.2.252", vm_username="tuser",
        vm_password_env="", restore_sql_instance_on_vm="localhost,1433", vm_platform="linux",
        restore_id="DRILL", copy_recent_hours=192,
        databases=(DatabaseRestoreMapping("PAYROLL_MAIN", "Payroll_Main"),))
    values.update(overrides)
    return BackupRestoreConfig(**values)


def _linux_node(monkeypatch, *, listing: str = LISTING, staged: dict[str, int] | None = None) -> list:
    """This process as the container worker: the share answers ``smb-list``, the target a listing."""
    from db_ops.common import smb

    asked: list = []

    def answer(command, request, **_kwargs):
        asked.append((command, dict(request)))
        assert command == "smb-list", f"a measurement fetched nothing, but ran {command}"
        files = smb.parse_ls(listing, subpath=request.get("path") or "")
        return {"files": files, "count": len(files)}

    monkeypatch.setattr(copy_backup, "_running_on_linux", lambda: True)
    monkeypatch.setattr(share_module.common_cli, "run", answer)
    monkeypatch.setattr(copy_backup, "_remote_destination_sizes",
                        lambda config, *, logger=None: dict(staged or {}))
    return asked


# --------------------------------------------------------------------------- #
# One rule decides how the source is read
# --------------------------------------------------------------------------- #
def test_the_copy_and_the_check_choose_their_engine_by_one_rule(tmp_path, monkeypatch):
    share = Path(r"\\192.0.2.250\SQLBK")
    monkeypatch.setattr(copy_backup, "_running_on_linux", lambda: True)
    assert copy_backup.copy_engine(_entry(tmp_path)) == copy_backup.ENGINE_SMBCLIENT

    monkeypatch.setattr(copy_backup, "_running_on_linux", lambda: False)
    assert copy_backup.copy_engine(_entry(tmp_path)) == copy_backup.ENGINE_SFTP

    windows_target = _entry(tmp_path, vm_platform="windows", vm_import_unc=Path(r"\\192.0.2.129\IMPORT"))
    monkeypatch.setattr(copy_backup, "should_list_the_share", lambda config: True)
    assert copy_backup.copy_engine(windows_target) == copy_backup.ENGINE_SHARE
    monkeypatch.setattr(copy_backup, "should_list_the_share", lambda config: False)
    assert copy_backup.copy_engine(dataclasses.replace(windows_target, prod_backup_share=share)) \
        == copy_backup.ENGINE_PYTHON


# --------------------------------------------------------------------------- #
# A Linux node: the share is read through smb-list, as its copy reads it
# --------------------------------------------------------------------------- #
def test_a_linux_node_counts_the_chain_its_copy_takes_from_the_share(tmp_path, monkeypatch):
    """The run this is for: 0 bytes, *fits*, on every restore the worker ran."""
    asked = _linux_node(monkeypatch)

    measured = space.measure_copy(_entry(tmp_path))

    assert measured is not None and measured.to_write == 4000 + 300, \
        "the newest FULL and the LOG after it - not the older FULL, and not nothing"
    assert [(request["host"], request["share"], request["path"]) for _command, request in asked] == [
        ("192.0.2.250", "SQLBK", "PAYROLL_MAIN")], "the mapped folder, as the copy lists it"


def test_the_sizes_are_keyed_as_the_copy_lays_the_files_out_on_the_target(tmp_path, monkeypatch):
    _linux_node(monkeypatch)

    assert copy_backup.selected_backup_sizes(_entry(tmp_path)) == {FULL: 4000, LOG: 300}


def test_a_file_the_target_holds_is_not_counted_on_a_linux_node_either(tmp_path, monkeypatch):
    _linux_node(monkeypatch, staged={FULL.lower(): 4000})
    said: list[str] = []

    measured = space.measure_copy(_entry(tmp_path), report=said.append)

    assert (measured.to_write, measured.staged) == (300, 4000)
    assert said and "already staged" in said[0]


def test_a_share_that_cannot_be_listed_is_unmeasured_not_empty(tmp_path, monkeypatch):
    """"Could not look" read as "nothing to copy" is a check that passes on a disk it never saw."""
    _linux_node(monkeypatch)

    def refuse(command, request, **_kwargs):
        raise share_module.common_cli.CommonCliError("NT_STATUS_LOGON_FAILURE")

    monkeypatch.setattr(share_module.common_cli, "run", refuse)
    said: list[str] = []

    assert space.measure_copy(_entry(tmp_path), report=said.append) is None
    assert said and "could not be read" in said[0] and "NT_STATUS_LOGON_FAILURE" in said[0]


def test_an_unreadable_source_stops_the_restore_unless_the_entry_accepts_that(tmp_path, monkeypatch):
    _linux_node(monkeypatch)
    monkeypatch.setattr(share_module.common_cli, "run", lambda *_a, **_k: (_ for _ in ()).throw(
        share_module.common_cli.CommonCliError("NT_STATUS_BAD_NETWORK_NAME")))
    monkeypatch.setattr(space, "measure_target_free_bytes", lambda config: 500 * GIB)

    with pytest.raises(space.RestoreSpaceRefused) as caught:
        space.check_free_space(_entry(tmp_path), log=lambda _line: None)
    assert "could not read the size of the incoming files" in str(caught.value)

    accepting = _entry(tmp_path, space_check=restore_space.SpaceCheck(on_unknown="proceed"))
    assert space.check_free_space(accepting, log=lambda _line: None)["checked"] is False


# --------------------------------------------------------------------------- #
# A source read as a path
# --------------------------------------------------------------------------- #
def _path_entry(tmp_path: Path, **overrides) -> BackupRestoreConfig:
    values = dict(prod_backup_share=tmp_path / "share", vm_import_unc=tmp_path / "import",
                  vm_import_local=Path(r"E:\SQLBK_IMPORT"), vm_platform="windows",
                  vm_credential_target="", vm_username="")
    values.update(overrides)
    return _entry(tmp_path, **values)


def _backup(root: Path, kind: str, hours_ago: float, suffix: str, size: int) -> Path:
    when = time.time() - hours_ago * 3600
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(when))
    path = root / "PAYROLL_MAIN" / kind / f"PAYROLL_MAIN_{kind}_{stamp}Z.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (when, when))
    return path


def test_a_source_folder_that_is_not_there_is_unmeasured_not_empty(tmp_path):
    config = _path_entry(tmp_path)
    assert not config.prod_backup_share.exists()

    assert space.measure_copy(config) is None


def test_a_mapped_database_with_no_backups_yet_still_counts_nothing(tmp_path):
    """The share answers and this database has no folder on it: that is the restore's to report."""
    config = _path_entry(tmp_path)
    config.prod_backup_share.mkdir()

    assert space.measure_incoming_bytes(config) == 0


@pytest.mark.skipif(os.name != "nt", reason="a share-to-share copy is a Windows node's: UNC paths")
def test_a_windows_node_copying_share_to_share_counts_the_sizes_its_listing_states(tmp_path, monkeypatch):
    """That copy lists through ``smb-list`` too; the check walked the share a second time, by path."""
    config = _path_entry(tmp_path, prod_backup_share=Path(r"\\192.0.2.250\SQLBK"))
    monkeypatch.setattr(copy_backup, "should_list_the_share", lambda config: True)
    monkeypatch.setattr(copy_backup, "store_share_logins", lambda requests: None)
    recent = time.time() - 3600
    monkeypatch.setattr(share_module, "list_files", lambda unc, **_kw: [
        {"path": "FULL\\PAYROLL_MAIN_FULL_1.bak", "name": "PAYROLL_MAIN_FULL_1.bak",
         "modified_epoch": recent, "size_bytes": 4000}])
    staged = config.vm_import_unc / "PAYROLL_MAIN" / "FULL" / "PAYROLL_MAIN_FULL_1.bak"

    assert space.measure_incoming_bytes(config) == 4000

    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"x" * 4000)
    assert space.measure_incoming_bytes(config) == 0, "staged at its size, so the copy leaves it"


def test_a_listing_that_states_no_size_is_unmeasured(tmp_path, monkeypatch):
    config = _path_entry(tmp_path, prod_backup_share=Path(r"\\192.0.2.250\SQLBK"))
    monkeypatch.setattr(copy_backup, "should_list_the_share", lambda config: True)
    monkeypatch.setattr(copy_backup, "store_share_logins", lambda requests: None)
    monkeypatch.setattr(share_module, "list_files", lambda unc, **_kw: [
        {"path": "FULL\\PAYROLL_MAIN_FULL_1.bak", "name": "PAYROLL_MAIN_FULL_1.bak",
         "modified_epoch": time.time() - 3600}])

    assert space.measure_copy(config) is None


def test_a_windows_node_stores_the_share_login_before_it_looks(tmp_path, monkeypatch):
    """A share never opened in this session is not a folder until its login is stored - which the
    copy does first, and the check runs before the copy."""
    config = _path_entry(tmp_path)
    _backup(config.prod_backup_share, "FULL", 48, "bak", 4000)
    stored: list = []
    monkeypatch.setattr(copy_backup, "_running_on_linux", lambda: False)
    monkeypatch.setattr(copy_backup, "store_share_logins", stored.append)

    assert space.measure_incoming_bytes(config) == 4000
    assert stored == [[]], "asked once, with the logins this entry names - here none"


# --------------------------------------------------------------------------- #
# What a Linux node fetches lands on the node first
# --------------------------------------------------------------------------- #
def _node_free(monkeypatch, free: int | None) -> None:
    monkeypatch.setattr(space, "_local_free_bytes", lambda path: free)
    monkeypatch.setattr(space, "measure_target_free_bytes", lambda config: 500 * GIB)


def test_what_a_linux_node_fetches_passes_through_its_own_temp_folder(tmp_path, monkeypatch):
    _linux_node(monkeypatch, staged={FULL.lower(): 4000})

    assert space.measure_copy(_entry(tmp_path)).through_node == 300, "only what the target lacks"
    assert space.measure_copy(_entry(tmp_path), recopy=True).through_node == 4300, \
        "forced, the whole selection is fetched again"


def test_a_copy_through_a_path_puts_nothing_on_the_node(tmp_path):
    config = _path_entry(tmp_path)
    _backup(config.prod_backup_share, "FULL", 48, "bak", 4000)

    assert space.measure_copy(config).through_node == 0


def test_a_fetch_that_cannot_fit_on_the_node_is_refused_before_any_byte_moves(tmp_path, monkeypatch):
    _linux_node(monkeypatch)
    _node_free(monkeypatch, 4000)

    with pytest.raises(space.RestoreSpaceRefused) as caught:
        space.check_free_space(_entry(tmp_path), log=lambda _line: None)

    message = str(caught.value)
    assert "will not fit on this node" in message and "Nothing was copied" in message
    assert str(space.node_staging_dir()) in message, "the folder that is short, by name"


def test_a_fetch_that_fits_on_the_node_says_so_and_the_target_is_judged_as_before(tmp_path, monkeypatch):
    _linux_node(monkeypatch)
    _node_free(monkeypatch, 4300)
    said: list[str] = []

    result = space.check_free_space(_entry(tmp_path), log=said.append)

    assert result["ok"] is True and result["incoming_bytes"] == 4300
    assert any("on this node" in line and "fits" in line for line in said)


def test_a_node_folder_that_cannot_be_measured_is_said_and_does_not_stop_the_copy(tmp_path, monkeypatch):
    """A check that is new must not be what stops a restore that ran last night."""
    _linux_node(monkeypatch)
    _node_free(monkeypatch, None)
    said: list[str] = []

    assert space.check_free_space(_entry(tmp_path), log=said.append)["ok"] is True
    assert any("on this node" in line and "not measured" in line for line in said)
