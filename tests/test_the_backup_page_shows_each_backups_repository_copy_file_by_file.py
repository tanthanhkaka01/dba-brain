"""The backup page shows each backup's repository copy, file by file, before and after encryption (0.37.0).

Why it matters: the operator, 2026-11-11 (translated) - *is the page right and complete? encrypted or
not, with which key, until when; the checksum and bytes before and after encryption; can it show the
three files belong to one FULL backup?* - and *a backup left on the server_id can be copied by anyone
on the VM*, so the page must say where each backup still is. And *use the open-source frontend*: the
opened row is drawn with Tabler's own datagrid, card table and badges.

So: the facts each row carries (pure, from the catalog's proofs and the encryption store's rows), the
row as the shared table draws it in a real JavaScript engine, a file deleted as meant never counted
"missing", and a drill whose entry is gone listed as such rather than failing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dbabrain.db.backup_catalog import MANIFEST_KEPT
from dbabrain.reports import backup_catalog_sections as S
from dbabrain.reports import backup_restore_report as page

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="the table is drawn by JavaScript")
REPO = "/mnt/nas/SRV/cluster/base/20261111T020801Z_FULL"
MEMBERS = {"backup_manifest": (643556, "8e" * 32, 643900, "24" * 32),
           "base.tar.zst": (6078777465, "a5" * 32, 6080261729, "73" * 32),
           "pg_wal.tar": (33556992, "87" * 32, 33565384, "12" * 32)}


def _members(differs=None):
    return [{"file_id": 7, "source_path": f"/b/base/F/{n}", "source_host_path": f"/host/b/base/F/{n}",
             "target_path": f"{REPO}/{n}", "size_source_bytes": b, "size_target_bytes": b,
             "checksum_source_before_copy": s, "checksum_target_after_copy": ("ff" * 32 if n == differs else s),
             "verdict": "copied", "copied_at": "2026-11-11T02:16:40Z", "copy_started_at": "2026-11-11T02:14:00Z",
             "copy_ended_at": "2026-11-11T02:16:30Z"} for n, (b, s, _ab, _as) in MEMBERS.items()]


def _sealed(only=None, unproven=None):
    return [{"file_id": 7, "plain_path": f"{REPO}/{n}", "plain_size_bytes": b, "plain_sha256": s,
             "encrypted_path": f"{REPO}/{n}.age", "encrypted_size_bytes": ab, "encrypted_sha256": as_,
             "key_id": 1, "algorithm": "age-x25519", "encrypted_at": "2026-11-11T02:17:00Z",
             "proven_at": None if n == unproven else "2026-11-11T02:17:01Z",
             "plain_deleted_at": None if n == unproven else "2026-11-11T02:17:02Z"}
            for n, (b, s, ab, as_) in MEMBERS.items() if only is None or n in only]


KEYS = {1: {"key_id": 1, "encrypt_until": "2027-02-08T07:19:31Z"}}
ROW = {"file_id": 7, "offload_state": "verified", "offload_server_id": "A1A-REPO", "offload_path": REPO,
       "offload_verified_at": "2026-11-11T02:16:33Z"}


# --------------------------------------------------------------------------- #
# The facts
# --------------------------------------------------------------------------- #
def test_each_file_of_a_backup_carries_its_bytes_and_sha256_on_the_server_the_repository_and_encrypted():
    facts = S.repository_facts(ROW, _members(), _sealed(), KEYS)

    assert facts["total"] == 3 and facts["encrypted"] == 3, "three files of one FULL, each encrypted and proven"
    assert facts["keys"] == [1] and facts["keyUntil"] == "2027-02-08T07:19:31Z"
    base = next(f for f in facts["files"] if f["name"] == "base.tar.zst")
    assert (base["bytes"], base["sha256"]) == (6078777465, "a5" * 32)
    assert (base["repoBytes"], base["repoSha256"]) == (6078777465, "a5" * 32)
    assert (base["age"]["bytes"], base["age"]["sha256"]) == (6080261729, "73" * 32)
    assert base["age"]["keyId"] == 1 and base["age"]["provenAt"] and base["age"]["plainRemovedAt"]


def test_a_backup_encrypted_in_part_or_not_proven_says_so():
    facts = S.repository_facts(ROW, _members(), _sealed(only={"backup_manifest", "pg_wal.tar"}, unproven="pg_wal.tar"),
                               KEYS)
    assert facts["encrypted"] == 1 and facts["total"] == 3
    assert next(f for f in facts["files"] if f["name"] == "base.tar.zst")["age"] is None


def test_an_encrypted_file_without_its_copy_row_is_shown_not_dropped():
    facts = S.repository_facts(ROW, _members()[:1], _sealed(), KEYS)
    assert facts["total"] == 3 and {f["name"] for f in facts["files"]} == set(MEMBERS)


@pytest.mark.parametrize("row, state", [
    ({"source_deleted_at": "2026-11-11T02:56:16Z", "on_disk": 0, "state": "pruned"}, "released"),
    ({"pruned_by": MANIFEST_KEPT, "on_disk": 1, "state": "present"}, "manifest_only"),
    ({"on_disk": 0, "state": "pruned", "pruned_by": "retention"}, "pruned"),
    ({"on_disk": 0, "state": "missing"}, "missing"),
    ({"on_disk": 1, "state": "present"}, "present"),
])
def test_where_a_backup_stands_on_its_own_server(row, state):
    assert S._on_server(row)["state"] == state


def test_a_file_deleted_as_meant_is_never_counted_missing():
    files = [{"kind": "backup", "verdict": "ok", "checksumMatches": None, "onDisk": False, "usedCount": 0,
              "restoredOk": 0, "lastRestore": None, "virus": None, "type": "WAL",
              "onServer": {"state": s}} for s in ("released", "manifest_only", "pruned", "missing")]
    assert S.summarize(files, total=4)["missing"] == 1


def test_a_drill_whose_entry_is_gone_is_listed_as_no_longer_configured():
    now = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    runs = [{"restore_id": "OLD_DRILL", "started_at": "2026-11-10T09:36:00Z", "phase": "error"}]
    assert S.build_drills(runs, now=now, entries=[{"restore_id": "OTHER"}])[0]["state"] == "REMOVED"
    assert S.build_drills(runs, now=now)[0]["state"] != "REMOVED", "without the config read, nothing is assumed"


# --------------------------------------------------------------------------- #
# The row as the shared table draws it
# --------------------------------------------------------------------------- #
def _draw(files):
    table = {**S.summarize(files, total=len(files)), "retention": []}
    shared = Path(page.BACKUP_FILES_JS).read_text(encoding="utf-8")
    script = ('const esc = s => String(s ?? "");\nconst when = v => v ? String(v) : "";\n' + shared
              + f"\nprocess.stdout.write(backupFilesBlock({json.dumps(table)}, {{fleet: true}}));")
    # On stdin, not as `-e`'s argument: the shared table with a row's data is longer than a Windows
    # command line may be (32,767 characters) since the row gained its timeline and chain (1.345).
    done = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _file(**over):
    base = {"kind": "backup", "at": "2026-11-11T02:13:26Z", "server": "SRV", "host": "SRV", "database": "cluster",
            "type": "FULL_BASE", "name": "20261111T020801Z_FULL", "sizeBytes": 6112978013, "sha256": "8e" * 32,
            "engineVerify": "passed", "engineVerifyText": "", "verdict": "ok", "encrypted": False, "onDisk": False,
            "checksumMatches": True, "lastChecked": None, "virus": None, "path": "/host/b/base/F", "container": "",
            "usedCount": 0, "restoredOk": 0, "mark": None, "lastRestore": None,
            "repo": S.repository_facts(ROW, _members(), _sealed(), KEYS),
            "onServer": {"state": "released", "at": "2026-11-11T02:56:16Z"}}
    base.update(over)
    return base


@needs_node
def test_the_row_says_verified_encrypted_with_which_key_until_when_and_deleted_on_the_server():
    html = _draw([_file()])
    assert "<th>Repository</th>" in html and "<th>On the server</th>" in html
    assert "encrypted 3/3" in html and "#1" in html and "until 2027-02-08" in html
    assert 'sev ok">deleted<' in html and "after its copy was verified" in html
    assert "3 files" in html, "the three files are one backup"
    assert 'class="bf-main ' in html and 'class="bf-main crit"' not in html, "deleted as meant is not a problem"


@needs_node
def test_the_opened_row_lists_each_file_before_and_after_encryption_in_tablers_classes():
    html = _draw([_file()])
    assert 'class="datagrid"' in html and 'datagrid-item' in html
    assert 'class="table table-sm table-vcenter card-table"' in html
    for name, (b, s, ab, as_) in MEMBERS.items():
        assert name in html and f"{b:,} B" in html and f"{ab:,} B" in html and s in html and as_ in html
    assert html.count('sev ok">= server<') == 3 and html.count("decrypted = server") == 3 and html.count(">none<") == 3


@needs_node
def test_a_repository_copy_of_other_bytes_is_said_on_its_file():
    html = _draw([_file(repo=S.repository_facts(ROW, _members(differs="pg_wal.tar"), _sealed(), KEYS))])
    assert html.count('sev ok">= server<') == 2 and "differs" in html


@needs_node
@pytest.mark.parametrize("state, words", [("manifest_only", "manifest only"), ("missing", "MISSING"),
                                          ("present", "on the server")])
def test_the_on_the_server_cell_says_where_the_backup_is(state, words):
    html = _draw([_file(onServer={"state": state, "at": None})])
    assert words in html
    assert ('class="bf-main crit"' in html) == (state == "missing")


@needs_node
def test_a_backup_never_copied_and_a_failed_copy_say_so():
    assert "not copied" in _draw([_file(repo=None, onServer={"state": "present", "at": None})])
    failed = _draw([_file(repo={**S.repository_facts(ROW, [], [], KEYS), "state": "failed", "error": "no route"})])
    assert "copy failed" in failed and "no route" in failed and 'class="bf-main crit"' in failed


def test_a_backup_released_at_its_source_still_covers_its_database(tmp_path):
    """Coverage read the server's files only: once the store's FULL went to the repository and left the
    server, its database read "a full backup missing" though the repository held it verified."""
    from dbabrain.db.backup_catalog import BackupCatalogStore

    catalog = BackupCatalogStore(tmp_path / "c.sqlite")
    catalog.initialize()
    file_id = catalog.record_backup_file(server_id="SRV", database_name="cluster", engine="postgresql",
                                         file_name="F", file_type="FULL_BASE", file_path="/b/F",
                                         checksum_after_backup="a" * 64, size_bytes=1, verdict="ok",
                                         backup_ended_at="2026-11-11T02:13:26Z")
    catalog.set_backup_file(file_id, offload_state="verified", offload_server_id="REPO", offload_path="/r/F",
                            offload_run_id=1, offload_verified_at="2026-11-11T02:16:33Z")
    catalog.set_backup_file(file_id, state="pruned", on_disk=0, source_deleted_at="2026-11-11T02:56:16Z")

    rows = S.build_coverage(catalog.coverage())
    assert rows[0]["newest_full"] == "2026-11-11T02:13:26Z" and "a full backup" not in rows[0]["missing"]
    assert rows[0]["offloaded"] == 1

# --------------------------------------------------------------------------- #
# The newest differential, a backup's timeline, on the server said plainly, a restore chain (0.38.0, 1.345)
# --------------------------------------------------------------------------- #
# The operator, 2026-11-12, reading one LOG file's opened row (translated): *there is a newest full and
# no newest diff; has the file been deleted on the server_id? - it says clearly only that the plain one
# was deleted on the repository; if it is a diff or a log, can the full, diff, log chain be had; and
# when was it written on the server, copied to the repository, encrypted, the plain copy gone there,
# deleted on the server?* The catalog held every one of those; the opened row said a few.
def test_coverage_says_when_the_newest_differential_was_beside_the_full_and_the_log():
    rows = [{"server_id": "S1", "database_name": "SALES", "engine": "sqlserver", "file_type": t, "files": 1,
             "newest": at, "offloaded": 1, "last_restored": None}
            for t, at in (("FULL", "2026-11-09T18:00:00Z"), ("DIFF", "2026-11-11T20:00:00Z"), ("LOG", "2026-11-12T01:43:31Z"))]
    rows.append({"server_id": "S2", "database_name": "cluster", "engine": "postgresql", "file_type": "INCR_BASE",
                 "files": 3, "newest": "2026-11-11T22:42:45Z", "offloaded": 3, "last_restored": None})

    found = {r["server_id"]: r for r in S.build_coverage(rows)}

    assert found["S1"]["newest_diff"] == "2026-11-11T20:00:00Z"
    assert found["S2"]["newest_diff"] == "2026-11-11T22:42:45Z", "an incremental is the level between a full and a log"
    assert (found["S1"]["newest_full"], found["S1"]["newest_log"]) == ("2026-11-09T18:00:00Z", "2026-11-12T01:43:31Z")


def test_the_coverage_table_has_a_column_for_it(tmp_path):
    from dbabrain.db.backup_catalog import BackupCatalogStore

    catalog = BackupCatalogStore(tmp_path / "c.sqlite")
    catalog.initialize()
    for name, kind, at in (("F.bak", "FULL", "2026-11-09T18:00:00Z"), ("D.bak", "DIFF", "2026-11-11T20:07:00Z")):
        catalog.record_backup_file(server_id="S1", database_name="SALES", engine="sqlserver", file_name=name, file_type=kind,
                                   file_path=f"/b/{name}", checksum_after_backup="a" * 64, size_bytes=1, verdict="ok",
                                   backup_ended_at=at)

    html = page.render_recoverability(catalog, entries=[])

    assert "<th>Newest full</th><th>Newest diff / incr.</th><th>Newest log</th>" in html
    assert ":07" in html, "the differential's own minute is on the row"


def test_a_backups_copy_reaches_a_step_when_its_last_file_does():
    whole = S.repository_facts(ROW, _members(), _sealed(), KEYS)
    assert (whole["copyStartedAt"], whole["copiedAt"]) == ("2026-11-11T02:14:00Z", "2026-11-11T02:16:30Z"), (
        "the copy's own window - not the run's end (02:16:40), which comes after the verify (02:16:33)")
    before_attempts = [{**m, "copy_started_at": None, "copy_ended_at": None} for m in _members()]
    assert S.repository_facts(ROW, before_attempts, _sealed(), KEYS)["copiedAt"] is None
    assert (whole["encryptedAt"], whole["provenAt"], whole["plainGoneAt"]) == (
        "2026-11-11T02:17:00Z", "2026-11-11T02:17:01Z", "2026-11-11T02:17:02Z")

    part = S.repository_facts(ROW, _members(), _sealed(unproven="pg_wal.tar"), KEYS)
    assert part["encryptedAt"] == "2026-11-11T02:17:00Z"
    assert part["provenAt"] is None and part["plainGoneAt"] is None, "one file not proven: the backup is not"
    assert S.repository_facts(ROW, _members(), _sealed(only=["base.tar.zst"]), KEYS)["encryptedAt"] is None


def _chain_row(file_id, kind, at, *, parent=None, engine="sqlserver", repo="verified", gone=True, name=None):
    return {"file_id": file_id, "chain_parent_file_id": parent, "server_id": "S1", "database_name": "SALES",
            "engine": engine, "file_type": kind, "file_name": name or f"SALES_{kind}_{file_id}", "ended_at": at,
            "verdict": "ok", "state": "pruned" if gone else "present", "on_disk": 0 if gone else 1,
            "source_deleted_at": "2026-11-12T00:00:00Z" if gone else None,
            "pruned_by": "offload: released after its copy was verified" if gone else None,
            "offload_state": repo, "offload_server_id": "REPO" if repo else None, "offload_verified_at": at if repo else None}


SQL_CHAIN = [
    _chain_row(1, "FULL", "2026-11-09T18:00:00Z"),
    _chain_row(2, "LOG", "2026-11-09T18:15:00Z", parent=1),
    _chain_row(3, "DIFF", "2026-11-10T20:00:00Z", parent=1),
    _chain_row(4, "LOG", "2026-11-10T20:15:00Z", parent=1),
    _chain_row(5, "DIFF", "2026-11-11T20:00:00Z", parent=1),
    _chain_row(6, "LOG", "2026-11-11T20:15:00Z", parent=1),
    _chain_row(7, "LOG", "2026-11-11T20:30:00Z", parent=1, gone=False),
    _chain_row(8, "LOG", "2026-11-11T20:45:00Z", parent=1),
]


def test_a_logs_chain_is_its_full_the_newest_differential_before_it_and_every_log_since():
    chain = S.build_chains([SQL_CHAIN[-1]], SQL_CHAIN)[8]

    assert chain["full"]["name"] == "SALES_FULL_1"
    assert [d["name"] for d in chain["diffs"]] == ["SALES_DIFF_5"], "a SQL Server differential holds all since the full"
    assert chain["logs"] == {"count": 3, "from": "2026-11-11T20:15:00Z", "to": "2026-11-11T20:45:00Z",
                             "onRepository": 3, "onServer": 1}
    assert chain["complete"] and chain["onRepository"] and chain["missing"] == []
    assert chain["full"]["onServer"] == "released" and chain["full"]["repo"] == "verified"


def test_a_log_before_any_differential_chains_from_the_full_and_a_differential_needs_no_log():
    chains = S.build_chains([SQL_CHAIN[1], SQL_CHAIN[4]], SQL_CHAIN)

    assert chains[2]["diffs"] == [] and chains[2]["logs"]["count"] == 1
    assert [d["name"] for d in chains[5]["diffs"]] == ["SALES_DIFF_5"] and chains[5]["logs"] is None


def test_an_incremental_of_another_engine_builds_on_every_one_since_the_full():
    rows = [_chain_row(1, "FULL_BASE", "2026-11-08T18:00:00Z", engine="postgresql"),
            _chain_row(2, "INCR_BASE", "2026-11-09T18:00:00Z", parent=1, engine="postgresql"),
            _chain_row(3, "INCR_BASE", "2026-11-10T18:00:00Z", parent=1, engine="postgresql"),
            _chain_row(4, "WAL", "2026-11-10T18:15:00Z", parent=1, engine="postgresql")]

    chains = S.build_chains(rows, rows)

    assert [d["name"] for d in chains[3]["diffs"]] == ["SALES_INCR_BASE_2", "SALES_INCR_BASE_3"]
    assert [d["name"] for d in chains[4]["diffs"]] == ["SALES_INCR_BASE_2", "SALES_INCR_BASE_3"] and chains[4]["logs"]["count"] == 1
    assert 1 not in chains, "a full is its own chain"


def test_a_chain_with_a_file_that_is_nowhere_is_incomplete_and_names_it():
    rows = [dict(r) for r in SQL_CHAIN]
    rows[5].update(offload_state="expired")                      # the 20:15 log: gone from the server, expired on the repository
    rows[6].update(offload_state="")                             # the 20:30 log: never copied, but still on its server

    chain = S.build_chains([rows[-1]], rows)[8]

    assert not chain["complete"] and chain["missing"] == ["SALES_LOG_6"] and chain["missingCount"] == 1
    assert chain["logs"]["onRepository"] == 1 and not chain["onRepository"]

    rows[5].update(offload_state="verified")
    still = S.build_chains([rows[-1]], rows)[8]
    assert still["complete"] and not still["onRepository"], "had from the server where the repository has none"


def test_a_file_with_no_full_before_it_says_so():
    orphan = _chain_row(9, "LOG", "2026-11-12T00:00:00Z")
    later_full = _chain_row(10, "FULL", "2026-11-12T06:00:00Z")
    another_database = {**_chain_row(11, "FULL", "2026-11-01T00:00:00Z"), "database_name": "OTHER"}

    chain = S.build_chains([orphan], [later_full, another_database, orphan])[9]

    assert chain["full"] is None and not chain["complete"]
    assert chain["note"] == "the catalog holds no full backup of this database taken before this file"


def test_a_log_written_after_its_full_left_the_server_is_still_chained_to_that_full():
    """What the estate showed on 2026-11-12: the newest full had gone to the repository and been
    deleted on its server; the row's recorded link then pointed at an older full still lying there
    (and, for the store's WAL, at none). The chain is read by time, so it is the newest full's."""
    old_full = _chain_row(1, "FULL", "2026-11-07T18:14:00Z", repo="", gone=False)          # before the offload flow
    old_diff = _chain_row(2, "DIFF", "2026-11-10T20:01:00Z", repo="", gone=False)
    new_full = _chain_row(3, "FULL", "2026-11-11T04:31:00Z")                                  # released, verified
    new_diff = _chain_row(4, "DIFF", "2026-11-11T05:02:00Z")
    logs = [_chain_row(5 + n, "LOG", f"2026-11-11T0{5 + n}:15:00Z") for n in range(3)]
    wrongly_linked = {**logs[-1], "chain_parent_file_id": 1}

    chain = S.build_chains([wrongly_linked], [old_full, old_diff, new_full, new_diff, *logs])[7]

    assert chain["full"]["name"] == "SALES_FULL_3" and [d["name"] for d in chain["diffs"]] == ["SALES_DIFF_4"]
    assert chain["logs"]["count"] == 3 and chain["logs"]["from"] == "2026-11-11T05:15:00Z"
    assert chain["complete"] and chain["onRepository"]


def test_a_new_full_begins_a_new_chain():
    rows = [_chain_row(1, "FULL", "2026-11-02T18:00:00Z"), _chain_row(2, "DIFF", "2026-11-05T20:00:00Z"),
            _chain_row(3, "LOG", "2026-11-05T20:15:00Z"), _chain_row(4, "FULL", "2026-11-09T18:00:00Z"),
            _chain_row(5, "LOG", "2026-11-09T18:15:00Z")]

    chains = S.build_chains([rows[2], rows[4]], rows)

    assert chains[3]["full"]["name"] == "SALES_FULL_1" and [d["name"] for d in chains[3]["diffs"]] == ["SALES_DIFF_2"]
    assert chains[5]["full"]["name"] == "SALES_FULL_4" and chains[5]["diffs"] == [], "last week's differential is not its"
    assert chains[5]["logs"]["count"] == 1


def _catalog_with_a_chain(tmp_path):
    from dbabrain.db.backup_catalog import BackupCatalogStore

    catalog = BackupCatalogStore(tmp_path / "c.sqlite")
    catalog.initialize()
    ids = {}
    for server, database, name, kind, at in (
            ("S1", "SALES", "F.bak", "FULL", "2026-11-09T18:00:00Z"), ("S1", "SALES", "D.bak", "DIFF", "2026-11-10T20:00:00Z"),
            ("S1", "SALES", "L1.trn", "LOG", "2026-11-10T20:15:00Z"), ("S1", "SALES", "L2.trn", "LOG", "2026-11-10T20:30:00Z"),
            ("S1", "OTHER", "O.bak", "FULL", "2026-11-09T18:00:00Z"), ("S2", "SALES", "S2.bak", "FULL", "2026-11-09T18:00:00Z")):
        ids[name] = catalog.record_backup_file(
            server_id=server, database_name=database, engine="sqlserver", file_name=name, file_type=kind,
            file_path=f"/b/{name}", checksum_after_backup="a" * 64, size_bytes=1, verdict="ok", backup_ended_at=at)
    return catalog, ids


def test_the_catalog_gives_a_servers_files_by_level_and_time(tmp_path):
    catalog, _ids = _catalog_with_a_chain(tmp_path)

    assert [r["file_name"] for r in catalog.chain_rows("S1", levels=("full",))] == ["F.bak", "O.bak"]
    assert [r["file_name"] for r in catalog.chain_rows("S1", levels=("diff", "log"))] == ["D.bak", "L1.trn", "L2.trn"]
    assert [r["file_name"] for r in catalog.chain_rows("S1", levels=("diff", "log"), since="2026-11-10T20:15:00Z")] == [
        "L1.trn", "L2.trn"]
    assert catalog.chain_rows("S1", levels=()) == [] and catalog.chain_rows("NOBODY", levels=("full",)) == []


def test_a_pages_rows_get_their_chains_from_the_catalog(tmp_path):
    catalog, ids = _catalog_with_a_chain(tmp_path)
    page_rows = catalog.server_backup_files("S1")

    chains = S.load_chains(catalog, page_rows)

    chain = chains[ids["L2.trn"]]
    assert chain["full"]["name"] == "F.bak" and [d["name"] for d in chain["diffs"]] == ["D.bak"] and chain["logs"]["count"] == 2
    assert chain["complete"], "never copied, and still on the server"
    assert ids["F.bak"] not in chains and ids["O.bak"] not in chains
    table = S.build_files(page_rows, [], server_id="S1", chains=chains)
    by_name = {f["name"]: f for f in table["files"]}
    assert by_name["L2.trn"]["chain"]["logs"]["count"] == 2 and by_name["F.bak"]["chain"] is None


def test_a_file_is_linked_to_the_newest_full_though_that_full_has_left_its_server(tmp_path):
    """The link's own rule, for the rows written from now on (a row's link is under its row_hash and
    is never rewritten): the newest full, whatever its state."""
    from dbabrain.db.backup_catalog import BackupCatalogStore

    catalog = BackupCatalogStore(tmp_path / "c.sqlite")
    catalog.initialize()

    def backup(name, kind, at):
        catalog.record_backup(
            run={"backup_id": f"SALES_{kind}", "server_id": "S1", "engine": "sqlserver", "started_at": at, "status": "DONE"},
            files=[{"backup_id": f"SALES_{kind}", "server_id": "S1", "database_name": "SALES", "engine": "sqlserver",
                    "file_name": name, "file_type": kind, "file_path": f"/b/{name}", "backup_ended_at": at, "size_bytes": 1,
                    "checksum_after_backup": "a" * 64, "verdict": "ok"}])
        return next(r for r in catalog.server_backup_files("S1") if r["file_name"] == name)

    backup("OLD.bak", "FULL", "2026-11-07T18:00:00Z")
    newest = backup("NEW.bak", "FULL", "2026-11-11T04:17:00Z")
    catalog.set_backup_file(newest["file_id"], offload_state="verified", offload_server_id="REPO", offload_path="/r/NEW.bak",
                            offload_run_id=1, offload_verified_at="2026-11-11T06:50:00Z")
    catalog.set_backup_file(newest["file_id"], state="pruned", on_disk=0, source_deleted_at="2026-11-11T07:07:00Z")

    log = backup("L.trn", "LOG", "2026-11-12T01:43:00Z")

    assert log["chain_parent_file_id"] == newest["file_id"], "not the older full still lying on the server"


@needs_node
def test_the_opened_row_says_whether_the_file_is_still_on_its_server():
    deleted = _draw([_file()])
    assert "On the server now" in deleted and "after its repository copy was verified" in deleted
    assert deleted.count('sev ok">deleted</span> 2026-11-11T02:56:16Z') >= 2, "in the facts, and on each file's line"

    there = _draw([_file(onServer={"state": "present", "at": None})])
    assert 'sev">still there</span> <span class="text-secondary">- not deleted' in there


@needs_node
def test_the_opened_row_has_the_backups_timeline_step_by_step():
    html = _draw([_file(startedAt="2026-11-11T02:08:01Z")])
    line = html[html.index(">Timeline<"):]
    for words in ("Written on SRV", "2026-11-11T02:08:01Z → 2026-11-11T02:13:26Z", "Copied to A1A-REPO",
                  "2026-11-11T02:14:00Z → 2026-11-11T02:16:30Z", "Copy verified", "2026-11-11T02:16:33Z",
                  "Encrypted there", "2026-11-11T02:17:00Z",
                  "Encrypted copy proven", "2026-11-11T02:17:01Z", "No plain copy left on the repository", "2026-11-11T02:17:02Z",
                  "Deleted on SRV", "2026-11-11T02:56:16Z"):
        assert words in line, words
    order = [line.index(w) for w in ("Written on SRV", "Copied to A1A-REPO", "Copy verified", "Encrypted there",
                                      "Encrypted copy proven", "No plain copy left", "Deleted on SRV")]
    assert order == sorted(order), "in the order it happened"


@needs_node
def test_a_step_that_has_not_happened_says_so():
    waiting = _draw([_file(repo=None, onServer={"state": "present", "at": None})])
    assert 'sev">not copied<' in waiting and 'sev">not deleted<' in waiting
    part = _draw([_file(repo=S.repository_facts(ROW, _members(), _sealed(unproven="pg_wal.tar"), KEYS),
                        onServer={"state": "present", "at": None})])
    assert "a plain file is still there" in part and 'sev">not yet<' in part
    unkept = _draw([_file(repo=S.repository_facts(
        ROW, [{**m, "copy_started_at": None, "copy_ended_at": None} for m in _members()], _sealed(), KEYS))])
    assert 'sev">not recorded<' in unkept and "the copy's own start and end were not kept" in unkept


@needs_node
def test_a_logs_opened_row_lists_its_restore_chain_and_says_it_is_complete():
    chain = S.build_chains([SQL_CHAIN[-1]], SQL_CHAIN)[8]
    html = _draw([_file(type="LOG", name="SALES_LOG_8", chain=chain)])
    block = html[html.index("Restore chain"):]
    assert 'sev ok">complete</span> every file of it is verified on the repository' in block
    for words in ("SALES_FULL_1", "SALES_DIFF_5", "LOG × 3", "2026-11-11T20:15:00Z → 2026-11-11T20:45:00Z", "verified 3/3",
                  "1 of 3 still there"):
        assert words in block, words
    assert "SALES_DIFF_3" not in block
    assert "Restore chain" not in _draw([_file()]), "a full has none to list"


@needs_node
def test_a_broken_chain_is_said_in_red_with_the_file_that_is_nowhere():
    rows = [dict(r) for r in SQL_CHAIN]
    rows[5].update(offload_state="expired")
    html = _draw([_file(type="LOG", name="SALES_LOG_8", chain=S.build_chains([rows[-1]], rows)[8])])
    assert 'sev crit">INCOMPLETE</span> 1 file(s) neither verified on the repository nor on the server: SALES_LOG_6' in html
    assert "verified 2/3" in html
    none = _draw([_file(type="LOG", name="X", chain=S.build_chains([_chain_row(9, "LOG", "t")], [])[9])])
    assert "no chain" in none and "holds no full backup of this database taken before this file" in none

def _swept_chain():
    rows = [_chain_row(1, "FULL_BASE", "2026-11-07T18:00:00Z", engine="postgresql", repo=""),
            _chain_row(2, "WAL", "2026-11-07T18:15:00Z", engine="postgresql", repo="")]
    return S.build_chains([rows[1]], rows)[2]


def test_a_chain_its_retention_swept_is_history_not_an_alarm():
    """The store's page, 2026-11-12: 53 WAL rows from before its newest full read INCOMPLETE - their
    base backup and they themselves had been swept by retention, as meant. A file that is itself
    nowhere is history; the alarm is a file still kept whose chain lacks a piece."""
    swept = _swept_chain()
    assert not swept["complete"] and swept["selfGone"] and swept["missingCount"] == 2

    rows = [dict(r) for r in SQL_CHAIN]
    rows[5].update(offload_state="expired")
    kept = S.build_chains([rows[-1]], rows)[8]
    assert not kept["complete"] and not kept["selfGone"]


@needs_node
def test_a_swept_chain_is_said_plainly_and_not_in_red():
    html = _draw([_file(type="WAL", name="000000010000000A", chain=_swept_chain())])
    assert 'sev">no longer restorable</span>' in html and "with 1 other file(s) of its chain" in html
    assert "INCOMPLETE" not in html
