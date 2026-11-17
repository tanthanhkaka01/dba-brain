"""A restore says its eleven steps, in order, whatever its config - and each reaches the chat its notify says (1.375).

Why it matters: the operator, 2026-11-15 - *a restore's messages step by step, concrete, like the
backup's*: full and diff a start and a done per database, each log its own message numbered under its
step, the instance metadata and the certificate always said, also when there is none. Confirmed on
2026-11-16: 1 started, 2 copy, 3 metadata before, 4 certificate, 5 full, 6 diff, 7 log, 8 check,
9 metadata after, 10 plain files, 11 done - the same numbers for every engine, a step that does not
apply said with why; the workflow calls each step and says it, the procs under it only answer.

And (the operator, the same day): *write the tests of the backup's and the restore's notify to catch
every case - with a repository, without, with metadata, without, each switched on, switched off*. Each
case below is a config, run through the workflow on the steps of ``restore_by_id`` with the hosts stood
in, and every message it says is then routed through the real ``notify`` rules.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from conftest import landing_of
from dbabrain.backup_restore import restore_by_id, restore_script, workflow
from dbabrain.lib import restore_space
from dbabrain.lib.integrity import RestoreIntegrity
from dbabrain.lib.notify import NotifyConfig, NotifyRule
from dbabrain.lib.notify_route import resolve_chat_id

pytestmark = pytest.mark.usefixtures("restore_targets_are_lab")

FULL = {"path": "/stage/APP/FULL/APP_FULL.bak", "finished_at": "2026-11-12 04:35:48"}
DIFF = {"path": "/stage/APP/DIFF/APP_DIFF.bak", "finished_at": "2026-11-12 06:00:10"}
LOGS = [{"path": f"/stage/APP/LOG/APP_LOG_{n:02d}.trn", "finished_at": f"2026-11-12 0{n}:00:05"} for n in (7, 8)]


def _sqlserver_plan(*, diff=True, logs=2):
    steps = [{"op": "restore-full", "source_database": "APP", "chain": [FULL],
              "request": {"database_name": "APP_COPY", "backup_path": FULL["path"], "with_recovery": False}}]
    if diff:
        steps.append({"op": "restore-diff", "source_database": "APP", "chain": [DIFF],
                      "request": {"database_name": "APP_COPY", "backup_path": DIFF["path"], "with_recovery": False}})
    for number, item in enumerate(LOGS[:logs], start=1):
        steps.append({"op": "restore-log", "source_database": "APP", "chain": [item], "log_number": number,
                      "log_total": logs, "request": {"database_name": "APP_COPY", "backup_path": item["path"],
                                                     "with_recovery": number == logs}})
    return steps + [{"op": "verify-restore", "request": {}}]


PG_PLAN = [{"op": "restore-full", "request": {"db_type": "postgresql", "wal_dir": "/b/wal",
                                              "backup_paths": ["/b/base/F", "/b/base/I"]}},
           {"op": "verify-restore", "request": {"db_type": "postgresql"}}]
ORA_PLAN = [{"op": "restore-full", "request": {"mode": "duplicate", "oracle_sid": "FREE", "backup_location": "/s"}},
            {"op": "verify-restore", "request": {}}]


class _World:
    def __init__(self, monkeypatch, *, engine="sqlserver", remote=True, repository=False, metadata=False,
                 encrypted=False, notify=None, plan=None, fail_op=None, copy_fails=False):
        self.events: list[dict] = []
        self.notify = notify or NotifyConfig()
        values = dict(
            restore_id="R", db_type=engine, script="restore.sh", label=f"R ({engine})", server_id="SRC", target_server_id="TGT",
            target_container="c", is_remote=remote, env={"MSSQL_USER": "sa_restore"},
            env_secrets={"BACKUP_ENCRYPTION_PASSWORD": "ref"} if encrypted else {}, backup_dir="/b",
            source_backup_host_dir="/b", target_backup_dir="/stage", target_visible_dir="/stage",
            cleanup_retention=86400, copy_mode="auto", space_check=restore_space.SpaceCheck(),
            copy_selection="chain", copy_recent_hours=24, staging_marker="", landing=landing_of("APP"),
            integrity=RestoreIntegrity(copy_retry_interval_seconds=0), overwrite_existing=False, tier=None,
            restore_from="repository" if repository else "server", action="restore",
            server_metadata=SimpleNamespace(enabled=metadata))
        # A dataclass, as a loaded entry is: a copy-only run is the entry with its action replaced.
        job = dataclasses.make_dataclass("Job", list(values))(**values)
        self.job = job
        monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
        # The entry's own rules (a repository entry is a loaded ScriptRestore; the secret store holds the
        # certificate's password) are other files' - here they pass.
        monkeypatch.setattr(workflow, "_entry_action", lambda entry, copy_only: ("restore", None))
        monkeypatch.setattr("dbabrain.backup_restore.backup._load_secrets", lambda **_k: {})
        monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})
        monkeypatch.setattr(restore_by_id, "assert_target_is_not_source", lambda *a, **k: None)
        monkeypatch.setattr(restore_by_id, "_refuse_a_name_already_held", lambda *a, **k: {})
        monkeypatch.setattr(restore_by_id, "_secret", lambda ref, secrets, where: "pw")
        monkeypatch.setattr(restore_by_id, "_key_from_vault",
                            lambda job, **k: {"op": "restore-key", "request": {}, "done": True, "vault_row": 7})
        monkeypatch.setattr(restore_by_id, "_sqlserver_target", lambda *a, **k: {"host": "h"})
        chosen = plan if plan is not None else (PG_PLAN if engine == "postgresql" else ORA_PLAN
                                                if engine == "oracle" else _sqlserver_plan())
        monkeypatch.setitem(restore_by_id._PLANNERS, engine, lambda *a, **k: [dict(s) for s in chosen])
        monkeypatch.setattr("dbabrain.backup_restore.backup.resolve_ssh_target",
                            lambda *a, **k: SimpleNamespace(host="h", server_id="X"))

        def transfer(*a, **k):
            if copy_fails:
                raise RuntimeError("prepare-staging failed: target not writable")
            return {"copied": 4, "skipped": 0, "bytes_copied": 400, "encrypted": repository,
                    "files": [{"path": "/stage/a", "size_bytes": 1, "sha256": "a" * 64}] if repository else [],
                    "restore_run_id": None}

        monkeypatch.setattr(restore_script, "transfer_backup_to_target", transfer)
        monkeypatch.setattr(restore_script, "staged_copies_of_entry", lambda *a, **k: [])
        monkeypatch.setattr(restore_script, "delete_decrypted_staging", lambda job, **k: {
            "plain_files": len(k["files"]), "deleted": len(k["files"]), "already_gone": 0, "bytes_deleted": 1})
        monkeypatch.setattr(restore_script, "prune_staged_backups", lambda *a, **k: {"pruned": 0})
        monkeypatch.setattr(restore_script, "replay_metadata_phase",
                            lambda job, *, phase, data_dir=None, on_phase=None: ("", {"ok": True, "target": "TGT"}))

        def execute(op, request):
            if op == fail_op:
                raise restore_by_id.RestoreByIdError(f"{op} failed: Msg 3201")
            if op == "verify-restore":
                return {"ok": True, "checked": 1, "failed": 0, "databases": []}
            return {"ok": True}

        monkeypatch.setattr(restore_by_id, "_execute", execute)
        monkeypatch.setattr(workflow, "emit_backup_restore_event", lambda **k: self.events.append(k))

    def run(self, *, copy_only=False):
        say = workflow._RestoreSay(app_config=None, logger=None, notify=self.notify, metadata={}, restore_id="R",
                                   engine=self.job.db_type, target="TGT", source="SRC")
        say.started(source="SRC", moment="", copy_only=copy_only,
                    from_repository=self.job.restore_from == "repository", remote=self.job.is_remote)
        try:
            outcome = workflow.run_restore_by_id({"restore_id": "R", **({"copy_only": True} if copy_only else {})},
                                                 say=say)
            say.finished(status="done", copy_only=copy_only, restored=workflow.restored_said(outcome),
                         warnings=outcome.get("warnings") or [], seconds=1)
        except Exception as exc:  # noqa: BLE001 - its failure is step 11's to say
            say.finished(status="error", error=str(exc), seconds=1)
        return self

    @property
    def titles(self):
        return [str(e["metadata"]["flow_title"]).split(" · ", 1)[1] for e in self.events]

    @property
    def numbers(self):
        return [title.split(" ", 1)[0].rstrip(".").rstrip(":") for title in self.titles]

    def said(self, start: str) -> str:
        return next(e["message"] for e in self.events if str(e["metadata"]["flow_title"]).split(" · ", 1)[1]
                    .startswith(start))


@pytest.fixture
def world(monkeypatch):
    return lambda **kwargs: _World(monkeypatch, **kwargs)


# --------------------------------------------------------------------------- #
# The steps each config says, in order
# --------------------------------------------------------------------------- #
def test_a_sql_server_restore_from_its_server_says_all_eleven_in_order(world):
    w = world().run()
    assert w.numbers == ["1", "2", "2", "3", "4", "5.1", "5.1", "6.1", "6.1", "7.1", "7.2", "8", "8", "9", "10", "11"]
    # Done - with the warning a test run has: no catalog is open to record the chain in.
    assert w.titles[0] == "1. Restore process started" and w.titles[-1].startswith("11. Restore process done")


def test_from_the_repository_the_copy_says_it_decrypts_and_step_10_deletes_what_it_decrypted(world):
    w = world(repository=True).run()
    assert "from the repository: the encrypted copies of SRC, decrypted on the way" in w.said("2. Copy to TGT started")
    assert "decrypted from the repository's encrypted copies" in w.said("2. Copy to TGT done")
    assert "1 of 1 plain file(s) deleted on TGT" in w.said("10. Plain files cleared")


def test_step_11_says_the_plain_files_size_in_the_form_step_10_says_it():
    # A lab drill, 2026-11-16: step 10 said 2.6 GB and step 11 the same files as 2746138112 bytes.
    said = workflow.restored_said({"plan": "p", "plain_deleted": {"deleted": 15, "plain_files": 15,
                                                                  "bytes_deleted": 2746138112}})
    assert said["plain_deleted"] == "15 of 15 staged copy(ies) (2.6 GB)"


def test_from_the_server_nothing_was_decrypted_and_step_10_says_so(world):
    w = world(repository=False).run()
    assert "nothing was decrypted or staged plain" in w.said("10. Plain files cleared")


def test_in_place_steps_2_and_10_are_said_as_not_applying(world):
    w = world(remote=False).run()
    assert w.said("2. Copy: none").endswith("the target reads the source's own folder - nothing is copied")
    assert w.said("10. Plain files cleared: none").endswith("nothing was staged on the target for this restore")


@pytest.mark.parametrize("metadata, said", [(False, "not restored - server_metadata is off for this entry"),
                                            (True, "done - ✅ replayed onto TGT")])
def test_the_metadata_is_said_before_and_after_whether_it_is_on_or_off(world, metadata, said):
    w = world(metadata=metadata).run()
    assert said in w.said("3. Instance metadata before the restore")
    assert said in w.said("9. Instance metadata after the restore")


@pytest.mark.parametrize("engine", ["postgresql", "oracle"])
def test_postgresql_and_oracle_say_their_metadata_is_inside_the_physical_backup(world, engine):
    w = world(engine=engine).run()
    assert "logins, roles and grants are inside the physical backup" in w.said("3. Instance metadata before")
    assert w.said("4. Certificate: none").endswith(f"{engine} has no certificate step")


@pytest.mark.parametrize("encrypted, said", [(True, "4. Certificate restored"),
                                             (False, "4. Certificate: none - none needed - the backup is not encrypted")])
def test_the_certificate_is_said_whether_the_backup_is_encrypted_or_not(world, encrypted, said):
    w = world(encrypted=encrypted).run()
    assert w.said("4. Certificate").startswith(said)


def test_a_postgresql_restore_says_its_diff_and_log_are_part_of_the_base_backup(world):
    w = world(engine="postgresql").run()
    assert w.said("6. Restore diff: none").endswith("the incrementals are combined into the base backup")
    assert w.said("7. Restore log: none").endswith("the WAL is replayed with the base backup")


def test_an_oracle_restore_says_its_diff_and_log_are_inside_the_duplicate(world):
    w = world(engine="oracle").run()
    assert w.said("6. Restore diff: none").endswith("inside the RMAN duplicate")
    assert w.said("7. Restore log: none").endswith("the archived logs are applied inside the RMAN duplicate")


def test_a_sql_server_restore_with_no_diff_says_so(world):
    w = world(plan=_sqlserver_plan(diff=False)).run()
    assert w.said("6. Restore diff: none").endswith("no differential after the full")


def test_a_copy_only_run_says_its_copy_and_its_end_and_nothing_between(world):
    w = world().run(copy_only=True)
    assert w.numbers == ["1", "2", "2", "11"]
    assert w.titles[0] == "1. Copy only process started" and w.titles[-1] == "11. Copy only process done"


# --------------------------------------------------------------------------- #
# A step that fails
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fail_op, at", [("restore-full", "5"), ("restore-diff", "6"), ("restore-log", "7"),
                                         ("verify-restore", "8")])
def test_a_step_that_fails_ends_the_restore_at_that_step(world, fail_op, at):
    w = world(fail_op=fail_op).run()
    assert w.titles[-1] == f"11. Restore process FAILED at step {at}"
    assert "Msg 3201" in w.events[-1]["message"]


def test_a_copy_that_fails_ends_the_restore_at_step_2(world):
    w = world(copy_fails=True).run()
    assert w.numbers == ["1", "2", "11"] and w.titles[-1] == "11. Restore process FAILED at step 2"


# --------------------------------------------------------------------------- #
# Each message reaches the chat its notify says
# --------------------------------------------------------------------------- #
GROUPS = {"logging": "-100LOG", "error": "-100ERR", "restore": "-100RST", "warning": "-100WARN"}


def _notify(logging_on=True, alert_on=True, chat="restore"):
    return NotifyConfig(logging_on_run=NotifyRule(enabled=logging_on, telegram_chat=chat),
                        alert_on_error=NotifyRule(enabled=alert_on, telegram_chat=chat))


def _delivered(event, notify):
    level = str(event["level"]).lower()
    return resolve_chat_id(level, notify, route={"enabled": True, "alert": True, "chat_id": GROUPS.get(level, "")},
                           groups=GROUPS)


@pytest.mark.parametrize("logging_on, alert_on", [(True, True), (True, False), (False, True), (False, False)])
@pytest.mark.parametrize("case", [dict(), dict(repository=True), dict(metadata=True), dict(remote=False),
                                  dict(engine="postgresql"), dict(engine="oracle"), dict(fail_op="restore-log")])
def test_each_step_reaches_the_chat_its_notify_says_and_no_other(world, logging_on, alert_on, case):
    notify = _notify(logging_on, alert_on)
    w = world(notify=notify, **case).run()
    assert w.events
    for event in w.events:
        assert event["notify"] is notify, "each message carries its entry's notify"
        alerting = event["level"] in ("error", "critical", "warning")
        assert _delivered(event, notify) == ("-100RST" if (alert_on if alerting else logging_on) else ""), \
            (event["metadata"]["flow_title"], event["level"])
    if "fail_op" in case:
        assert w.events[-1]["level"] == "error", "a failed restore's end is alert_on_error's to send"


def test_a_metadata_phase_that_failed_is_a_warning_and_the_restore_still_ends_done(world, monkeypatch):
    w = world(metadata=True)
    monkeypatch.setattr(restore_script, "replay_metadata_phase",
                        lambda job, *, phase, data_dir=None, on_phase=None: ("", {"ok": False, "error": "login failed"}))
    w.run()
    pre = next(e for e in w.events if "3. Instance metadata" in e["metadata"]["flow_title"])
    assert pre["level"] == "warning" and "login failed" in pre["message"]
    assert w.titles[-1] == "11. Restore process done with a warning"
    assert "instance metadata (pre-database) was asked for and not replayed" in w.events[-1]["message"]


# --------------------------------------------------------------------------- #
# Who says the steps
# --------------------------------------------------------------------------- #
def test_no_proc_under_the_workflow_says_a_restore_step():
    """*The workflow calls each step and says it* - restore_by_id and restore_database only answer."""
    import inspect

    from dbabrain.backup_restore import restore_database

    for module in (restore_by_id, restore_database):
        source = inspect.getsource(module)
        assert "restore_messages" not in source and "flow_title" not in source, module.__name__
        assert "announce(" not in source, module.__name__


def test_a_decrypted_file_that_is_not_its_backup_ends_the_restore_at_step_2_with_its_facts(world, monkeypatch):
    """The operator, 2026-11-17: *a decrypted file whose bytes or checksum differ stops at once and says
    so*. From the repository, the copy is the decryption: its refusal is step 2's, the end names it
    with the bytes and hashes that differed, and no RESTORE is sent for any database."""
    from dbabrain.lib.common_cli import CommonCliError

    w = world(repository=True)
    reason = (f"/mnt/nas/SRC/APP/FULL/APP_FULL.bak.age: not restored - it decrypts to 9 bytes / {'1' * 64}, "
              f"not the plain file's 10 / {'a' * 64}.")

    def transfer(*a, **k):
        raise CommonCliError(reason)

    monkeypatch.setattr(restore_script, "transfer_backup_to_target", transfer)
    sent = []
    monkeypatch.setattr(restore_by_id, "_execute", lambda op, request: sent.append(op) or {"ok": True})

    w.run()

    assert w.numbers == ["1", "2", "11"] and w.titles[-1] == "11. Restore process FAILED at step 2"
    assert "decrypts to 9 bytes" in w.events[-1]["message"] and "not the plain file's 10" in w.events[-1]["message"]
    assert not [op for op in sent if op.startswith("restore-")], f"a RESTORE was sent after the copy failed: {sent}"
