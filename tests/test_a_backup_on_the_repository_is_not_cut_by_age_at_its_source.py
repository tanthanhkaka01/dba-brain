"""A backup that goes to the repository is not cut by age at its source - only what never goes there is (0.39.0, 1.373).

A SQL Server entry that releases its files once their repository copy is proven
(``offload_delete_source``) kept its backup script's age window as well (14 days on this estate), and
the window asked nothing of the repository: an offload stuck for longer than the window, and the
script deleted a file no copy had reached - the one way a backup could be lost.

The operator, 2026-11-14 (translated): *a backup that follows the repository is not cleaned the old
way any more - unless it does not go to the repository, and then by a cleanup retention stated at the
source backup*; and *keep the windows per file type, because some backups are not copied to the
repository*. So:

- an entry that releases, on SQL Server, hands its script ``RETENTION_ONLY_BEFORE`` - the moment from
  which its files go to the repository (``offload_files_since``): the age window, per type as stated,
  cuts only the files written before it; every later file is the release's;
- one that releases with no such moment, or on an engine whose script cannot bound its cut
  (PostgreSQL's chains), states ``keep`` for every type - refused by name otherwise;
- an entry that does not release keeps its own windows, per type, as before.

The scripts' retention blocks run here on real files with real ages, as the per-type test runs them.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from test_sqlserver_folders_are_pruned_by_their_own_window import (
    DAY, POWERSHELL, WINDOWS, _left, _run_ps1, _run_sh, _tree, needs_bash,
)

from dbabrain.backup_restore import spec_builder
from dbabrain.lib import cleanup_retention
from dbabrain.lib import offload as O

#: The files of _tree and their ages: FULL 20 / 10 / 1 days, DIFF 20 / 10, LOG 10 days and 2 hours.
FIFTEEN_DAYS_AGO = int(time.time()) - 15 * DAY


def _offload(*, enabled=True, delete_source=True, files_since="2026-11-11T04:16:00Z"):
    return SimpleNamespace(enabled=enabled, delete_source=delete_source, files_since=files_since)


# --------------------------------------------------------------------------- the rule
def test_an_entry_that_releases_on_sql_server_cuts_only_before_the_moment_its_files_go_to_the_repository():
    cut = O.source_age_cut_before(_offload(), db_type="sqlserver", keeps_every_type=False, label="E")
    assert cut == 1794370560, "2026-11-11T04:16:00Z"


@pytest.mark.parametrize("offload", [None, _offload(enabled=False, delete_source=False),
                                     _offload(delete_source=False)])
def test_an_entry_that_does_not_release_keeps_its_own_window_unbounded(offload):
    assert O.source_age_cut_before(offload, db_type="sqlserver", keeps_every_type=False, label="E") is None


@pytest.mark.parametrize("db_type, files_since, why", [
    ("sqlserver", "", "state offload_files_since"),
    ("postgresql", "2026-11-11T04:16:00Z", "state cleanup_retention as keep for every file type"),
])
def test_an_entry_that_releases_and_cannot_bound_its_cut_states_keep_or_is_refused(db_type, files_since, why):
    with pytest.raises(O.OffloadConfigError, match=why):
        O.source_age_cut_before(_offload(files_since=files_since), db_type=db_type, keeps_every_type=False, label="E")
    assert O.source_age_cut_before(_offload(files_since=files_since), db_type=db_type,
                                   keeps_every_type=True, label="E") is None


def _job(db_type, retention, offload):
    return SimpleNamespace(db_type=db_type, retention=retention, offload=offload, label=f"E/{db_type}")


def test_the_job_reads_keep_off_its_own_windows_for_its_engines_types():
    keep_all = cleanup_retention.Retention(default=None)
    some = cleanup_retention.Retention(windows={"WAL": 86400}, default=None)

    assert spec_builder._source_age_cut_before(_job("postgresql", keep_all, _offload(files_since=""))) is None
    with pytest.raises(O.OffloadConfigError):
        spec_builder._source_age_cut_before(_job("postgresql", some, _offload(files_since="")))
    assert spec_builder._source_age_cut_before(
        _job("sqlserver", cleanup_retention.Retention(default=14 * DAY, uniform=True), _offload())) == 1794370560


# --------------------------------------------------------------------------- the Linux script
@needs_bash
def test_bash_per_type_windows_cut_only_the_files_before_the_moment(tmp_path):
    db = _tree(tmp_path)
    done = _run_sh(tmp_path, {**WINDOWS, "RETENTION_ONLY_BEFORE": str(FIFTEEN_DAYS_AGO)})

    assert done.returncode == 0, done.stderr
    # The 20-day FULL and DIFF were written before the moment: the window cuts them. The 10-day LOG is
    # past its 7 days, but written after the moment - the repository copy's to release, kept here.
    assert _left(db) == {"FULL/f_mid.bak", "FULL/f_new.bak", "DIFF/d_mid.bak", "LOG/l_old.trn", "LOG/l_new.trn"}
    assert "the newer are the repository copy's to release" in done.stdout


@needs_bash
def test_bash_the_one_window_form_is_bounded_the_same_way(tmp_path):
    db = _tree(tmp_path)
    done = _run_sh(tmp_path, {"RETENTION_ONLY_BEFORE": str(FIFTEEN_DAYS_AGO)})

    assert done.returncode == 0, done.stderr
    assert _left(db) == {"FULL/f_mid.bak", "FULL/f_new.bak", "DIFF/d_mid.bak", "LOG/l_old.trn", "LOG/l_new.trn"}


@needs_bash
def test_bash_without_the_moment_cuts_as_before(tmp_path):
    db = _tree(tmp_path)
    assert _run_sh(tmp_path, WINDOWS).returncode == 0
    assert "LOG/l_old.trn" not in _left(db)


@needs_bash
def test_bash_refuses_a_moment_that_is_not_epoch_seconds(tmp_path):
    _tree(tmp_path)
    assert _run_sh(tmp_path, {**WINDOWS, "RETENTION_ONLY_BEFORE": "2026-11-11"}).returncode == 3


# --------------------------------------------------------------------------- the Windows script
@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell only")
def test_powershell_cuts_only_the_files_before_the_moment_and_refuses_a_bad_one(tmp_path):
    db = _tree(tmp_path / "a")
    done = _run_ps1(tmp_path / "a", {**WINDOWS, "RETENTION_ONLY_BEFORE": str(FIFTEEN_DAYS_AGO)})
    assert done.returncode == 0, done.stdout + done.stderr
    assert _left(db) == {"FULL/f_mid.bak", "FULL/f_new.bak", "DIFF/d_mid.bak", "LOG/l_old.trn", "LOG/l_new.trn"}
    assert "the newer are the repository copy's to release" in done.stdout

    _tree(tmp_path / "b")
    assert _run_ps1(tmp_path / "b", {**WINDOWS, "RETENTION_ONLY_BEFORE": "soon"}).returncode == 3
