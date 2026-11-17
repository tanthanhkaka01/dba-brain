"""A backup says its ten steps, in order, whatever its config - and each reaches the chat its notify says (1.374).

Why it matters: the operator, 2026-11-15, reading a night's DIFF - *these messages are not quite right*:
no start or end of the process, the repository's copy told before the backup's own end, step numbers
that were ranges. Decided 2026-11-16: 1. process started, 2. backup on the server started, 3. done,
4. copy started, 5. done, 6. encrypt started, 7. done, 8. this backup's plain files cleared, 9. the
repository's upkeep, 10. the process done - every level alike (*full, diff, log share one code path*),
said by ``run_workflow`` and nowhere under it (*the messages are handled in the outermost code*).

And (the operator, the same day): *write the tests of the backup's and the restore's notify to catch
every case - with a repository, without, with metadata, without, each switched on, switched off*. A
configuration nobody ran is still held: each case below is a config, run through the real workflow on a
real catalog with the hosts stood in, and every message it says is then routed through the real
``notify`` rules.
"""

from __future__ import annotations

import dataclasses
import hashlib
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import backup as B
from dbabrain.backup_restore import backup_messages as BM
from dbabrain.backup_restore import offload as O
from dbabrain.backup_restore import workflow as W
from dbabrain.backup_restore.server_metadata import ServerMetadataPlan
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import cleanup_retention as R
from dbabrain.lib.notify import NotifyConfig, NotifyRule
from dbabrain.lib.notify_route import resolve_chat_id
from dbabrain.lib.offload import BackupOffload
from dbabrain.lib.time_window import TimeWindow

SRC, REPO = "SRV-01", "REPO-01"


# --------------------------------------------------------------------------- #
# The world: a real catalog, the hosts stood in, every message captured
# --------------------------------------------------------------------------- #
class _Store:
    """The run rows: claimed, closed, and the offload's own rows read back for a waiting backup."""

    def __init__(self) -> None:
        self.runs: dict[int, dict] = {}
        self.order: list[str] = []

    def fetch_latest_job_runs_by_job_code(self):
        return {}

    def fetch_running_job_runs(self, _prefix=""):
        return [{"log_id": i} for i, run in self.runs.items() if run["status"] == "RUNNING"]

    def insert_job_run(self, run):
        log_id = len(self.runs) + 1
        self.runs[log_id] = {"status": "RUNNING", "job_code": run.job_code, "metadata_json": "{}"}
        return log_id

    def fetch_job_run(self, log_id):
        return {"metadata_json": "{}"}

    def update_job_run(self, **kwargs):
        self.runs[kwargs["log_id"]]["status"] = kwargs["status"]
        self.order.append("backup row closed")

    def fetch_terminal_job_runs(self, *, job_codes, since_created_at, limit=50):
        import json

        return [{"metadata_json": json.dumps(run["metadata"])} for run in reversed(list(self.runs.values()))
                if run.get("job_code") in job_codes and "metadata" in run]


class _World:
    def __init__(self, monkeypatch, tmp_path, *, offload=None, status="done", files=2, notify=None):
        self.catalog = BackupCatalogStore(tmp_path / "catalog.sqlite")
        self.catalog.initialize()
        self.store = _Store()
        self.events: list[dict] = []
        self.status = status
        self.files = files
        self.copy_fails: set[str] = set()
        self.encrypt_waits: set[str] = set()
        self.encrypt_fails: set[str] = set()
        self.sealed: set[int] = set()
        self.on_the_way = True
        self.job = B.BackupJob(
            backup_id="SRV_FULL", job="full", db_type="sqlserver", server_id=SRC, script="s.ps1",
            backup_dir="F:\\BK", cleanup_retention=86400, time_window=TimeWindow(), offload=offload,
            notify=notify or NotifyConfig(), server_metadata=ServerMetadataPlan())
        store, catalog = self.store, self.catalog
        from dbabrain.db.store import DbaBrainStore

        monkeypatch.setattr(DbaBrainStore, "from_config", classmethod(lambda cls, _c, **_k: store))
        monkeypatch.setattr(BackupCatalogStore, "from_config", classmethod(lambda cls, _c: catalog))
        monkeypatch.setattr(B.schedule, "reap_stale_runs", lambda **_k: [])
        monkeypatch.setattr(B, "load_backup_jobs", lambda _p: [self.job])
        monkeypatch.setattr(B, "resolve_backup_target", lambda *_a, **_k: None)
        monkeypatch.setattr(B, "execute_backup_job", self._execute)
        monkeypatch.setattr(W, "emit_backup_restore_event", self._said)
        monkeypatch.setattr(O, "entries_to_offload", lambda _p: O.entries_from_jobs([self.job]))
        monkeypatch.setattr(O.schedule, "start_run", self._start_run)
        monkeypatch.setattr(O.schedule, "finish_run", self._finish_run)
        monkeypatch.setattr(O, "open_hosts", lambda entry, **_k: (SimpleNamespace(repository_host={"host": "repo"}), {}))
        monkeypatch.setattr(O, "sealing_of", lambda entry, **_k: object() if self.on_the_way else None)
        monkeypatch.setattr(O, "offload_entry", self._copy)
        monkeypatch.setattr(O, "encrypt_copies", self._encrypt)
        monkeypatch.setattr(O, "encryption_state", lambda entry, **_k: (set(self.sealed), {}))
        monkeypatch.setattr(O, "_encryptable", lambda entry, catalog, _c: [
            r for r in catalog.repository_copies(backup_id=entry.backup_id) if int(r["file_id"]) not in self.sealed])
        monkeypatch.setattr(O, "release_sources", self._release)
        monkeypatch.setattr(O, "prune_repository", lambda entry, **_k: {"expired": 0, "refused": []})
        monkeypatch.setattr(O, "check_folder", lambda entry, **_k: {"clean": True, "files": len(self.sealed)})
        monkeypatch.setattr(O, "_key_until", lambda *_a: "2027-02-09")
        monkeypatch.setattr(W, "lockdown_on", lambda _c: False, raising=False)
        from dbabrain.db import lockdown_store

        monkeypatch.setattr(lockdown_store, "lockdown_on", lambda _c: False)

    # -- the backup on the server ------------------------------------------------
    def _execute(self, item, **_k):
        files = [{"database_name": "APPDB", "file_name": f"APPDB_{n}.bak", "file_type": "FULL",
                  "path": f"F:\\BK\\APPDB_{n}.bak", "size_bytes": 10, "sha256": hashlib.sha256(b"x").hexdigest(),
                  "verified": True, "engine_verify": "passed"} for n in range(self.files)]
        return B.BackupRunResult(job=item, status=self.status, exit_code=0 if self.status == "done" else 1,
                                 duration_ms=1, stdout="", stderr="", files=files,
                                 error_text=None if self.status == "done" else "the disk is full")

    # -- the offload's run rows --------------------------------------------------
    def _start_run(self, *, store, job_code, message, metadata, every_node=False):
        log_id = 1000 + len(self.store.runs)
        self.store.runs[log_id] = {"status": "RUNNING", "job_code": job_code}
        return log_id, "2026-11-16T03:10:00Z"

    def _finish_run(self, *, store, log_id, status, message, duration_ms, error_text, metadata):
        self.store.runs[log_id].update(status=status, metadata=metadata)
        return "2026-11-16T03:20:00Z"

    # -- the repository ------------------------------------------------------------
    def _copy(self, entry, *, catalog, hosts, rows, sleep, sealing):
        failed = [f"{r['file_name']}: FactMismatch" for r in rows if r["file_name"] in self.copy_fails]
        done = [r for r in rows if r["file_name"] not in self.copy_fails]
        for row in done:
            catalog.set_backup_file(int(row["file_id"]), offload_state="verified", offload_path=f"/r/{row['file_name']}",
                                    offload_server_id=REPO, offload_run_id=1,
                                    offload_verified_at="2026-11-16T03:15:00Z")
        for row in rows:
            if row["file_name"] in self.copy_fails:
                catalog.set_backup_file(int(row["file_id"]), offload_state="failed",
                                        offload_attempted_at="2026-11-16T03:15:00Z")
        answer = {"files": len(rows), "verified": len(done), "failed": failed}
        if sealing is not None:
            sealed_now = [r for r in done if r["file_name"] not in self.encrypt_waits | self.encrypt_fails]
            self.sealed.update(int(r["file_id"]) for r in sealed_now)
            answer["sealed"] = {"rows": len(sealed_now), "encrypted": len(sealed_now), "files": len(sealed_now),
                                "failed": [], "waiting": [], "key_id": 4, "on_the_way": True}
        return answer

    def _encrypt(self, entry, **_k):
        rows = [r for r in self.catalog.repository_copies(backup_id=entry.backup_id)
                if int(r["file_id"]) not in self.sealed]
        answer = {"rows": len(rows), "encrypted": 0, "files": 0, "failed": [], "waiting": [], "file_ids": [],
                  "waiting_ids": [], "failed_ids": [], "key_id": 4}
        for row in rows:
            if row["file_name"] in self.encrypt_waits:
                answer["waiting"].append(f"{row['file_name']}: still being encrypted")
                answer["waiting_ids"].append(int(row["file_id"]))
            elif row["file_name"] in self.encrypt_fails:
                answer["failed"].append(f"{row['file_name']}: AgeError")
                answer["failed_ids"].append(int(row["file_id"]))
            else:
                self.sealed.add(int(row["file_id"]))
                answer["encrypted"] += 1
                answer["file_ids"].append(int(row["file_id"]))
        return answer

    def _release(self, entry, *, catalog, hosts, rows, encrypted, sealed):
        for row in rows:
            catalog.set_backup_file(int(row["file_id"]), state="pruned", on_disk=0,
                                    source_deleted_at="2026-11-16T03:18:00Z", pruned_by="test")
        return {"deleted": len(rows), "kept": 0, "refused": [], "deleted_ids": [int(r["file_id"]) for r in rows]}

    # -- what was said -----------------------------------------------------------
    def _said(self, **event):
        self.events.append(event)
        self.store.order.append(str(event["metadata"].get("flow_title", "")))

    def backup(self, **kwargs):
        return W.run_backup(app_config=SimpleNamespace(), config_path="x", force=True, sleep=lambda _s: None, **kwargs)

    def offload_pass(self):
        return W.run_offloads(app_config=SimpleNamespace(), config_path="x", sleep=lambda _s: None)

    @property
    def titles(self) -> list[str]:
        return [str(e["metadata"]["flow_title"]).split(" · ", 1)[1] for e in self.events]

    @property
    def steps(self) -> list[str]:
        return [title.split(".", 1)[0] for title in self.titles]

    def lines_of(self, step: str) -> list[str]:
        [event] = [e for e in self.events if str(e["metadata"]["flow_title"]).split(" · ", 1)[1].startswith(f"{step}.")]
        return list(event["metadata"]["flow_lines"])


def _offload(**over) -> BackupOffload:
    values = dict(enabled=True, server_id=REPO, folder="/mnt/nas", copy_max_retry=1, scan="none", delete_source=True,
                  source_share="", source_username="", source_password_ref="", encrypt=True, repository_check=True,
                  source_keeps_newest_chain=False, retention=R.Retention(default=86400 * 30))
    values.update(over)
    return BackupOffload(**values)


@pytest.fixture
def world(monkeypatch, tmp_path):
    return lambda **kwargs: _World(monkeypatch, tmp_path, **kwargs)


# --------------------------------------------------------------------------- #
# The steps each config says, in order
# --------------------------------------------------------------------------- #
def test_without_a_repository_the_backup_says_1_2_3_and_its_own_10(world):
    w = world(offload=None)
    w.backup()
    assert w.steps == ["1", "2", "3", "10"]
    assert w.titles[-1] == "10. Backup process done"
    assert "4-9 off (no repository for this entry)" in w.lines_of("1")[1]
    assert "only - this entry has no repository" in w.lines_of("10")[1]


def test_with_its_repository_off_the_backup_says_the_same_as_with_none(world):
    w = world(offload=_offload(enabled=False))
    w.backup()
    assert w.steps == ["1", "2", "3", "10"]


def test_with_every_step_on_the_backup_says_all_ten_in_the_order_they_happen(world):
    w = world(offload=_offload())
    w.backup()
    # The copy encrypts on the way: 4 and 6 go together, 5 and 7 together (the operator allowed it).
    assert w.steps == ["1", "2", "3", "4", "6", "5", "7", "8", "9", "10"]
    assert w.titles == ["1. Backup process started", f"2. Backup on {SRC} started", f"3. Backup on {SRC} done",
                        f"4. Copy to {REPO} started", "6. Encrypt started", f"5. Copy to {REPO} done",
                        "7. Encrypt done", "8. Plain files cleared", "9. Repository upkeep done",
                        "10. Backup process done"]
    assert w.lines_of("8") == [f"✅ at the source: 2 of 2 deleted on {SRC}, file by file",
                               f"✅ at {REPO}: no plain file of this backup - each is .age"]
    assert "each .age" in w.lines_of("10")[1]


def test_step_3_is_said_before_the_backups_row_closes_so_no_copy_is_told_first(world):
    w = world(offload=_offload())
    w.backup()
    order = w.store.order
    assert order.index("backup row closed") > order.index(f"SRV_FULL · 3. Backup on {SRC} done")
    assert order.index("backup row closed") < order.index(f"SRV_FULL · 4. Copy to {REPO} started")


def test_with_encryption_off_steps_6_and_7_are_not_said_and_8_says_the_repository_keeps_it_plain(world):
    w = world(offload=_offload(encrypt=False))
    w.backup()
    assert w.steps == ["1", "2", "3", "4", "5", "8", "9", "10"]
    assert "6-7 encrypt off" in w.lines_of("1")[1]
    assert w.lines_of("8")[-1] == f"- at {REPO}: kept plain (encryption off for this entry)"
    assert "- late encryption: off (offload_encrypt false)" in w.lines_of("9")


def test_with_the_delete_off_step_8_says_the_files_stay_on_the_server(world):
    w = world(offload=_offload(delete_source=False))
    w.backup()
    assert w.lines_of("8")[0] == f"- at the source: kept on {SRC} (offload_delete_source false)"
    assert "- earlier backups at the source: off (offload_delete_source false)" in w.lines_of("9")
    assert "8 plain files: kept at the source (offload_delete_source false)" in w.lines_of("1")[1]


def test_with_the_check_off_step_9_says_so(world):
    w = world(offload=_offload(repository_check=False))
    w.backup()
    assert "- repository check: off (offload_repository_check false)" in w.lines_of("9")


def test_with_the_check_on_step_9_says_what_it_found(world):
    w = world(offload=_offload())
    w.backup()
    assert "✅ repository check: clean · 2 file(s)" in w.lines_of("9")


def test_with_no_retention_window_step_9_says_every_copy_is_kept(world):
    w = world(offload=_offload(retention=R.Retention(default=None)))
    w.backup()
    assert "- retention: off (offload_retention keeps every copy)" in w.lines_of("9")


def test_with_the_offload_skipped_the_backup_stops_at_3_and_a_later_pass_says_4_to_10(world):
    w = world(offload=_offload())
    w.backup(offload=False)
    assert w.steps == ["1", "2", "3"]
    w.offload_pass()
    assert w.steps == ["1", "2", "3", "4", "6", "5", "7", "8", "9", "10"]


# --------------------------------------------------------------------------- #
# A step that fails, or waits
# --------------------------------------------------------------------------- #
def test_a_backup_that_failed_on_its_server_says_3_failed_and_10_failed_at_3_with_the_reason(world):
    w = world(offload=_offload(), status="error", files=0)
    w.backup()
    assert w.steps == ["1", "2", "3", "10"]
    assert w.titles[2] == f"3. Backup on {SRC} FAILED"
    assert w.titles[3] == "10. Backup process FAILED at step 3"
    assert "❌ the disk is full" in w.lines_of("10")


def test_a_copy_that_failed_ends_the_process_at_step_5(world):
    w = world(offload=_offload())
    w.copy_fails = {"APPDB_1.bak"}
    w.backup()
    assert w.titles[w.steps.index("5")] == f"5. Copy to {REPO} FAILED"
    assert w.titles[-1] == "10. Backup process FAILED at step 5"
    assert "8" not in w.steps, "this backup's plain files are not cleared while a copy of it failed"


def test_an_encryption_that_failed_ends_the_process_at_step_7(world):
    w = world(offload=_offload())
    w.on_the_way = False
    w.encrypt_fails = {"APPDB_0.bak"}
    w.backup()
    assert w.titles[w.steps.index("7")] == "7. Encrypt FAILED"
    assert w.titles[-1] == "10. Backup process FAILED at step 7"


def test_an_encryption_still_running_ends_this_pass_and_a_later_pass_says_7_8_and_10(world):
    w = world(offload=_offload())
    w.on_the_way = False
    w.encrypt_waits = {"APPDB_1.bak"}
    w.backup()
    assert w.steps == ["1", "2", "3", "4", "6", "5", "7", "8", "9", "10"]
    assert w.titles[w.steps.index("7")] == "7. Encrypt waiting"
    assert w.titles[w.steps.index("8")] == "8. Plain files waiting"
    assert w.titles[-1] == "10. Backup process done", "not a failure: an earlier pass's age is still writing"
    assert any(line.startswith("⏳") and "later pass" in line for line in w.lines_of("10"))

    said = len(w.events)
    w.encrypt_waits = set()
    w.offload_pass()
    later = [str(e["metadata"]["flow_title"]).split(" · ", 1)[1] for e in w.events[said:]]
    assert later[:3] == ["7. Encrypt done", "8. Plain files cleared", "10. Backup process done"]


def test_a_backup_cut_by_the_files_per_pass_says_its_copy_in_part_and_ends_in_a_later_pass(world):
    w = world(offload=_offload(files_per_pass=1))
    w.backup()
    assert w.titles[w.steps.index("5")] == f"5. Copy to {REPO} done in part"
    assert "10" not in w.steps
    w.offload_pass()
    assert w.titles[-1] == "10. Backup process done"
    assert f"4. Copy to {REPO} started again" in w.titles


def test_a_pass_with_only_upkeep_to_do_says_step_9_alone(world, monkeypatch):
    w = world(offload=_offload())
    w.backup()
    said = len(w.events)
    # Nothing new to copy; an older copy past its window: the pass's work is the upkeep.
    monkeypatch.setattr(O, "_expirable", lambda entry, catalog: [{"file_id": 1}])
    monkeypatch.setattr(O, "prune_repository", lambda entry, **_k: {"expired": 1, "refused": []})
    w.offload_pass()
    assert [str(e["metadata"]["flow_title"]).split(" · ", 1)[1] for e in w.events[said:]] ==         ["9. Repository upkeep done"]
    assert f"✅ retention: 1 copy(ies) past their window deleted on {REPO}" in w.events[-1]["metadata"]["flow_lines"]


# --------------------------------------------------------------------------- #
# The instance's metadata bundle, carried beside the backup (1.376)
# --------------------------------------------------------------------------- #
def _exporting(w, monkeypatch, *, carried):
    w.job = dataclasses.replace(w.job, server_metadata=ServerMetadataPlan(enabled=True, repository_path="/r/SRV/_i"))
    monkeypatch.setattr(B, "export_for_backup", lambda *a, **k: {"ok": True, "status": "EXPORTED"})
    monkeypatch.setattr(B, "summarize_server_metadata", lambda result: dict(result))
    from dbabrain.backup_restore import instance_bundle_store

    def carry(entry, **kwargs):
        if carried is not True:
            raise carried
        return {"path": kwargs["repository_path"], "key_id": 4}

    monkeypatch.setattr(instance_bundle_store, "carry_to_repository", carry)


def test_a_backup_that_exported_its_metadata_carries_the_bundle_sealed_and_says_so(world, monkeypatch):
    w = world(offload=_offload())
    _exporting(w, monkeypatch, carried=True)
    w.backup()
    assert "✅ instance metadata bundle -> /r/SRV/_i · sealed (age key #4)" in w.lines_of("5")


def test_a_bundle_that_could_not_be_carried_is_said_and_the_backup_stands(world, monkeypatch):
    w = world(offload=_offload())
    _exporting(w, monkeypatch, carried=OSError("the repository did not answer"))
    w.backup()
    assert any(line.startswith("❌ instance metadata bundle not carried to /r/SRV/_i") for line in w.lines_of("5"))
    assert w.titles[-1] == "10. Backup process done"


@pytest.mark.parametrize("chain, line, level", [
    ({"pieces": 3}, "✅ RMAN chain recorded: 3 piece(s)", "logging"),
    ({"error": "RuntimeError: ssh timed out"}, "⚠ RMAN chain not recorded - RuntimeError: ssh timed out", "warning"),
])
def test_an_oracle_backup_says_its_recorded_chain_in_step_3_and_a_failed_one_only_warns(world, monkeypatch, chain,
                                                                                        line, level):
    # 0.40.0 (1.380): the chain a restore from the repository copies by, asked after the backup.
    w = world(offload=_offload())
    w.job = dataclasses.replace(w.job, db_type="oracle", record_chain=True)
    asked = []
    monkeypatch.setattr(W, "_record_rman_chain", lambda item, **k: asked.append(k["run_id"]) or chain)
    w.backup()
    step3 = next(e for e in w.events if str(e["metadata"]["flow_title"]).split(" · ", 1)[1].startswith("3."))
    assert any(said.startswith(line) for said in w.lines_of("3"))
    assert step3["level"] == level and "done" in step3["metadata"]["flow_title"], "the backup stands"
    assert asked and w.titles[-1] == "10. Backup process done"


# --------------------------------------------------------------------------- #
# Every level alike
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("job", ["full", "diff", "log"])
def test_full_diff_and_log_say_the_same_steps(world, job):
    """The operator, 2026-11-16: *full, diff, log have shared one code path from the start* - no rule
    in the code decides which level's start reaches the chat."""
    w = world(offload=_offload())
    w.job = dataclasses.replace(w.job, job=job)
    w.backup()
    assert w.steps == ["1", "2", "3", "4", "6", "5", "7", "8", "9", "10"]


def test_no_level_rule_is_left_in_the_code():
    for module, name in ((B, "is_long_level"), (O, "is_long_copy"), (B, "LONG_LEVELS"), (O, "LONG_COPY_BYTES")):
        assert not hasattr(module, name), f"{module.__name__}.{name}: a level rule the operator never asked for"


def test_no_proc_under_the_workflow_says_a_step():
    """*The messages are handled in the outermost code* - backup.py and offload.py answer, never say."""
    import inspect

    for module in (B, O):
        source = inspect.getsource(module)
        assert "backup_messages" not in source and "flow_title" not in source, module.__name__


# --------------------------------------------------------------------------- #
# Each message reaches the chat its notify says
# --------------------------------------------------------------------------- #
GROUPS = {"logging": "-100LOG", "error": "-100ERR", "backup": "-100BAK", "warning": "-100WARN", "critical": "-100CRIT"}


def _delivered(event, notify):
    level = str(event["level"]).lower()
    route = {"enabled": True, "alert": True, "chat_id": GROUPS.get(level, "")}
    return resolve_chat_id(level, notify, route=route, groups=GROUPS)


def _notify(logging_on=True, alert_on=True, chat="backup"):
    return NotifyConfig(logging_on_run=NotifyRule(enabled=logging_on, telegram_chat=chat),
                        alert_on_error=NotifyRule(enabled=alert_on, telegram_chat=chat))


@pytest.mark.parametrize("logging_on, alert_on", [(True, True), (True, False), (False, True), (False, False)])
@pytest.mark.parametrize("case", ["no repository", "every step", "copy failed", "backup failed"])
def test_each_step_reaches_the_chat_its_notify_says_and_no_other(world, logging_on, alert_on, case):
    notify = _notify(logging_on, alert_on)
    w = world(offload=None if case == "no repository" else _offload(), notify=notify,
              status="error" if case == "backup failed" else "done", files=0 if case == "backup failed" else 2)
    if case == "copy failed":
        w.copy_fails = {"APPDB_1.bak"}
    w.backup()
    assert w.events, "every case says something"
    for event in w.events:
        assert event["notify"] is notify, "each message carries its job's notify"
        failed = event["level"] in ("error", "critical", "warning")
        expected = "-100BAK" if (alert_on if failed else logging_on) else ""
        assert _delivered(event, notify) == expected, (event["metadata"]["flow_title"], event["level"])
    # A failed step is an error, so it is alert_on_error that decides it - never logging_on_run.
    if case in ("copy failed", "backup failed"):
        assert any(e["level"] == "error" for e in w.events)


def test_a_redirect_to_another_chat_and_an_explicit_chat_id_are_followed():
    event = {"level": "logging"}
    assert _delivered(event, _notify(chat="logging")) == "-100LOG"
    explicit = NotifyConfig(logging_on_run=NotifyRule(enabled=True, telegram_chat="backup", chat_id="-100OWN"))
    assert _delivered(event, explicit) == "-100OWN"


def test_a_successful_step_is_never_an_error_and_a_failed_one_always_is(world):
    w = world(offload=_offload())
    w.copy_fails = {"APPDB_0.bak"}
    w.backup()
    for event in w.events:
        failed = any(str(line).startswith("❌") for line in event["metadata"]["flow_lines"]) \
            or "FAILED" in event["metadata"]["flow_title"]
        assert (event["level"] == "error") == failed, event["metadata"]["flow_title"]


# --------------------------------------------------------------------------- #
# The words
# --------------------------------------------------------------------------- #
def test_the_plan_line_names_each_step_its_config_runs_and_each_it_has_off():
    assert BM.plan_line(server_id=SRC, offload=_offload()) == (
        f"steps: 2-3 backup on {SRC}, 4-5 copy -> {REPO}, 6-7 encrypt (age, on the way), "
        "8 plain files cleared (deleted at the source once proven), 9 repository upkeep, 10 done")
    assert BM.plan_line(server_id=SRC, offload=None) == \
        f"steps: 2-3 backup on {SRC}, 4-9 off (no repository for this entry), 10 done"


def test_a_flow_message_is_its_title_and_its_steps_with_no_json():
    from dbabrain.backup_restore import events

    text = events._format_telegram_message(level="logging", message="ignored", metadata={
        "command": "offload", "backup_id": "SRV_FULL", "output": {"big": "x" * 500},
        "flow_title": "SRV_FULL · 5. Copy to REPO done", "flow_lines": ["✅ 1/1 file(s)", "", "- next"]})
    head, *lines = text.split("\n")
    assert head.startswith("LOGGING|") and head.endswith("|SRV_FULL · 5. Copy to REPO done")
    assert lines == ["✅ 1/1 file(s)", "- next"] and "{" not in text


# --------------------------------------------------------------------------- #
# A file known lost is said once
# --------------------------------------------------------------------------- #
def test_a_file_already_found_missing_is_not_rechecked_on_every_later_run(tmp_path):
    catalog = BackupCatalogStore(tmp_path / "c.sqlite")
    catalog.initialize()
    sha = "a" * 64
    ids = [catalog.record_backup_file(server_id="SRV", backup_id="SRV_FULL", engine="sqlserver", file_name=n,
                                      file_type="FULL", file_path=f"/b/{n}", verdict="ok", size_bytes=1,
                                      checksum_after_backup=sha) for n in ("A.bak", "B.bak")]
    catalog.check_backup_file(ids[0], checksum_now=None, checked_by="scrub", by_run_id="1")   # found gone
    assert [r["file_name"] for r in catalog.files_to_check(backup_id="SRV_FULL")] == ["B.bak"]
