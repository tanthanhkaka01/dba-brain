"""A staging filled by decryption is prepared like one filled by a copy (0.39.0, 1.355).

Read on 2026-11-13, mapping the restore half of the new flow: a restore from an encrypted repository
copy called `prepare-staging` only to read the chain at the repository, then decrypted each `.age`
straight into the staging folder. Three things a plain copy gets were skipped - the free space was
never checked, the folder was never marked as this tool's (so its cleanup kept it and only said why),
and `finish-staging` never opened it to the engine, whose own uid reads it (10001 in a SQL Server
container).

`prepare-staging` now takes `incoming` - the files the caller already knows, plain name and bytes from
the store - and does the target half alone: folder made and proven writable, marked by the same rule,
parents made, the files not there yet held to the free space. Nothing is copied or removed.
"""

from __future__ import annotations

import pytest

from tests.test_a_removed_backup_set_leaves_no_husk import _Session, _Sftp

from dbabrain.common import backup_copy, cli_backup_copy
from dbabrain.lib import restore_space

INCOMING = [("APP/FULL/APP_FULL_1.bak", 5000), ("APP/LOG/APP_LOG_2.trn", 300)]


def _prepare(target, **more):
    return backup_copy.prepare_target(target_session=_Session(target), target_dir="/stage", incoming=INCOMING, **more)


def test_an_empty_target_is_made_marked_and_given_every_parent_folder():
    target = _Sftp({}, {"/"})
    result = _prepare(target)
    assert "/stage/" + backup_copy.STAGING_MARKER in target.files
    assert {"/stage", "/stage/APP/FULL", "/stage/APP/LOG"} <= target.dirs
    assert result.pending == [("APP/FULL/APP_FULL_1.bak", 5000, None), ("APP/LOG/APP_LOG_2.trn", 300, None)]
    assert target.deletes == [], "nothing removed"


def test_a_file_already_there_at_its_size_is_not_counted_again():
    target = _Sftp({"/stage/" + backup_copy.STAGING_MARKER: 1, "/stage/APP/FULL/APP_FULL_1.bak": 5000},
                   {"/", "/stage", "/stage/APP", "/stage/APP/FULL"})
    result = _prepare(target)
    assert result.skipped == 1 and [rel for rel, _s, _m in result.pending] == ["APP/LOG/APP_LOG_2.trn"]


def test_an_unmarked_folder_holding_someone_elses_files_is_refused():
    target = _Sftp({"/stage/payroll.xlsx": 10}, {"/", "/stage"})
    with pytest.raises(Exception, match="staging"):
        _prepare(target)
    assert "/stage/" + backup_copy.STAGING_MARKER not in target.files


def test_files_that_do_not_fit_stop_it_before_anything_is_decrypted(monkeypatch):
    monkeypatch.setattr(backup_copy, "_target_free_bytes", lambda session, path: 6000)
    rule = restore_space.parse_space_check({"space_check": {"enabled": True, "factor": 2.0}})
    with pytest.raises(backup_copy.CopySpaceError, match="will not fit"):
        _prepare(_Sftp({}, {"/"}), space_check=rule)
    monkeypatch.setattr(backup_copy, "_target_free_bytes", lambda session, path: 20000)
    assert _prepare(_Sftp({}, {"/"}), space_check=rule).space["ok"] is True


@pytest.mark.parametrize("request_, words", [
    ({"incoming": [{"path": "a.bak", "size_bytes": 1}], "source_dir": "/r"}, "belong to a request that reads"),
    ({"incoming": [], "target_dir": "/stage"}, "non-empty list"),
    ({"incoming": [{"path": "/abs.bak", "size_bytes": 1}], "target_dir": "/stage"}, "relative to target_dir"),
    ({"incoming": [{"path": "../up.bak", "size_bytes": 1}], "target_dir": "/stage"}, "relative to target_dir"),
    ({"incoming": [{"path": "a.bak", "size_bytes": "1"}], "target_dir": "/stage"}, "whole number of bytes"),
    ({"incoming": [{"path": "a.bak", "size_bytes": 1}]}, "target_dir"),
])
def test_a_request_with_incoming_is_held_to_its_own_shape_before_a_host_is_reached(request_, words, monkeypatch):
    monkeypatch.setattr(cli_backup_copy, "_open", lambda *a, **k: pytest.fail("no host may be reached"))
    with pytest.raises(Exception, match=words):
        cli_backup_copy._prepare_staging(request_)


def test_an_encryption_left_half_done_on_the_repository_is_never_part_of_a_chain():
    """`<file>.age.<pid>.partial` is what a killed encryption leaves (0.38.0, 1.331) - kept there for the
    operator. Selected, it refused the restore as a file no offload proved (0.39.0, 1.360)."""
    files = [("APP/LOG/APP_LOG_1.trn.age", 10), ("APP/LOG/APP_LOG_2.trn.age.4711.partial", 4),
             ("APP/LOG/APP_LOG_3.trn.dbabrain_partial", 4)]
    selected, _dirs = backup_copy._selected(files, [], {}, include=(), window_since=None)
    assert selected == [("APP/LOG/APP_LOG_1.trn.age", 10)]
