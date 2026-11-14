"""A failed restore is continued from where it stopped - it is neither refused nor started over (0.39.0, 1.372).

The operator, 2026-11-13 (translated): *and like backup - can a failed restore go on running?*

Read that day, it could not. A SQL Server restore that failed after its full left the database
RESTORING; the next run - which since 1.362 replaces nothing that exists - was refused by that very
database, and with ``overwrite_existing: true`` it started again from the full. A chain decrypted
from the repository was decrypted again whole, whatever the staging already held.

Now a run goes on:

* a plain file the staging already holds, hashed there and equal to the plaintext the store
  recorded, is not decrypted again;
* a database this entry's own unfinished run left RESTORING is continued after the last backup the
  TARGET says it applied - two records held to each other: the chain the catalog recorded for that
  run (what makes the database this entry's) and the target's own ``restorehistory`` (where it
  really stopped: a log step that failed on its seventeenth file is recorded failed whole);
* a database that run had already restored to its end and opened is kept, so the others go on;
* a RESTORING database nobody recorded is somebody's standby: refused, as any name that exists.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import landing_of

from dbabrain.backup_restore import restore_by_id, restore_script
from dbabrain.db.backup_catalog import BackupCatalogStore


def _file(kind: str, hour: int, minute: int = 0) -> dict:
    stamp = f"2026-11-12 {hour:02d}:{minute:02d}:00"
    return {"path": f"/stage/APP/{kind}/APP_{kind}_{hour:02d}{minute:02d}.bak", "finished_at": stamp,
            "database_name": "APP"}


FULL, DIFF = _file("FULL", 4), _file("DIFF", 6)
LOGS = [_file("LOG", hour) for hour in (7, 8, 9)]


def _recorded(*files, run=7, result="applied", **last):
    rows = [{"restore_run_id": run, "position": number, "file_name": item["path"].rsplit("/", 1)[-1],
             "backup_ended_at": item["finished_at"], "restored_as": "APP_COPY", "result": result}
            for number, item in enumerate(files, start=1)]
    rows[-1].update(last)
    return rows


# --------------------------------------------------------------------------- #
# The rule: two records held to each other
# --------------------------------------------------------------------------- #
def test_a_database_left_restoring_goes_on_after_the_last_backup_the_target_applied():
    recorded = _recorded(FULL, DIFF, *LOGS, result="failed")

    found = restore_by_id.continuation(recorded, state="RESTORING",
                                       restored_to={"last_backup_finished": "2026-11-12T07:00:00"})

    assert found == {"restore_run_id": 7, "file_name": "APP_LOG_0700.bak", "after": "2026-11-12 07:00:00",
                     "complete": False}, "the catalog said the log step failed whole; the target had applied one"


def test_a_restoring_database_nobody_recorded_is_not_this_entrys():
    """A log-shipping standby is RESTORING too: without this entry's own record it is never taken up."""
    there = {"last_backup_finished": "2026-11-12 07:00:00"}

    assert restore_by_id.continuation([], state="RESTORING", restored_to=there) is None
    assert restore_by_id.continuation(_recorded(FULL), state="RESTORING", restored_to=there) is None, \
        "recorded, and the target's last backup is not one of its files"
    assert restore_by_id.continuation(_recorded(FULL), state="RESTORING", restored_to=None) is None
    assert restore_by_id.continuation(_recorded(FULL), state="RESTORING", restored_to={}) is None


def test_a_database_the_unfinished_run_restored_to_its_end_is_kept():
    recorded = _recorded(FULL, *LOGS, stopat="2026-11-12 08:30:00")

    found = restore_by_id.continuation(recorded, state="ONLINE",
                                       restored_to={"last_backup_finished": "2026-11-12 09:00:00"})

    assert found["complete"] is True and found["stopat"] == "2026-11-12 08:30:00"
    # Open, and not at its recorded end - or its last file never applied: an ordinary database that exists.
    assert restore_by_id.continuation(recorded, state="ONLINE",
                                      restored_to={"last_backup_finished": "2026-11-12 08:00:00"}) is None
    assert restore_by_id.continuation(_recorded(FULL, *LOGS, result="failed"), state="ONLINE",
                                      restored_to={"last_backup_finished": "2026-11-12 09:00:00"}) is None


def test_any_other_state_is_held_like_a_database_that_exists():
    recorded = _recorded(FULL, *LOGS)

    for state in ("SUSPECT", "RECOVERY_PENDING", "OFFLINE", ""):
        assert restore_by_id.continuation(recorded, state=state,
                                          restored_to={"last_backup_finished": "2026-11-12 07:00:00"}) is None


# --------------------------------------------------------------------------- #
# The catalog: the chain since the last finished restore
# --------------------------------------------------------------------------- #
@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


_STARTED = iter(range(10_000))


def _run(catalog, files, *, phase, restore_id="R", target="TGT", result="applied"):
    # One entry's runs are told apart by when they started: each stand-in gets its own second.
    started = f"2026-11-13T08:{next(_STARTED) // 60:02d}:{next(_STARTED) % 60:02d}Z"
    run_id = catalog.start_restore_run(action="restore", restore_id=restore_id, source_server_id="SRC",
                                       target_server_id=target, copy_max_retry=3, started_at=started)
    catalog.plan_chain(run_id, [dict(source_server_id="SRC", target_server_id=target, database_name="APP",
                                     restored_as="APP_COPY", step="restore-log", file_name=name,
                                     file_path=f"/stage/{name}") for name in files])
    catalog.set_chain_step(run_id, list(range(1, len(files) + 1)), result=result)
    catalog.set_restore_run(run_id, phase=phase)
    return run_id


def test_the_unfinished_chain_is_every_run_since_the_last_one_that_finished(catalog):
    _run(catalog, ["old_full.bak"], phase="error")
    _run(catalog, ["done_full.bak"], phase="done")
    failed = _run(catalog, ["a.bak", "b.bak"], phase="error", result="failed")
    killed = _run(catalog, ["c.bak"], phase="restoring")       # a process that died never closed its row
    _run(catalog, ["other.bak"], phase="error", restore_id="ANOTHER_ENTRY")
    _run(catalog, ["elsewhere.bak"], phase="error", target="ANOTHER_TARGET")

    rows = catalog.unfinished_chain(restore_id="R", target_server_id="TGT")

    assert [(row["restore_run_id"], row["file_name"]) for row in rows] == [
        (failed, "a.bak"), (failed, "b.bak"), (killed, "c.bak")]
    assert rows[0]["run_phase"] == "error" and rows[0]["restored_as"] == "APP_COPY"


def test_an_entry_whose_newest_restore_finished_has_nothing_unfinished(catalog):
    _run(catalog, ["a.bak"], phase="error")
    _run(catalog, ["a.bak", "b.bak"], phase="done")

    assert catalog.unfinished_chain(restore_id="R", target_server_id="TGT") == []
    assert catalog.unfinished_chain(restore_id="NEVER_RAN", target_server_id="TGT") == []


# --------------------------------------------------------------------------- #
# What the target holds, asked with the record
# --------------------------------------------------------------------------- #
def _job(**over):
    values = dict(restore_id="R", db_type="sqlserver", server_id="SRC", target_server_id="TGT",
                  env={"MSSQL_USER": "sa"}, env_secrets={}, overwrite_existing=False,
                  landing=landing_of("APP"), target_container="", label="R (sqlserver)")
    values.update(over)
    return SimpleNamespace(**values)


def _target_says(monkeypatch, **databases):
    """``name=(state, last backup finished)`` as verify-restore answers it."""
    rows = [{"database_name": name, "state": state,
             "restored_to": {"last_backup_finished": last} if last else None}
            for name, (state, last) in databases.items()]
    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, **_: 1433)
    monkeypatch.setattr(restore_by_id.common_cli, "run_allowing_failure",
                        lambda command, request: (False, {"databases": rows}, ""))


class _Recorded:
    def __init__(self, rows):
        self.rows, self.asked = rows, []

    def unfinished_chain(self, **who):
        self.asked.append(who)
        return self.rows


def test_the_runs_own_unfinished_database_is_continued_and_not_refused(monkeypatch):
    _target_says(monkeypatch, APP=("RESTORING", "2026-11-12 06:00:00"))
    record = _Recorded([{**row, "restored_as": "APP"} for row in _recorded(FULL, DIFF, *LOGS, result="failed")])

    resume = restore_by_id._refuse_a_name_already_held(_job(), {}, host={"host": "h"}, data_dir=None, catalog=record)

    assert resume == {"app": {"restore_run_id": 7, "file_name": "APP_DIFF_0600.bak",
                              "after": "2026-11-12 06:00:00", "complete": False}}
    assert record.asked == [{"restore_id": "R", "target_server_id": "TGT"}]


def test_a_restoring_database_with_no_record_is_refused_and_says_why(monkeypatch):
    _target_says(monkeypatch, APP=("RESTORING", "2026-11-12 06:00:00"))

    with pytest.raises(restore_by_id.RestoreByIdError) as refused:
        restore_by_id._refuse_a_name_already_held(_job(), {}, host={"host": "h"}, data_dir=None,
                                                  catalog=_Recorded([]))

    said = str(refused.value)
    assert "APP exists on the target (RESTORING) and overwrite_existing is false" in said
    assert "not an unfinished restore of this entry that can be continued" in said
    assert "Nothing was copied or restored" in said


def test_an_entry_that_may_replace_restores_over_it_as_before(monkeypatch):
    """The operator: *overwrite_existing: true still overwrites as before* - nothing is asked, nothing continued."""
    asked = []
    monkeypatch.setattr(restore_by_id.common_cli, "run_allowing_failure",
                        lambda *a, **k: asked.append(a) or (False, {}, ""))

    assert restore_by_id._refuse_a_name_already_held(_job(overwrite_existing=True), {}, host={"host": "h"},
                                                     data_dir=None, catalog=_Recorded([])) == {}
    assert asked == []


def test_the_dry_run_says_which_database_goes_on_and_which_is_kept(monkeypatch):
    _target_says(monkeypatch, APP=("RESTORING", "2026-11-12 07:00:00"), HR=("ONLINE", "2026-11-12 09:00:00"),
                 NEW=("", ""))
    rows = ([{**row, "restored_as": "APP"} for row in _recorded(FULL, *LOGS, result="failed")]
            + [{**row, "restored_as": "HR"} for row in _recorded(FULL, *LOGS)])

    lands = restore_by_id._where_it_lands(_job(landing=landing_of("APP", "HR", "NEW")), {}, host={"host": "h"},
                                          data_dir=None, files=[], catalog=_Recorded(rows))
    will = {item["lands_as"]: item["will"] for item in lands}

    assert will["APP"].startswith("CONTINUED after APP_LOG_0700.bak - run 7 of this entry stopped there")
    assert will["HR"].startswith("KEPT - run 7 of this entry restored it to its end (2026-11-12 09:00:00)")
    assert will["NEW"].startswith("created")
    assert not [item for item in lands if item.get("refused")]


# --------------------------------------------------------------------------- #
# The plan of a continued run
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan(monkeypatch):
    def build(resume, *, diff=True, logs=LOGS, point_in_time=""):
        def listing(request):
            kinds = request.get("kinds") or []
            files = {"full": [FULL], "diff": [DIFF] if diff else [], "log": list(logs)}[kinds[0]]
            return {"files": files, "newest_finished_at": files[-1]["finished_at"] if files else ""}

        monkeypatch.setattr(restore_by_id, "_list_backup_files", listing)
        monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, **_: 1433)
        monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/stage")
        monkeypatch.setattr(restore_by_id.restore_integrity, "sqlserver_chain_break", lambda *a, **k: "")
        monkeypatch.setattr(restore_by_id, "source_offset_minutes", lambda files: 0)
        job = _job(landing=landing_of("APP"), checkdb=True)
        return restore_by_id._plan_sqlserver(job, {}, point_in_time=point_in_time, host={"host": "h"},
                                             data_dir=None, resume=resume)

    return build


def _ops(steps):
    """The plan's steps with their files; a database's log steps - one file each since 1.370 - read
    together as one, so a test states the chain rather than how it is cut into messages."""
    ops: list = []
    for s in steps:
        if not s["op"].startswith("restore-"):
            continue
        files = [f["path"].rsplit("/", 1)[-1] for f in s.get("chain") or []]
        if s["op"] == "restore-log":
            assert len(files) == 1, "one log file per step"
            if ops and ops[-1][0] == "restore-log":
                ops[-1][1].extend(files)
                continue
        ops.append((s["op"], files))
    return ops


def _after(item, **more):
    return {"app": {"restore_run_id": 7, "file_name": item["path"].rsplit("/", 1)[-1],
                    "after": item["finished_at"], "complete": False, **more}}


def test_a_plan_with_nothing_to_continue_is_the_whole_chain(plan):
    assert _ops(plan(None)) == [("restore-full", ["APP_FULL_0400.bak"]), ("restore-diff", ["APP_DIFF_0600.bak"]),
                                ("restore-log", ["APP_LOG_0700.bak", "APP_LOG_0800.bak", "APP_LOG_0900.bak"])]


@pytest.mark.parametrize("stopped_after, left", [
    (FULL, [("restore-diff", ["APP_DIFF_0600.bak"]),
            ("restore-log", ["APP_LOG_0700.bak", "APP_LOG_0800.bak", "APP_LOG_0900.bak"])]),
    (DIFF, [("restore-log", ["APP_LOG_0700.bak", "APP_LOG_0800.bak", "APP_LOG_0900.bak"])]),
    (LOGS[0], [("restore-log", ["APP_LOG_0800.bak", "APP_LOG_0900.bak"])]),
    (LOGS[1], [("restore-log", ["APP_LOG_0900.bak"])]),
])
def test_a_continued_plan_starts_at_the_file_after_the_last_one_applied(plan, stopped_after, left):
    steps = plan(_after(stopped_after))

    assert _ops(steps) == left
    assert [s for s in steps if s["op"] == "restore-full"] == [], "the full is never restored over what it left"
    last = [s for s in steps if s["op"].startswith("restore-")][-1]
    assert last["request"]["with_recovery"] is True, "the last step still recovers the database"
    note = next(s["continued"] for s in steps if s["op"] == "continued")
    assert note["from_run"] == 7 and note["complete"] is False
    assert note["skipped"] + note["left"] == 5 and note["left"] == sum(len(files) for _op, files in left)


def test_a_chain_with_no_differential_counts_its_logs_from_the_full(plan):
    assert _ops(plan(_after(FULL), diff=False)) == [
        ("restore-log", ["APP_LOG_0700.bak", "APP_LOG_0800.bak", "APP_LOG_0900.bak"])]
    assert _ops(plan(_after(LOGS[0]), diff=False)) == [("restore-log", ["APP_LOG_0800.bak", "APP_LOG_0900.bak"])]


def test_the_database_is_still_held_to_the_end_of_its_whole_chain(plan):
    expect = next(s["expect"] for s in plan(_after(LOGS[0])) if s["op"] == "expect")

    assert expect["chain_ends"] == "2026-11-12 09:00:00" and expect["database_name"] == "APP"


@pytest.mark.parametrize("stopped_after, why", [
    ({"path": "/stage/APP/FULL/APP_FULL_0100.bak", "finished_at": "2026-11-12 01:00:00"},
     "that file is not in the chain to restore now"),
    (LOGS[-1], "nothing of its chain is left to apply"),
])
def test_a_chain_that_does_not_go_on_from_that_file_is_refused_and_not_guessed(plan, stopped_after, why):
    with pytest.raises(restore_by_id.RestoreByIdError) as refused:
        plan(_after(stopped_after))

    said = str(refused.value)
    assert why in said and "run 7 of this entry stopped after" in said
    assert "It cannot be continued" in said and "Nothing was restored" in said


def test_a_database_already_restored_to_its_end_gets_no_step_and_keeps_its_own_stop_point(plan):
    steps = plan({"app": {"restore_run_id": 7, "file_name": "APP_LOG_0800.bak", "after": "2026-11-12 08:00:00",
                          "complete": True, "stopat": "2026-11-12 07:30:00"}})

    assert _ops(steps) == []
    assert next(s["expect"] for s in steps if s["op"] == "expect") == {
        "database_name": "APP", "source_database": "APP", "moment": "2026-11-12 07:30:00", "stopat": True,
        "chain_ends": "2026-11-12 08:00:00", "files": 0}
    assert steps[-1]["op"] == "verify-restore" and steps[-1]["request"]["database_names"] == ["APP"], \
        "it is still checked with the others"


def test_what_a_continued_run_says():
    said = restore_by_id.continued_said([
        {"database_name": "APP_COPY", "source_database": "APP", "complete": False, "from_run": 7,
         "after_file": "APP_LOG_0700.bak", "skipped": 3, "left": 2},
        {"database_name": "HR", "source_database": "HR", "complete": True, "from_run": 7,
         "after": "2026-11-12 09:00:00"}])

    assert said == ("APP as APP_COPY: continued after APP_LOG_0700.bak - run 7 stopped there; 3 file(s) already "
                    "applied, 2 to go; HR: kept as run 7 restored it (to 2026-11-12 09:00:00)")


# --------------------------------------------------------------------------- #
# The decryption: a plain file already proven in the staging is not decrypted again
# --------------------------------------------------------------------------- #
SHA_A, SHA_B = "a" * 64, "b" * 64
EXPECTED = {"APP/FULL/f.bak": {"plain_sha256": SHA_A, "plain_size_bytes": 100},
            "APP/LOG/l.trn": {"plain_sha256": SHA_B, "plain_size_bytes": 20}}


def _hashes(monkeypatch, files):
    asked = []

    def run(command, request, **_):
        asked.append((command, request["paths"]))
        if isinstance(files, Exception):
            raise files
        return {"files": files}

    monkeypatch.setattr(restore_script.common_cli, "run", run)
    monkeypatch.setattr(restore_script, "_ssh_host_block", lambda login: {"host": "t"})
    return asked


def _staged(staging):
    return restore_script._already_decrypted(staging, target_login={}, target_dir="/stage", expected=EXPECTED)


def test_a_file_the_staging_holds_as_the_recorded_plaintext_is_kept(monkeypatch):
    asked = _hashes(monkeypatch, [{"path": "/stage/APP/FULL/f.bak", "sha256": SHA_A.upper(), "size_bytes": 100}])

    kept = _staged({"pending": [{"path": "APP/LOG/l.trn", "size_bytes": 20}]})

    assert kept == {"APP/FULL/f.bak"}
    assert asked == [("file-checksum", ["/stage/APP/FULL/f.bak"])], "only what is there at its size is hashed"


@pytest.mark.parametrize("found", [
    [{"path": "/stage/APP/FULL/f.bak", "sha256": SHA_B, "size_bytes": 100}],        # another file's bytes
    [{"path": "/stage/APP/FULL/f.bak", "sha256": SHA_A, "size_bytes": 99}],
    [{"path": "/stage/APP/FULL/f.bak", "sha256": "", "size_bytes": 100}],
    [],
    OSError("the target did not answer"),
])
def test_a_file_that_is_not_proven_is_decrypted_again(monkeypatch, found):
    _hashes(monkeypatch, found)

    assert _staged({"pending": [{"path": "APP/LOG/l.trn", "size_bytes": 20}]}) == set()


def test_nothing_is_hashed_when_every_file_is_still_to_come_or_the_staging_did_not_say(monkeypatch):
    asked = _hashes(monkeypatch, [])

    assert _staged({"pending": [{"path": rel} for rel in EXPECTED]}) == set()
    assert _staged({}) == set(), "an answer that names no pending list proves nothing"
    assert asked == []
