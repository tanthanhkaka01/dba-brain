"""A restore that worked deletes every copy its entry staged - decrypted or plain, this run's or an earlier one's (0.39.0, 1.371).

A restore lays its chain down in the target's staging: decrypted from the encrypted repository, or
copied plain from a source that was never encrypted (the server's own folder, a plain repository). A
run that FAILS keeps what it staged - a continued run reads it (1.372) - and the run that then
succeeded deleted only the decrypted files of its own chain: a file the failed run staged that the
later run did not need stayed for the entry's whole retention, a failed restore nobody ran again left
its whole chain there, and a plain copy always waited for the retention.

The operator, 2026-11-14 (translated): *each restore checks, and deletes them once it has restored the
DB successfully* - and on the plain copies from a source that was never encrypted: *delete them too*.
So a restore that worked asks the catalog for every copy THE SAME ENTRY's runs staged there and nobody
recorded as gone, and deletes each by the verified delete, held to the bytes and sha256 its copy
measured on the target.

What it must never take: another entry's files (a staging can be shared, and that entry may be in the
middle of its own run), a file nobody recorded, a file whose bytes are not what its copy recorded,
and anything while the restore has not worked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import restore_by_id, restore_script, verified_cleanup
from dbabrain.db import repository_encryption_store
from dbabrain.db.backup_catalog import BackupCatalogStore

# Its restore targets are lab machines the tests do not list in an inventory; a target whose
# environment nothing states is refused since 0.39.0 (D-39.6) - see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("restore_targets_are_lab")

A, B, C, D = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
REPO, REPO_DIR, STAGE = "REPO", "/mnt/nas/SRC", "/stage"


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


_STARTS = iter(range(10, 60))


def _run(catalog, restore_id="APP_TO_LAB", target="TGT"):
    return catalog.start_restore_run(action="restore", restore_id=restore_id, source_server_id="SRC",
                                     target_server_id=target, copy_max_retry=3,
                                     started_at=f"2026-11-13T{next(_STARTS):02d}:00:00Z")


def _file(catalog, run_id, name, sha, *, size=100, target="TGT", source=REPO, folder=STAGE, verdict="match",
          on_disk=1):
    return catalog.add_run_file(
        restore_run_id=run_id, source_server_id=source, target_server_id=target, engine="sqlserver",
        file_name=name, file_type="LOG", source_path=f"{REPO_DIR}/APP/LOG/{name}",
        target_path=f"{folder}/APP/LOG/{name}", target_host_path=f"{folder}/APP/LOG/{name}", copy_max_retry=3,
        size_source_bytes=size, size_target_bytes=size, checksum_source_before_copy=sha,
        checksum_target_after_copy=sha, target_on_disk=on_disk, target_checksum_matches=1, verdict=verdict)


# --------------------------------------------------------------------------- #
# The catalog: what an entry's runs staged and nobody recorded as gone
# --------------------------------------------------------------------------- #
def test_only_this_entrys_proven_files_still_on_disk_under_its_staging_are_listed(catalog):
    failed = _run(catalog)
    _file(catalog, failed, "L1.trn", A)
    _file(catalog, failed, "L2.trn", B, on_disk=0)                     # already recorded as gone
    _file(catalog, failed, "L3.trn", C, verdict="copy_failed_after_retries")  # never proven
    _file(catalog, failed, "L4.trn", D, folder="/elsewhere")           # not under this staging
    other_entry = _run(catalog, restore_id="HR_TO_LAB")
    _file(catalog, other_entry, "H1.trn", A)
    other_target = _run(catalog, target="TGT2")
    _file(catalog, other_target, "L5.trn", A, target="TGT2")

    rows = catalog.staged_by_entry(restore_id="APP_TO_LAB", target_server_id="TGT", folder=STAGE)

    assert [row["path"] for row in rows] == [f"{STAGE}/APP/LOG/L1.trn"]
    assert rows[0] | {} == {**rows[0], "size_bytes": 100, "sha256": A, "source_server_id": REPO,
                            "source_path": f"{REPO_DIR}/APP/LOG/L1.trn", "restore_run_id": failed}


@pytest.mark.parametrize("verdict", ["copied", "recopied"])
def test_a_file_copied_fresh_is_listed_as_a_proven_copy(catalog, verdict):
    """A plain copy from a source that was never encrypted is ``copied`` by the run that made it - read
    as ``match`` alone it was never listed, and the Oracle overwrite test on the lab host (2026-11-14) left
    its 11 files, 2.35 GB, in the clear after a restore that worked."""
    run = _run(catalog)
    _file(catalog, run, "FREE_L0_1.bkp", A, verdict=verdict)

    rows = catalog.staged_by_entry(restore_id="APP_TO_LAB", target_server_id="TGT", folder=STAGE)

    assert [(row["path"], row["sha256"]) for row in rows] == [(f"{STAGE}/APP/LOG/FREE_L0_1.bkp", A)]


def test_a_path_its_newest_row_says_is_gone_is_not_listed_whatever_an_older_row_says(catalog):
    """A failed run copied the file; the next run found it there (``match``), worked, deleted it and
    marked its own row gone. The failed run's row still said *on disk* - read past the newest row, the
    deleted file was listed again (80 SQL Server and 11 Oracle files on the lab host, 2026-11-14)."""
    failed, worked = _run(catalog), _run(catalog)
    _file(catalog, failed, "L1.trn", A, verdict="copied")
    _file(catalog, worked, "L1.trn", A, on_disk=0)
    _file(catalog, failed, "L2.trn", B, verdict="copied")              # not deleted by anyone: still staged

    rows = catalog.staged_by_entry(restore_id="APP_TO_LAB", target_server_id="TGT", folder=STAGE)

    assert [row["path"] for row in rows] == [f"{STAGE}/APP/LOG/L2.trn"]
    assert "on_disk" not in rows[0]


def test_the_run_that_is_cleaning_up_is_not_listed_and_a_path_staged_twice_is_its_newest_row(catalog):
    first, second, now = _run(catalog), _run(catalog), _run(catalog)
    _file(catalog, first, "L1.trn", A)
    newest = _file(catalog, second, "L1.trn", A)
    _file(catalog, now, "L9.trn", B)

    rows = catalog.staged_by_entry(restore_id="APP_TO_LAB", target_server_id="TGT", folder=STAGE + "/",
                                   exclude_run_ids=(now,))

    assert [(row["path"], row["run_file_id"]) for row in rows] == [(f"{STAGE}/APP/LOG/L1.trn", newest)]


# --------------------------------------------------------------------------- #
# Which copies: the entry's own, by what the catalog recorded
# --------------------------------------------------------------------------- #
def _row(name, sha, *, size=100, source=REPO, run_file_id=1, run=7):
    return {"path": f"{STAGE}/APP/LOG/{name}", "run_file_id": run_file_id, "restore_run_id": run,
            "source_server_id": source, "source_path": f"{REPO_DIR}/APP/LOG/{name}", "size_bytes": size,
            "sha256": sha}


JOB = SimpleNamespace(restore_id="APP_TO_LAB", target_server_id="TGT", target_backup_dir=STAGE + "/")
OWN = {"path": f"{STAGE}/APP/LOG/L9.trn", "size_bytes": 100, "sha256": A, "key_id": 1}
DECRYPTED = {"encrypted": True, "repository_server_id": REPO, "repository_dir": REPO_DIR + "/", "files": [OWN]}


def test_every_copy_the_catalog_recorded_for_the_entry_is_taken_with_its_facts_decrypted_or_plain():
    rows = [_row("L1.trn", A, run_file_id=1),                       # decrypted by an earlier run
            _row("L4.trn", D, source="SRC", run_file_id=4, run=9),  # copied plain from the server, this run
            _row("L9.trn", A, run_file_id=6)]                       # this run's own decrypted file: already held
    catalog = SimpleNamespace(staged_by_entry=lambda **kwargs: rows)

    copies = restore_script.staged_copies_of_entry(JOB, catalog, exclude_paths={OWN["path"]})

    assert [(one["path"], one["run_file_id"], one["restore_run_id"]) for one in copies] == [
        (f"{STAGE}/APP/LOG/L1.trn", 1, 7), (f"{STAGE}/APP/LOG/L4.trn", 4, 9)]
    assert copies[1]["sha256"] == D and copies[1]["size_bytes"] == 100


def test_the_catalog_is_asked_by_the_entry_the_target_and_the_staging():
    asked = []
    catalog = SimpleNamespace(staged_by_entry=lambda **kwargs: asked.append(kwargs) or [])

    assert restore_script.staged_copies_of_entry(JOB, catalog) == []
    assert asked == [{"restore_id": "APP_TO_LAB", "target_server_id": "TGT", "folder": STAGE}]


def test_with_no_catalog_or_no_staging_nothing_is_looked_for():
    assert restore_script.staged_copies_of_entry(JOB, None) == []
    catalog = SimpleNamespace(staged_by_entry=lambda **kwargs: pytest.fail("the catalog is not asked"))
    assert restore_script.staged_copies_of_entry(SimpleNamespace(**{**vars(JOB), "target_backup_dir": ""}), catalog) == []


# --------------------------------------------------------------------------- #
# The delete: an earlier run's file goes the same way, and its own row says so
# --------------------------------------------------------------------------- #
def test_an_earlier_runs_file_is_deleted_by_its_facts_and_its_own_row_is_marked_gone(monkeypatch):
    earlier = {"path": f"{STAGE}/APP/LOG/L1.trn", "size_bytes": 100, "sha256": A, "run_file_id": 31,
               "restore_run_id": 7}
    calls, marked = [], []

    def delete_files(**kwargs):
        calls.append(kwargs)
        return {"deleted": [OWN["path"]], "already_gone": [earlier["path"]], "not_deleted": [], "kept": []}

    monkeypatch.setattr(verified_cleanup, "delete_files", delete_files)
    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_: {"host": "t"})
    monkeypatch.setattr(restore_script, "_ssh_host_block", lambda login: {"host": "t"})
    catalog = SimpleNamespace(run_files=lambda run_id: [{"run_file_id": 90, "target_host_path": OWN["path"]}],
                              set_run_file=lambda run_file_id, **values: marked.append((run_file_id, values["target_on_disk"])))

    result = restore_script.delete_decrypted_staging(JOB, target=object(), files=[OWN, earlier], restore_run_id=9,
                                                     catalog=catalog)

    assert calls[0]["facts"][earlier["path"]] == {"size_bytes": 100, "sha256": A}
    assert result["plain_files"] == 2 and result["earlier_runs"] == 1
    assert sorted(marked) == [(31, 0), (90, 0)], "gone from the disk, said on both rows"


# --------------------------------------------------------------------------- #
# The run: only after it worked, and said in the chat
# --------------------------------------------------------------------------- #
EARLIER = [{"path": f"{STAGE}/APP/LOG/L1.trn", "size_bytes": 100, "sha256": A, "run_file_id": 31,
            "restore_run_id": 7}]


@pytest.fixture
def run(monkeypatch):
    from conftest import landing_of

    from dbabrain.backup_restore import backup as backup_module
    from dbabrain.backup_restore import restore_messages

    def start(*, fail=False, earlier=None, transferred=None):
        job = SimpleNamespace(
            restore_id="R", db_type="sqlserver", server_id="SRC", target_server_id="TGT", env_secrets={},
            is_remote=True, label="R (sqlserver)", landing=landing_of("APP"), overwrite_existing=False,
            integrity=None, tier=None, target_backup_dir=STAGE, cleanup_retention=86400,
            restore_from="server", server_metadata=SimpleNamespace(enabled=False))
        said, deleted, asked = [], [], []
        monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
        monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})
        monkeypatch.setattr(restore_by_id, "assert_target_is_not_source", lambda *a, **k: None)
        monkeypatch.setattr(restore_by_id, "_refuse_a_name_already_held", lambda *a, **k: {})
        monkeypatch.setitem(restore_by_id._PLANNERS, "sqlserver", lambda *a, **k: [
            {"op": "restore-full", "source_database": "APP", "chain": [{"path": f"{STAGE}/F.bak"}],
             "request": {"database_name": "APP", "backup_path": f"{STAGE}/F.bak", "with_recovery": True}},
            {"op": "verify-restore", "request": {}}])
        monkeypatch.setattr(backup_module, "resolve_ssh_target", lambda server_id, **_: SimpleNamespace(server_id=server_id))
        monkeypatch.setattr(restore_script, "transfer_backup_to_target",
                            lambda *a, **k: dict(transferred or {"copied": 1, "bytes_copied": 100, "skipped": 0,
                                                                 **DECRYPTED}))
        monkeypatch.setattr(restore_script, "prune_staged_backups", lambda *a, **k: {"pruned": 0})

        def find(job_, catalog, **kwargs):
            asked.append(kwargs)
            if isinstance(earlier, Exception):
                raise earlier
            return list(earlier or [])

        monkeypatch.setattr(restore_script, "staged_copies_of_entry", find)

        def delete_plain(job_, **kwargs):
            deleted.append(kwargs["files"])
            return {"plain_files": len(kwargs["files"]), "deleted": len(kwargs["files"]), "already_gone": 0,
                    "bytes_deleted": 200, **({"earlier_runs": 1} if len(kwargs["files"]) > 1 else {})}

        monkeypatch.setattr(restore_script, "delete_decrypted_staging", delete_plain)

        def execute(op, request):
            if fail and op == "restore-full":
                raise restore_by_id.RestoreByIdError("Msg 3201")
            return {"ok": True, "checked": 1, "failed": 0, "databases": []}

        monkeypatch.setattr(restore_by_id, "_execute", execute)
        outcome = None
        try:
            outcome = restore_by_id.restore_by_id({"restore_id": "R"},
                                                  on_phase=lambda phase, message, extra: said.append((phase, message, extra)))
        except restore_by_id.RestoreByIdError:
            pass
        return outcome, said, deleted, asked, restore_messages

    return start


def test_a_restore_that_worked_deletes_an_earlier_runs_plain_files_with_its_own_and_says_so(run):
    outcome, said, deleted, _asked, _messages = run(earlier=EARLIER)

    assert deleted == [[OWN, *EARLIER]]
    start = next(message for phase, message, _e in said if phase == "DELETE_START")
    assert "the 1 file(s) this restore staged and 1 an earlier run of this entry left" in start
    lines = next(extra["flow_lines"] for phase, _m, extra in said if phase == "DELETE_DONE")
    assert any("plain files 2/2 deleted on TGT" in line and "1 of them left by an earlier run" in line for line in lines)
    assert outcome["plain_deleted"]["earlier_runs"] == 1


def test_a_restore_that_failed_looks_for_nothing_and_deletes_nothing(run):
    outcome, said, deleted, asked, _messages = run(fail=True, earlier=EARLIER)

    assert outcome is None and deleted == [] and asked == []
    assert not [phase for phase, *_ in said if phase.startswith("DELETE")]


def test_a_catalog_that_cannot_be_read_leaves_the_restore_done_and_its_own_files_deleted(run):
    outcome, _said, deleted, _asked, _messages = run(earlier=OSError("the store is down"))

    assert deleted == [[OWN]]
    assert any(warning.startswith("staging: the copies this entry staged could not be read from the catalog - "
                                  "OSError: the store is down") for warning in outcome["warnings"])


def test_a_plain_copy_from_a_source_never_encrypted_is_deleted_too(run):
    """The operator, 2026-11-14: *delete them too*. Nothing was decrypted; the copies this run staged
    plain are the catalog's rows of this run, and they go as a decrypted chain does."""
    this_run = [{"path": f"{STAGE}/APP/FULL/F.bak", "size_bytes": 900, "sha256": B, "run_file_id": 50,
                 "restore_run_id": None}]
    outcome, said, deleted, asked, _messages = run(
        earlier=this_run, transferred={"copied": 1, "bytes_copied": 900, "skipped": 0, "encrypted": False,
                                       "files": [{"path": "APP/FULL/F.bak", "size_bytes": 900}]})

    assert deleted == [this_run]
    assert asked == [{"exclude_paths": set()}]
    start = next(message for phase, message, _e in said if phase == "DELETE_START")
    assert "the 1 file(s) this restore staged - the databases are restored and checked" in start
