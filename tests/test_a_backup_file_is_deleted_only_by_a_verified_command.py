"""A backup file is deleted only by a verified command - one file per command, never a list of paths.

Until 0.38.0 five things deleted backup files without holding them to anything: ``delete-file`` /
``delete-files`` (a path, or a list of paths, one ``rm`` each), ``smb-delete`` (a list, through a
share), ``prune-backup-files`` with ``delete``, and ``prune-staged-backups`` - one ``find -delete``
for a staging folder's files and one ``rm -rf`` for an emptied backup set. The operator, 2026-11-10:
*deleting this file is very dangerous*; and 2026-11-11 (translated): *the old delete commands refuse
to run; but with force a file can still be deleted, to clear junk too - one file at a time: two
files or more fail outright, a folder that still holds a file fails, and to delete many, make an
array and call the delete once per file.*

So (1.329): every cleanup in the app - a backup entry's prune, a restore's staging cleanup, the
target import folder's - deletes a file the catalog recorded by ``delete-verified-files``, with its
name, path, bytes and sha256; **a file the catalog does not know is kept and named**; an operator
clears one stray file with ``delete-file`` and ``"force": true``, which measures it on the host,
deletes it and answers with its bytes and sha256. These tests hold each of those, the forbidden
cases included.
"""
from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import delete_backup, prune, restore_script, verified_cleanup
from dbabrain.common import cli_backup_copy, cli_backup_files, cli_delete_files, deleteverified, listfiles, smb
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import verified_cleanup as rules

SHA = "a" * 64
LOCAL = {"runtime": "windows" if os.name == "nt" else "linux"}


# ============================================================================ the rules (lib)
def test_a_file_the_catalog_knows_becomes_one_item_with_all_four_facts():
    items, kept = rules.items_with_facts(
        ["/bk/APP/FULL/APP_FULL_1.bak"], {"/bk/APP/FULL/APP_FULL_1.bak": {"size_bytes": 10, "sha256": SHA}},
        must_be_under="/bk")
    assert kept == []
    assert items == [{"path": "/bk/APP/FULL/APP_FULL_1.bak", "file_name": "APP_FULL_1.bak", "size_bytes": 10,
                      "sha256": SHA, "must_be_under": "/bk"}]


def test_a_file_the_catalog_does_not_know_is_kept_and_named():
    items, kept = rules.items_with_facts(["/bk/stray.tmp"], {}, must_be_under="/bk")
    assert items == []
    assert kept == [{"path": "/bk/stray.tmp", "reason": rules.NOT_IN_THE_CATALOG}]


@pytest.mark.parametrize("facts", [{"size_bytes": None, "sha256": SHA}, {"size_bytes": 10, "sha256": ""},
                                   {"size_bytes": 10, "sha256": "zz"}, {"size_bytes": True, "sha256": SHA},
                                   {"size_bytes": -1, "sha256": SHA}])
def test_a_record_without_whole_facts_is_no_licence_to_delete(facts):
    items, kept = rules.items_with_facts(["/bk/a.bak"], {"/bk/a.bak": facts}, must_be_under="/bk")
    assert items == [] and kept == [{"path": "/bk/a.bak", "reason": rules.NO_FACTS}]


def test_a_windows_host_matches_a_path_whatever_its_case_or_slashes():
    facts = {"D:/Import/APP/a.bak": {"size_bytes": 1, "sha256": SHA}}
    items, kept = rules.items_with_facts(["d:\\import\\app\\A.BAK"], facts, must_be_under="D:\\Import", windows=True)
    assert len(items) == 1 and kept == []
    # ... and a Linux host does not: another case is another file.
    items, kept = rules.items_with_facts(["/bk/A.bak"], {"/bk/a.bak": {"size_bytes": 1, "sha256": SHA}},
                                         must_be_under="/bk")
    assert items == [] and len(kept) == 1


def _entry(path, mtime, kind="file"):
    return {"path": path, "type": kind, "size_bytes": 1, "mtime_utc": mtime}


def test_only_regular_files_written_before_the_window_are_chosen():
    now = rules.epoch_of("2026-11-11T00:00:00Z")
    entries = [_entry("/st/old.bak", "2026-11-01T00:00:00Z"), _entry("/st/new.bak", "2026-11-10T23:00:00Z"),
               _entry("/st/.dbabrain-staging", "2026-01-01T00:00:00Z"),
               _entry("/st/link", "2026-01-01T00:00:00Z", "link"), _entry("/st/odd", "not a time")]
    chosen = rules.past_retention(entries, retention_seconds=86400, now=now, keep_names=[".dbabrain-staging"])
    assert chosen == ["/st/old.bak"]
    # No window stated: nothing is past it.
    assert rules.past_retention(entries, retention_seconds=0, now=now) == []


def test_an_emptied_set_goes_with_its_folders_deepest_first():
    folders = ["/st/base", "/st/base/20261101_FULL", "/st/base/20261101_FULL/pg_tblspc",
               "/st/base/20261101_FULL/pg_wal", "/st/base/20261108_FULL", "/st/base/20261108_FULL/pg_tblspc"]
    left = ["/st/.dbabrain-staging", "/st/base/20261108_FULL/PG_VERSION"]
    assert rules.husk_folders(folders, left, root="/st") == [
        "/st/base/20261101_FULL/pg_tblspc", "/st/base/20261101_FULL/pg_wal", "/st/base/20261101_FULL"]


def test_a_folder_empty_by_design_inside_a_live_set_stays():
    # pg_tblspc of the newest base backup is empty and is part of that backup.
    folders = ["/st/base", "/st/base/20261108_FULL", "/st/base/20261108_FULL/pg_tblspc"]
    assert rules.husk_folders(folders, ["/st/base/20261108_FULL/PG_VERSION"], root="/st") == []


def test_a_link_left_in_a_set_counts_as_something_and_the_root_is_never_named():
    assert rules.husk_folders(["/st/set"], ["/st/set/a-link"], root="/st") == []
    assert "/st" not in rules.husk_folders(["/st/set"], [], root="/st")


def test_an_item_the_answer_does_not_mention_is_not_assumed_deleted():
    items = [{"path": "/bk/a"}, {"path": "/bk/b"}, {"path": "/bk/c"}]
    answer = {"files": [{"path": "/bk/a", "status": "deleted"}, {"path": "/bk/b", "status": "not_found"}]}
    read = rules.read_answer(answer, items, error="the list stopped")
    assert (read["deleted"], read["already_gone"]) == (["/bk/a"], ["/bk/b"])
    assert read["not_deleted"] == [{"path": "/bk/c", "status": "not answered", "reason": "the list stopped"}]


# ============================================================================ the commands (common)
def test_delete_file_without_force_is_refused_and_names_the_verified_delete(tmp_path):
    victim = tmp_path / "a.bak"
    victim.write_bytes(b"x")
    with pytest.raises(deleteverified.DeleteVerifiedError, match="delete-verified-file"):
        deleteverified.delete_forced_file({"host": LOCAL, "path": str(victim), "must_be_under": str(tmp_path)})
    with pytest.raises(deleteverified.DeleteVerifiedError, match="force"):
        deleteverified.delete_forced_file({"host": LOCAL, "path": str(victim), "must_be_under": str(tmp_path),
                                           "force": "yes"})
    assert victim.exists()


def test_a_forced_delete_removes_one_file_and_records_what_it_was(tmp_path):
    victim = tmp_path / "stray.partial"
    victim.write_bytes(b"junk-bytes")
    answer = deleteverified.delete_forced_file({"host": LOCAL, "path": str(victim),
                                                "must_be_under": str(tmp_path), "force": True})
    assert not victim.exists()
    row = answer["file"]
    assert (answer["deleted"], answer["forced"], row["status"]) == (True, True, "deleted")
    assert (row["size_bytes_found"], row["sha256_found"]) == (10, hashlib.sha256(b"junk-bytes").hexdigest())


def test_two_files_in_one_forced_command_fail_and_delete_nothing(tmp_path):
    one, two = tmp_path / "a.bak", tmp_path / "b.bak"
    one.write_bytes(b"1")
    two.write_bytes(b"2")
    for request in ({"paths": [str(one), str(two)]}, {"files": [str(one), str(two)]},
                    {"path": str(one), "paths": [str(two)]}):
        with pytest.raises(deleteverified.DeleteVerifiedError, match="ONE file per command"):
            deleteverified.delete_forced_file({"host": LOCAL, "must_be_under": str(tmp_path), "force": True, **request})
    assert one.exists() and two.exists()


def test_a_forced_delete_never_takes_a_folder_empty_or_not(tmp_path):
    full, empty = tmp_path / "set", tmp_path / "empty"
    full.mkdir()
    empty.mkdir()
    (full / "a.bak").write_bytes(b"x")
    for folder in (full, empty):
        row = deleteverified.delete_forced_file({"host": LOCAL, "path": str(folder),
                                                 "must_be_under": str(tmp_path), "force": True})["file"]
        assert row["status"] == "refused" and "folder" in row["reason"]
    assert (full / "a.bak").exists() and empty.exists()


def test_a_forced_delete_is_fenced_takes_no_pattern_and_dry_runs(tmp_path):
    victim = tmp_path / "in" / "a.bak"
    victim.parent.mkdir()
    victim.write_bytes(b"x")
    with pytest.raises(deleteverified.DeleteVerifiedError, match="not inside must_be_under"):
        deleteverified.delete_forced_file({"host": LOCAL, "path": str(victim),
                                           "must_be_under": str(tmp_path / "other"), "force": True})
    with pytest.raises(deleteverified.DeleteVerifiedError, match="not a pattern"):
        deleteverified.delete_forced_file({"host": LOCAL, "path": str(tmp_path / "in" / "*.bak"),
                                           "must_be_under": str(tmp_path), "force": True})
    row = deleteverified.delete_forced_file({"host": LOCAL, "path": str(victim), "must_be_under": str(tmp_path),
                                             "force": True, "dry_run": True})["file"]
    assert row["status"] == "skipped" and row["sha256_found"] == hashlib.sha256(b"x").hexdigest()
    assert victim.exists()
    gone = deleteverified.delete_forced_file({"host": LOCAL, "path": str(tmp_path / "in" / "never.bak"),
                                              "must_be_under": str(tmp_path), "force": True})["file"]
    assert gone["status"] == "not_found"


def test_the_forced_script_is_the_verified_script_less_its_two_comparisons():
    for script in (deleteverified.posix_file_script, deleteverified.windows_file_script):
        held = script("/bk/a.bak", size=5, sha256=SHA, dry_run=False)
        forced = script("/bk/a.bak", size=None, sha256=None, dry_run=False)
        assert SHA in held and SHA not in forced
        for word in ("LINK", "DIR", "MISSING", "CHANGED", "DELETED", "UNREADABLE"):
            assert word in held and word in forced, word
        assert "SIZE" in held and "SIZE" not in forced
    # One file, never recursive, on either platform.
    assert "rm -- \"$p\"" in deleteverified.posix_file_script("/bk/a", size=None, sha256=None, dry_run=False)
    assert "rm -r" not in deleteverified.posix_file_script("/bk/a", size=None, sha256=None, dry_run=False)


def _answer(capsys):
    return json.loads(capsys.readouterr().out)


def test_delete_files_is_refused_whatever_it_carries(capsys, tmp_path):
    victim = tmp_path / "a.bak"
    victim.write_bytes(b"x")

    # Read like any request - a malformed one is still called malformed - and then refused.
    request = {"host": LOCAL, "paths": [str(victim)], "must_be_under": str(tmp_path), "force": True}
    code = cli_delete_files.run("delete-files", ["-"], read_request=lambda _arg, _usage: (request, 0))
    answer = _answer(capsys)
    assert code != 0 and answer["success"] is False
    assert "one command never deletes two files" in answer["error"] and "delete-verified-file" in answer["error"]
    assert victim.exists()


def test_prune_staged_backups_is_refused_before_anything_is_reached(capsys, monkeypatch):
    def never_opened(*_args, **_kwargs):
        raise AssertionError("no host may be reached")

    monkeypatch.setattr(cli_backup_copy, "_open", never_opened)
    request = {"target": {"host": "192.0.2.40", "username": "u", "password": "p"}, "target_dir": "/st",
               "cleanup_retention": 60}
    code = cli_backup_copy.run("prune-staged-backups", ["-"], read_request=lambda _arg, _usage: (request, 0))
    answer = _answer(capsys)
    assert code != 0 and "refused" in answer["error"] and "delete-verified-files" in answer["error"]


def test_prune_backup_files_plans_and_refuses_to_delete(capsys, monkeypatch):
    from dbabrain.common import backupfiles

    # Two fulls: a database's newest full is never obsolete, whatever its age.
    listed = {"files": [{"path": "/bk/APP_FULL_20200101.bak", "kind": "full", "database_name": "APP",
                         "size_bytes": 5, "finished_at": "2020-01-01 00:00:00"},
                        {"path": "/bk/APP_FULL_20200601.bak", "kind": "full", "database_name": "APP",
                         "size_bytes": 5, "finished_at": "2020-06-01 00:00:00"}]}
    monkeypatch.setattr(backupfiles, "list_backup_files", lambda request: listed)
    request = {"db_type": "oracle", "path": "/bk", "cleanup_retention": 86400}
    assert cli_backup_files._prune(dict(request)) == 0
    planned = _answer(capsys)
    assert planned["data"]["obsolete_paths"] == ["/bk/APP_FULL_20200101.bak"] and "deleted" not in planned["data"]
    assert cli_backup_files._prune({**request, "delete": True}) != 0
    refused = _answer(capsys)
    assert refused["success"] is False and "no longer deletes" in refused["error"]
    # The plan is still what the refusal carries: what to delete, each on its own, is the next step.
    assert refused["data"]["obsolete_paths"] == ["/bk/APP_FULL_20200101.bak"]


def test_smb_delete_is_forced_one_file_and_never_a_pattern():
    share = {"host": "192.0.2.9", "share": "S", "username": "u", "password": "p", "backend": "smbclient"}
    with pytest.raises(smb.SmbError, match="refused"):
        smb.delete({**share, "paths": ["a.tmp"]})
    with pytest.raises(smb.SmbError, match="ONE file per command; got 2"):
        smb.delete({**share, "paths": ["a.tmp", "b.tmp"], "force": True})
    with pytest.raises(smb.SmbError, match="never a pattern"):
        smb.delete({**share, "paths": ["*.tmp"], "force": True})


def test_the_old_path_list_delete_holds_no_code():
    from dbabrain.common import deletefiles

    assert not [name for name in vars(deletefiles) if not name.startswith("__")]
    from dbabrain.common import backup_copy

    assert not hasattr(backup_copy, "prune_target_dir")
    source = open(backup_copy.__file__, encoding="utf-8").read()
    assert "rm -rf" not in source.replace("`rm -rf`", "") or "find" not in source.split("rm -rf")[0][-200:]


def test_list_files_answers_the_folders_only_when_asked(tmp_path):
    (tmp_path / "set" / "empty").mkdir(parents=True)
    (tmp_path / "set" / "a.bak").write_bytes(b"x")
    plain = listfiles.list_files({"folder": str(tmp_path)})
    assert "folders" not in plain and plain["listed"] == 1
    asked = listfiles.list_files({"folder": str(tmp_path), "with_folders": True})
    assert [os.path.relpath(path, tmp_path) for path in asked["folders"]] == ["set", os.path.join("set", "empty")]
    assert asked["listed"] == 1 and [entry["type"] for entry in asked["files"]] == ["file"]
    assert "-type d" not in listfiles.posix_command("/x", with_folders=True)
    assert "! -type d" in listfiles.posix_command("/x")


# ============================================================================ the app's one way to delete
class _Common:
    """Stands in for ``common.cli``: records every call, answers from a table."""

    def __init__(self, statuses=None, listing=None):
        self.calls: list[tuple[str, dict]] = []
        self.statuses = statuses or {}
        self.listing = listing

    def run_allowing_failure(self, command, request, **_kwargs):
        self.calls.append((command, request))
        if command == "delete-verified-files":
            return True, {"files": [{"path": item["path"], "status": self.statuses.get(item["path"], "deleted"),
                                     "reason": "the checksum on the host is not the one stated"}
                                    for item in request["files"]]}, ""
        if command == "delete-empty-folders":
            return True, {"folders": [{"path": item["path"], "status": "deleted"} for item in request["folders"]]}, ""
        if command == "delete-file":
            return True, {"file": {"path": request["path"], "status": "deleted", "size_bytes_found": 7,
                                   "sha256_found": "b" * 64}}, ""
        raise AssertionError(f"unexpected command {command}")

    def run(self, command, request, **_kwargs):
        self.calls.append((command, request))
        if command == "list-files":
            return self.listing
        raise AssertionError(f"unexpected command {command}")

    def named(self, command):
        return [request for name, request in self.calls if name == command]


@pytest.fixture()
def common(monkeypatch):
    fake = _Common()
    monkeypatch.setattr(verified_cleanup.common_cli, "run_allowing_failure", fake.run_allowing_failure)
    monkeypatch.setattr(restore_script.common_cli, "run", fake.run)
    return fake


HOST = {"runtime": "linux", "access": "ssh", "host": "192.0.2.40", "username": "u", "password": "p"}


def test_known_files_go_in_one_verified_request_and_an_unknown_one_is_kept(common):
    facts = {"/bk/a.bak": {"size_bytes": 3, "sha256": SHA}}
    cleanup = verified_cleanup.delete_files(host=HOST, paths=["/bk/a.bak", "/bk/stray.tmp"], facts=facts,
                                            must_be_under="/bk")
    assert [name for name, _request in common.calls] == ["delete-verified-files"]
    assert common.named("delete-verified-files")[0]["files"] == [
        {"path": "/bk/a.bak", "file_name": "a.bak", "size_bytes": 3, "sha256": SHA, "must_be_under": "/bk"}]
    assert cleanup["deleted"] == ["/bk/a.bak"]
    assert cleanup["kept"] == [{"path": "/bk/stray.tmp", "reason": rules.NOT_IN_THE_CATALOG}]
    assert "1 kept - not in the catalog (stray.tmp)" in verified_cleanup.summary_line(cleanup)


def test_force_deletes_each_unknown_file_by_its_own_command(common):
    cleanup = verified_cleanup.delete_files(host=HOST, paths=["/bk/s1.tmp", "/bk/s2.tmp"], facts={},
                                            must_be_under="/bk", force=True)
    forced = common.named("delete-file")
    # An array here, a call per file there: no forced request ever names two files.
    assert [request["path"] for request in forced] == ["/bk/s1.tmp", "/bk/s2.tmp"]
    assert all(request["force"] is True and "paths" not in request and "files" not in request for request in forced)
    assert cleanup["kept"] == [] and [one["path"] for one in cleanup["forced"]] == ["/bk/s1.tmp", "/bk/s2.tmp"]
    assert cleanup["forced"][0]["sha256"] == "b" * 64            # what was removed is on record


def test_a_file_that_is_not_what_was_recorded_is_reported_never_forced(common):
    common.statuses = {"/bk/a.bak": "refused"}
    cleanup = verified_cleanup.delete_files(host=HOST, paths=["/bk/a.bak"],
                                            facts={"/bk/a.bak": {"size_bytes": 3, "sha256": SHA}},
                                            must_be_under="/bk", force=True)
    assert common.named("delete-file") == []                      # force is for the unknown, not the changed
    assert cleanup["deleted"] == [] and cleanup["not_deleted"][0]["status"] == "refused"


# ---------------------------------------------------------------------------- a backup entry's prune
def test_the_catalog_answers_only_for_a_file_at_the_path_it_was_recorded_at():
    rows = {"APP_FULL_1.bak": {"file_path": "/bk/APP/FULL/APP_FULL_1.bak", "host_path": "/srv/bk/APP/FULL/APP_FULL_1.bak",
                               "size_bytes": 9, "checksum_after_backup": SHA}}
    catalog = SimpleNamespace(stored_files=lambda **_kwargs: rows)
    here = prune.catalog_facts(catalog, server_id="SRV-01", paths=["/bk/APP/FULL/APP_FULL_1.bak"])
    assert here == {"/bk/APP/FULL/APP_FULL_1.bak": {"size_bytes": 9, "sha256": SHA}}
    assert list(prune.catalog_facts(catalog, server_id="SRV-01", paths=["/srv/bk/APP/FULL/APP_FULL_1.bak"])) == [
        "/srv/bk/APP/FULL/APP_FULL_1.bak"]
    # The same name lying somewhere else is not that row's file.
    assert prune.catalog_facts(catalog, server_id="SRV-01", paths=["/tmp/APP_FULL_1.bak"]) == {}
    assert prune.catalog_facts(None, server_id="SRV-01", paths=["/bk/APP/FULL/APP_FULL_1.bak"]) == {}


def test_a_prune_deletes_by_the_catalogs_facts_and_keeps_what_it_does_not_know(common, monkeypatch):
    # A database's newest full is never obsolete: each has an old one to prune and a newer one kept.
    listed = {"files": [
        {"path": "/bk/APP_FULL_20200101.bak", "kind": "full", "database_name": "APP", "size_bytes": 9,
         "finished_at": "2020-01-01 00:00:00"},
        {"path": "/bk/APP_FULL_20200601.bak", "kind": "full", "database_name": "APP", "size_bytes": 9,
         "finished_at": "2020-06-01 00:00:00"},
        {"path": "/bk/MANUAL_COPY_20200101.bak", "kind": "full", "database_name": "MANUAL", "size_bytes": 4,
         "finished_at": "2020-01-01 00:00:00"},
        {"path": "/bk/MANUAL_COPY_20200601.bak", "kind": "full", "database_name": "MANUAL", "size_bytes": 4,
         "finished_at": "2020-06-01 00:00:00"}]}
    monkeypatch.setattr(prune.common_cli, "run", lambda command, request, **_kw: listed)
    catalog = SimpleNamespace(stored_files=lambda **_kwargs: {
        "APP_FULL_20200101.bak": {"file_path": "/bk/APP_FULL_20200101.bak", "size_bytes": 9, "checksum_after_backup": SHA}})
    request = {"db_type": "oracle", "path": "/bk", "cleanup_retention": 86400, "mode": "age", "delete": True,
               "allow_mass_delete": True, "host": HOST}
    result = prune._prune_one(request, secrets={}, catalog=catalog, server_id="SRV-01")
    assert result["deleted"]["deleted"] == ["/bk/APP_FULL_20200101.bak"]
    assert [one["path"] for one in result["deleted"]["kept"]] == ["/bk/MANUAL_COPY_20200101.bak"]
    assert common.named("delete-file") == []
    forced = prune._prune_one(request, secrets={}, catalog=catalog, server_id="SRV-01", force=True)
    assert [one["path"] for one in forced["deleted"]["forced"]] == ["/bk/MANUAL_COPY_20200101.bak"]


# ---------------------------------------------------------------------------- a restore's staging
def _job(**values):
    base = {"cleanup_retention": 86400, "target_backup_dir": "/st", "staging_marker": "", "target_server_id": "TGT-01",
            "restore_id": "R1", "label": "R1"}
    return SimpleNamespace(**{**base, **values})


def _staging(common, monkeypatch, files, folders=(), truncated=False):
    common.listing = {"folder": "/st", "folder_found": True, "files": files, "folders": list(folders),
                      "listed": len(files), "truncated": truncated}
    monkeypatch.setattr(restore_script, "_ssh_login", lambda *_a, **_k: {"host": "192.0.2.40", "username": "u"})


NOW = rules.epoch_of("2026-11-11T00:00:00Z")
MARKER = _entry("/st/.dbabrain-staging", "2026-01-01T00:00:00Z")


def test_a_staging_is_cleaned_file_by_file_from_what_the_copy_recorded(common, monkeypatch):
    _staging(common, monkeypatch,
             [MARKER, _entry("/st/base/1_FULL/PG_VERSION", "2026-11-01T00:00:00Z"),
              _entry("/st/base/2_FULL/PG_VERSION", "2026-11-10T23:00:00Z"),
              _entry("/st/operator-notes.txt", "2026-10-01T00:00:00Z")],
             folders=["/st/base", "/st/base/1_FULL", "/st/base/1_FULL/pg_tblspc", "/st/base/2_FULL",
                      "/st/base/2_FULL/pg_tblspc"])
    asked = {}

    def staged_file_facts(*, target_server_id, paths):
        asked["paths"] = (target_server_id, paths)
        return {"/st/base/1_FULL/PG_VERSION": {"size_bytes": 3, "sha256": SHA}}

    result = restore_script.prune_staged_backups(_job(), target=None, catalog=SimpleNamespace(
        staged_file_facts=staged_file_facts), now=NOW)
    assert asked["paths"] == ("TGT-01", ["/st/base/1_FULL/PG_VERSION", "/st/operator-notes.txt"])
    assert common.named("list-files")[0]["with_folders"] is True
    assert [item["path"] for item in common.named("delete-verified-files")[0]["files"]] == ["/st/base/1_FULL/PG_VERSION"]
    # The emptied set and its empty-by-design folder go, deepest first; the live set keeps its own.
    assert [item["path"] for item in common.named("delete-empty-folders")[0]["folders"]] == [
        "/st/base/1_FULL/pg_tblspc", "/st/base/1_FULL"]
    assert all(item["must_be_under"] == "/st" for item in common.named("delete-empty-folders")[0]["folders"])
    assert (result["pruned"], result["folders_removed"]) == (1, 2)
    assert result["kept_unknown"] == [{"path": "/st/operator-notes.txt", "reason": rules.NOT_IN_THE_CATALOG}]
    assert "error" not in result and common.named("delete-file") == []


def test_a_folder_without_the_marker_is_not_a_staging_and_nothing_is_deleted(common, monkeypatch):
    _staging(common, monkeypatch, [_entry("/st/host-own.bak", "2020-01-01T00:00:00Z")])
    result = restore_script.prune_staged_backups(_job(), target=None, catalog=None, now=NOW)
    assert "no .dbabrain-staging marker" in result["error"] and result["pruned"] == 0
    assert [name for name, _r in common.calls] == ["list-files"]


def test_half_a_listing_deletes_nothing(common, monkeypatch):
    _staging(common, monkeypatch, [MARKER, _entry("/st/a.bak", "2020-01-01T00:00:00Z")], truncated=True)
    result = restore_script.prune_staged_backups(_job(), target=None, catalog=None, now=NOW)
    assert "listed in part" in result["error"] and [name for name, _r in common.calls] == ["list-files"]


def test_no_retention_means_no_cleanup_and_no_call(common, monkeypatch):
    _staging(common, monkeypatch, [MARKER])
    result = restore_script.prune_staged_backups(_job(cleanup_retention=0), target=None, catalog=None, now=NOW)
    assert result == {"pruned": 0, "retention_seconds": 0, "skipped": "retention disabled"} and common.calls == []


def test_a_staged_file_that_changed_is_an_error_named_not_a_silent_skip(common, monkeypatch):
    _staging(common, monkeypatch, [MARKER, _entry("/st/a.bak", "2020-01-01T00:00:00Z")])
    common.statuses = {"/st/a.bak": "refused"}
    catalog = SimpleNamespace(staged_file_facts=lambda **_k: {"/st/a.bak": {"size_bytes": 3, "sha256": SHA}})
    result = restore_script.prune_staged_backups(_job(), target=None, catalog=catalog, now=NOW)
    assert result["pruned"] == 0 and "/st/a.bak: refused" in result["error"]


# ---------------------------------------------------------------------------- the catalog's staged facts
def _run_file(catalog, run_id, path, *, verdict="match", sha=SHA):
    return catalog.add_run_file(
        restore_run_id=run_id, source_server_id="SRC-01", target_server_id="TGT-01", file_name=path.rsplit("/", 1)[-1],
        file_type="FULL", source_path="/bk/" + path.rsplit("/", 1)[-1], target_path=path, target_host_path=path,
        copy_max_retry=3, size_source_bytes=5, size_target_bytes=5 if verdict == "match" else None,
        checksum_source_before_copy=sha, checksum_target_after_copy=sha if verdict == "match" else None,
        verdict=verdict)


def test_only_a_proven_copy_gives_a_staged_file_its_facts(tmp_path):
    catalog = BackupCatalogStore(tmp_path / "store.sqlite")
    run_id = catalog.start_restore_run(action="restore", restore_id="R1", source_server_id="SRC-01",
                                       target_server_id="TGT-01", target_folder="/st", copy_max_retry=3,
                                       phase="copying")
    _run_file(catalog, run_id, "/st/proven.bak")
    _run_file(catalog, run_id, "/st/unproven.bak", verdict="copying")
    facts = catalog.staged_file_facts(target_server_id="TGT-01",
                                      paths=["/st/proven.bak", "/st/unproven.bak", "/st/never-seen.bak"])
    assert facts == {"/st/proven.bak": {"size_bytes": 5, "sha256": SHA}}
    # Another server's staging is another matter.
    assert catalog.staged_file_facts(target_server_id="OTHER-01", paths=["/st/proven.bak"]) == {}
    assert catalog.staged_file_facts(target_server_id="TGT-01", paths=[]) == {}


# ---------------------------------------------------------------------------- the target import folder
def _restore_config(**values):
    base = {"restore_id": "R1", "source_id": "SRC-01", "target_id": "TGT-01"}
    return SimpleNamespace(**{**base, **values})


def test_an_import_folder_reachable_only_as_a_share_keeps_its_files_and_says_why(common):
    results = delete_backup._delete_selected(
        _restore_config(), [("\\\\192.0.2.50\\import\\APP\\a.bak", 9)], host=None,
        why_no_host="TGT-01 cannot be reached as a host (no cmd_access)", host_path_of=str,
        must_be_under="D:\\Import", windows=True, dry_run=False, force=True, catalog=None)
    assert [(row.status, row.bytes) for row in results] == [(delete_backup.KEPT, 9)]
    assert "cannot be reached as a host" in results[0].reason and common.calls == []


def test_the_import_cleanup_reports_each_file_in_its_own_word(common):
    catalog = SimpleNamespace(staged_file_facts=lambda **_k: {"/imp/APP/a.bak": {"size_bytes": 9, "sha256": SHA}})
    results = delete_backup._delete_selected(
        _restore_config(), [("/imp/APP/a.bak", 9), ("/imp/APP/stray.bak", 4)], host=HOST, host_path_of=str,
        must_be_under="/imp", windows=False, dry_run=False, force=False, catalog=catalog)
    assert sorted((str(row.target_file).replace("\\", "/"), row.status) for row in results) == [
        ("/imp/APP/a.bak", "DELETED"), ("/imp/APP/stray.bak", delete_backup.KEPT)]
    assert common.named("delete-verified-files")[0]["files"][0]["must_be_under"] == "/imp"


# ---------------------------------------------------------------------------- the copy no longer mirrors
def _absent(files=(), folders=()):
    return {"absent_at_source": list(files), "folders_absent_at_source": list(folders)}


def test_what_the_source_no_longer_has_is_deleted_by_the_facts_of_the_copy_that_staged_it(common):
    """Until 0.38.0 the staging copy mirrored: a staged file the source had dropped was removed over
    SFTP, held to nothing. The copy names it now, and the restore deletes it by a verified command."""
    catalog = SimpleNamespace(staged_file_facts=lambda **_k: {
        "/st/base/1_FULL/PG_VERSION": {"size_bytes": 3, "sha256": SHA}})
    cleared = restore_script._clear_absent_at_source(
        _job(), _absent(["base/1_FULL/PG_VERSION"], ["base/1_FULL/pg_tblspc", "base/1_FULL"]),
        target_host=HOST, target_dir="/st", catalog=catalog)
    assert cleared == 1
    assert common.named("delete-verified-files")[0]["files"] == [
        {"path": "/st/base/1_FULL/PG_VERSION", "file_name": "PG_VERSION", "size_bytes": 3, "sha256": SHA,
         "must_be_under": "/st"}]
    # The emptied set's folders, deepest first as the copy listed them, each fenced to the staging.
    assert common.named("delete-empty-folders")[0]["folders"] == [
        {"path": "/st/base/1_FULL/pg_tblspc", "must_be_under": "/st"},
        {"path": "/st/base/1_FULL", "must_be_under": "/st"}]


def test_a_stray_the_source_never_had_stops_the_restore_by_name(common):
    """An old life's RMAN pieces, or the husk of a removed base backup, must not reach a restore -
    the two reasons the mirror existed. A file no proven copy recorded is not deleted, and not
    restored over: the restore stops and names it, with the command that clears it."""
    with pytest.raises(restore_script.StagingNotClearError) as refused:
        restore_script._clear_absent_at_source(
            _job(), _absent(["old_life.bkp"], ["old_set"]), target_host=HOST, target_dir="/st",
            catalog=SimpleNamespace(staged_file_facts=lambda **_k: {}))
    said = str(refused.value)
    assert "/st/old_life.bkp (not in the catalog)" in said and "delete-file" in said and "force" in said
    assert common.calls == [], "nothing is deleted, and no folder is tried while a file is left"


def test_a_folder_that_will_not_go_stops_the_restore_too(common, monkeypatch):
    def refuse_folders(command, request, **_kwargs):
        if command == "delete-empty-folders":
            return False, {"folders": [{"path": item["path"], "status": "refused",
                                        "reason": "the folder holds 1 entry"} for item in request["folders"]]}, ""
        return common.run_allowing_failure(command, request)

    monkeypatch.setattr(verified_cleanup.common_cli, "run_allowing_failure", refuse_folders)
    with pytest.raises(restore_script.StagingNotClearError, match="the folder holds 1 entry"):
        restore_script._clear_absent_at_source(_job(), _absent([], ["husk"]), target_host=HOST, target_dir="/st",
                                               catalog=None)


def test_a_staging_that_holds_nothing_extra_asks_for_nothing(common):
    assert restore_script._clear_absent_at_source(_job(), _absent(), target_host=HOST, target_dir="/st",
                                                  catalog=None) == 0
    assert restore_script._clear_absent_at_source(_job(), {}, target_host=HOST, target_dir="/st", catalog=None) == 0
    assert common.calls == []


def test_the_staging_copy_itself_removes_nothing():
    from dbabrain.common import backup_copy

    source = open(backup_copy.__file__, encoding="utf-8").read()
    body = source[source.index("def prepare_staging("):source.index("def open_for_the_engine(")]
    assert ".remove(" not in body and ".rmdir(" not in body and "unlink" not in body
    assert "absent_at_source" in source and "removed_absent_at_source" not in source
    # ... and carries no byte either (0.38.0, stage 2): the tar stream and the per-file SFTP copy are
    # gone with copy-backup-dir - a backup file travels by copy-files, with its bytes and sha256.
    assert not hasattr(backup_copy, "sync_backup_dir") and not hasattr(backup_copy, "_stream_files")
    assert "tar -cf" not in source and ".putfo(" not in source


def test_the_three_old_copy_commands_read_their_request_and_refuse_it(capsys, monkeypatch):
    def never_opened(*_args, **_kwargs):
        raise AssertionError("no host may be reached")

    monkeypatch.setattr(cli_backup_copy, "_open", never_opened)
    request = {"source": {"host": "192.0.2.49", "username": "u", "password": "p"}, "source_dir": "/bk",
               "target": {"host": "192.0.2.40", "username": "u", "password": "p"}, "target_dir": "/st",
               "db_type": "postgresql"}
    for command, replacement in (("backup-chain", "prepare-staging"), ("copy-backup-dir", "copy-files"),
                                 ("prune-staged-backups", "delete-verified-files")):
        code = cli_backup_copy.run(command, ["-"], read_request=lambda _arg, _usage: (dict(request), 0))
        answer = _answer(capsys)
        assert code != 0 and answer["success"] is False, command
        assert "refused" in answer["error"] and replacement in answer["error"], command
