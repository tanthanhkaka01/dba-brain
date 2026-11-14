"""The copy of a SQL Server restore takes the chains of the databases the entry restores, and no other (0.39.0, 1.362).

A server's backup folder - and its copy on the repository - holds every database that server backs
up. A restore entry names the ones it restores; the copy before it took the chain of **every**
database under the folder. Two things followed, both found reading the restore for 1.362:

* the others' chains travelled for nothing - and from an encrypted repository each of their files
  was decrypted on the way, the slow half of a restore;
* a point in time refused the whole copy when a database nobody had asked for had no full before the
  moment, though the one asked for did.

So the entry's statement (``database_mappings``) reaches the copy as ``chain.databases``, and
``prepare-staging`` chooses only those databases' files. A named database the folder does not hold
is said there, by name. ``restore_all_databases`` narrows nothing: every database is the restore's.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import restore_script
from dbabrain.common import backup_copy, cli_backup_copy
from dbabrain.lib.restore.landing import Landing

#: `find -printf '%P|%T@'` on a folder holding two databases; the name carries each backup's time.
FOLDER = "\n".join(f"{path}|1.0" for path in (
    "APP/FULL/APP_FULL_20261112_043500Z.bak",
    "APP/LOG/APP_LOG_20261112_050000Z.trn",
    "APP/LOG/APP_LOG_20261112_060000Z.trn",
    "HR/FULL/HR_FULL_20261112_053000Z.bak",
    "HR/LOG/HR_LOG_20261112_060000Z.trn",
    "_cert/db_ops_backup_cert.cer",
)) + "\n"


class _Out:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")


class _Source:
    def __init__(self, listing=FOLDER):
        self.listing = listing

    def open_stream(self, command, timeout_seconds=None):
        return None, _Out(self.listing), None


def _chain(databases=(), moment="", source=None):
    return set(backup_copy.chain_include("sqlserver", source or _Source(), source_dir="/mnt/nas/SRV",
                                         point_in_time=moment, databases=tuple(databases)))


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def test_a_named_database_brings_its_own_chain_and_no_other():
    assert _chain(["APP"]) == {"APP/FULL/APP_FULL_20261112_043500Z.bak", "APP/LOG/APP_LOG_20261112_050000Z.trn",
                               "APP/LOG/APP_LOG_20261112_060000Z.trn"}
    assert all(path.startswith("HR/") for path in _chain(["HR"]))


def test_with_no_database_named_every_database_travels_as_before():
    paths = _chain()

    assert {path.split("/")[0] for path in paths} == {"APP", "HR"}
    assert not any(path.startswith("_cert") for path in paths), "a certificate never travels"


def test_a_database_is_found_whatever_the_case_it_is_named_in():
    assert _chain(["app"]) == _chain(["APP"])


def test_a_point_in_time_is_not_refused_for_a_database_nobody_asked_for():
    """05:10Z is after APP's full and before HR's: HR has no chain at that moment."""
    moment = "2026-11-12 05:10:00 +00:00"

    with pytest.raises(backup_copy.ChainUnknownError, match="no FULL at or before the moment for HR"):
        _chain(moment=moment)
    assert _chain(["APP"], moment=moment) == {"APP/FULL/APP_FULL_20261112_043500Z.bak",
                                              "APP/LOG/APP_LOG_20261112_050000Z.trn",
                                              "APP/LOG/APP_LOG_20261112_060000Z.trn"}


def test_a_named_database_the_folder_does_not_hold_is_said_by_name():
    with pytest.raises(backup_copy.ChainUnknownError, match="no backup of SALES under /mnt/nas/SRV - the folder holds APP, HR"):
        _chain(["APP", "SALES"])


def test_a_folder_whose_layout_cannot_tell_databases_apart_is_not_copied_whole_for_a_named_one():
    flat = _Source("APP_FULL.bak|1.0\nHR_FULL.bak|1.0\n")

    assert _chain(source=flat) == set(), "nothing named: the whole directory, as before"
    with pytest.raises(backup_copy.ChainUnknownError, match="cannot be told from the others"):
        _chain(["APP"], source=flat)


@pytest.mark.parametrize("engine", ["postgresql", "oracle"])
def test_a_whole_instance_backup_has_no_chain_of_one_database(engine):
    with pytest.raises(backup_copy.ChainUnknownError, match="holds a whole cluster / instance"):
        backup_copy.chain_include(engine, _Source(), source_dir="/b", databases=("HR",))


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
def test_prepare_staging_reads_the_names_from_its_chain_object(monkeypatch):
    seen = {}
    monkeypatch.setattr(backup_copy, "chain_include",
                        lambda db_type, source, **kw: seen.update(kw) or ("APP/FULL/a.bak",))

    cli_backup_copy._chain({"chain": {"db_type": "sqlserver", "databases": [" APP ", "HR"]}}, object(), "/b")

    assert seen["databases"] == ("APP", "HR")


@pytest.mark.parametrize("databases", ["APP,HR", ["APP", ""], [1], {"APP": 1}])
def test_a_mistyped_list_is_refused(databases):
    with pytest.raises(ValueError, match="chain.databases must be a list of database names"):
        cli_backup_copy._chain({"chain": {"db_type": "sqlserver", "databases": databases}}, object(), "/b")


def test_the_request_is_one_the_reference_takes():
    from dbabrain.lib import request_check

    request = {"source": {"host": "h"}, "source_dir": "/b", "plan_only": True,
               "chain": {"db_type": "sqlserver", "point_in_time": "", "databases": ["APP"]}}
    assert request_check.refusal(request_check.check("prepare-staging", request)) == ""


# --------------------------------------------------------------------------- #
# The entry's statement reaches the copy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("landing, expected", [
    (Landing(names=(("APP", "APP"), ("HR", "HR_COPY")), all_databases=False), {"databases": ["APP", "HR"]}),
    (Landing(names=(), all_databases=True), {}),
    (None, {}),
])
def test_the_copy_is_told_the_databases_the_entry_names(landing, expected):
    """By the name the BACKUP carries - the folder knows nothing of the name it will land under."""
    assert restore_script._chain_databases(SimpleNamespace(landing=landing)) == expected


def test_the_dry_runs_plan_asks_for_the_same_chain(monkeypatch):
    asked = []

    def run(op, request, **_kw):
        asked.append(request)
        return {"files": [], "include": []}

    monkeypatch.setattr(restore_script.common_cli, "run", run)
    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_k: {"host": "h"})
    job = SimpleNamespace(restore_id="R", db_type="sqlserver", backup_dir="C:\\SQLBK", restore_from="server",
                          source_backup_host_dir="/b", target_backup_dir="/s/R", target_server_id="TGT",
                          copy_selection="chain", copy_recent_hours=24, staging_marker="",
                          landing=Landing(names=(("APP", "APP_COPY"),), all_databases=False))

    restore_script.plan_transfer(job, source=SimpleNamespace(container_name="", server_id="SRC"),
                                 target=SimpleNamespace())

    assert asked[0]["chain"]["databases"] == ["APP"]
