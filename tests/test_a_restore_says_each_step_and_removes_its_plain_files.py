"""A restore says each step of its chain, and removes the plain files it staged once it has worked (0.39.0, 1.370, 1.371).

The operator, 2026-11-13 (translated): *restore is like backup - it must message every step of its
flow the same way; restore has restore full -> diff -> log as well, a message for each* - and - *a
successful restore must delete the plain files too (both at the repository and on the target
server_id)*.

**Each step.** A restore announced its copy, then *restoring <the plan>*, then *N restore step(s)
finished* - one line for a full that takes an hour and the forty logs after it, and when a step
failed the chat said the engine's error with no word of which backup it was applying. Now each step
of the chain - the full, the differential, the logs, per database - is its own message as it
finishes, numbered, with the backup it applied and whether it left the database open; the failing
one is named the same way in the error.

**The plain files.** The repository keeps a backup encrypted; a restore lays its chain down in the
clear in the target's staging (on the repository host itself when the drill runs there). Those
copies stayed for the entry's whole ``cleanup_retention``. Now they go as soon as the restored
databases are checked - each by a verified delete held to the bytes and sha256 its decryption
proved - and only then: a failed restore keeps them, which is what a continued run reads.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import landing_of

from dbabrain.backup_restore import backup as backup_module
from dbabrain.backup_restore import restore_by_id, restore_script, verified_cleanup

# Its restore targets are lab machines the tests do not list in an inventory; a target whose
# environment nothing states is refused since 0.39.0 (D-39.6) - see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("restore_targets_are_lab")

FULL = {"path": "/stage/APP/FULL/APP_FULL.bak", "finished_at": "2026-11-12 04:35:48"}
DIFF = {"path": "/stage/APP/DIFF/APP_DIFF.bak", "finished_at": "2026-11-12 06:00:10"}
LOGS = [{"path": f"/stage/APP/LOG/APP_LOG_{hour:02d}.trn", "finished_at": f"2026-11-12 {hour:02d}:00:05"}
        for hour in (7, 8, 9)]


def _steps(stopat=""):
    return [
        {"op": "restore-full", "source_database": "APP", "chain": [FULL],
         "request": {"database_name": "APP_COPY", "backup_path": FULL["path"], "with_recovery": False}},
        {"op": "restore-diff", "source_database": "APP", "chain": [DIFF],
         "request": {"database_name": "APP_COPY", "backup_path": DIFF["path"], "with_recovery": False}},
        {"op": "restore-log", "source_database": "APP", "chain": LOGS,
         "request": {"database_name": "APP_COPY", "backup_paths": [f["path"] for f in LOGS],
                     "with_recovery": True, **({"stopat": stopat} if stopat else {})}},
        {"op": "expect", "expect": {}},
        {"op": "verify-restore", "request": {}},
    ]


def _steps_one_log_each():
    """The plan as the SQL Server planner writes it since 1.370: one step per log file, the recovery
    on the last."""
    full, diff, _logs, *rest = _steps()
    logs = [{"op": "restore-log", "source_database": "APP", "chain": [item], "log_number": number,
             "log_total": len(LOGS),
             "request": {"database_name": "APP_COPY", "backup_path": item["path"],
                         "with_recovery": number == len(LOGS)}}
            for number, item in enumerate(LOGS, start=1)]
    return [full, diff, *logs, *rest]


# --------------------------------------------------------------------------- #
# 1.370 - the words of one step
# --------------------------------------------------------------------------- #
def test_a_full_a_differential_and_the_logs_are_each_said_with_their_backup():
    full, diff, logs = _steps(stopat="2026-11-12 08:30:00")[:3]

    assert restore_by_id.step_said(full) == (
        "APP as APP_COPY: full APP_FULL.bak (backup finished 2026-11-12 04:35:48) - left restoring for the next step")
    assert restore_by_id.step_said(diff) == (
        "APP as APP_COPY: diff APP_DIFF.bak (backup finished 2026-11-12 06:00:10) - left restoring for the next step")
    assert restore_by_id.step_said(logs) == (
        "APP as APP_COPY: 3 log(s) (backups finished 2026-11-12 07:00:05 .. 2026-11-12 09:00:05), "
        "stopped at 2026-11-12 08:30:00 - recovered, the database is open")


def test_a_whole_instance_step_is_said_as_its_plan_says_it():
    duplicate = {"op": "restore-full", "request": {"mode": "duplicate", "oracle_sid": "ORCL",
                                                   "backup_location": "/stage/rman"}}

    assert restore_by_id.step_said(duplicate).startswith("RMAN DUPLICATE of ORCL from /stage/rman")


def test_a_step_that_cannot_be_described_is_named_by_its_command_and_fails_nothing():
    assert restore_by_id.step_said({"op": "restore-log", "request": None, "chain": "not a list"}) == "restore-log"
    assert restore_by_id.step_said({}) == "a step"


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
@pytest.fixture
def run(monkeypatch):
    """One restore through ``restore_by_id`` with the hosts stood in for: what it announced, what it
    deleted, what it answered."""
    def start(*, fail_at="", remote=False, transferred=None, delete=None, steps=None, fail_path=""):
        job = SimpleNamespace(
            restore_id="R", db_type="sqlserver", server_id="SRC", target_server_id="TGT", env_secrets={},
            is_remote=remote, label="R (sqlserver)", landing=landing_of("APP"), overwrite_existing=False,
            integrity=None, tier=None, target_backup_dir="/stage", cleanup_retention=86400,
            # The entry's own rules for a repository source are another file's; here the copy is stood
            # in for, and what it answers - encrypted or not - is what decides the clean-up.
            restore_from="server", server_metadata=SimpleNamespace(enabled=False))
        said: list[tuple[str, str, dict]] = []
        deleted: list[dict] = []
        monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
        monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})
        monkeypatch.setattr(restore_by_id, "assert_target_is_not_source", lambda *a, **k: None)
        monkeypatch.setattr(restore_by_id, "_refuse_a_name_already_held", lambda *a, **k: {})
        monkeypatch.setitem(restore_by_id._PLANNERS, "sqlserver", lambda *a, **k: steps or _steps())
        monkeypatch.setattr(backup_module, "resolve_ssh_target", lambda server_id, **_: SimpleNamespace(server_id=server_id))
        monkeypatch.setattr(restore_script, "transfer_backup_to_target", lambda *a, **k: dict(transferred or {}))
        monkeypatch.setattr(restore_script, "prune_staged_backups",
                            lambda *a, **k: {"pruned": 0, "retention_seconds": 86400})

        def delete_plain(job_, **kwargs):
            deleted.append(kwargs)
            if isinstance(delete, Exception):
                raise delete
            return delete or {"plain_files": len(kwargs["files"]), "deleted": len(kwargs["files"]),
                              "already_gone": 0, "bytes_deleted": 130}

        monkeypatch.setattr(restore_script, "delete_decrypted_staging", delete_plain)

        def execute(op, request):
            if op == fail_at or (fail_path and request.get("backup_path") == fail_path):
                raise restore_by_id.RestoreByIdError("Msg 3201: cannot open backup device")
            return {"ok": True, "checked": 1, "failed": 0, "databases": []}

        monkeypatch.setattr(restore_by_id, "_execute", execute)

        def go():
            return restore_by_id.restore_by_id(
                {"restore_id": "R"}, on_phase=lambda phase, message, extra: said.append((phase, message, extra)))

        return go, said, deleted

    return start


def test_each_step_of_the_chain_is_a_message_of_its_own_in_order(run):
    go, said, _deleted = run()
    go()

    phases = [phase for phase, _message, _extra in said]
    assert phases == ["METADATA_SKIP", "RESTORE_START", "RESTORE_STEP", "RESTORE_STEP", "RESTORE_STEP",
                      "RESTORE_DONE", "VERIFY_START", "VERIFY_DONE", "METADATA_SKIP"][:len(phases)]
    steps = [(message, extra) for phase, message, extra in said if phase == "RESTORE_STEP"]
    assert [extra["step"] for _message, extra in steps] == ["restore-full", "restore-diff", "restore-log"]
    assert [(extra["step_number"], extra["steps"], extra["files"]) for _message, extra in steps] == [
        (1, 3, 1), (2, 3, 1), (3, 3, 3)]
    assert steps[0][0].startswith("Restore R: step 1 of 3 done in ")
    assert "APP as APP_COPY: full APP_FULL.bak" in steps[0][0]
    assert "3 log(s)" in steps[2][0] and steps[2][0].endswith("recovered, the database is open.")


def test_each_log_file_is_a_message_of_its_own_naming_the_file(run):
    """The operator, 2026-11-14: *one message for each log file*. 46 logs were one message with a
    count; each is its own now, as it goes in, naming the file and where it stands among the logs."""
    go, said, _deleted = run(steps=_steps_one_log_each())
    go()

    steps = [(message, extra) for phase, message, extra in said if phase == "RESTORE_STEP"]
    assert [extra["step"] for _message, extra in steps] == [
        "restore-full", "restore-diff", "restore-log", "restore-log", "restore-log"]
    assert [(extra["step_number"], extra["steps"], extra["files"]) for _message, extra in steps] == [
        (1, 5, 1), (2, 5, 1), (3, 5, 1), (4, 5, 1), (5, 5, 1)]
    logs = [message for message, extra in steps if extra["step"] == "restore-log"]
    assert "APP as APP_COPY: log APP_LOG_07.trn (1 of 3 logs) (backup finished 2026-11-12 07:00:05)" in logs[0]
    assert logs[0].endswith("left restoring for the next step.")
    assert "log APP_LOG_09.trn (3 of 3 logs)" in logs[2] and logs[2].endswith("recovered, the database is open.")
    title = [extra["flow_title"].split(" · ", 1)[-1] for _message, extra in steps]
    assert title[2:] == ["2. Restore log done (3 of 5)", "2. Restore log done (4 of 5)", "2. Restore log done (5 of 5)"]


def test_a_log_that_fails_is_named_by_its_file_and_the_ones_before_it_were_said(run):
    go, said, _deleted = run(steps=_steps_one_log_each(), fail_path=LOGS[1]["path"])

    with pytest.raises(restore_by_id.RestoreByIdError) as failed:
        go()

    assert str(failed.value).startswith("R: step 4 of 5 failed - APP as APP_COPY: log APP_LOG_08.trn (2 of 3 logs)")
    assert [extra["step"] for phase, _m, extra in said if phase == "RESTORE_STEP"] == [
        "restore-full", "restore-diff", "restore-log"], "the first log went in and was said; the second is the error's"


def test_the_plan_counts_a_databases_logs_and_names_a_single_one():
    assert restore_by_id._plan_summary(_steps_one_log_each()) == (
        "APP as APP_COPY: full APP_FULL.bak + diff APP_DIFF.bak + 3 log(s)")
    one = _steps_one_log_each()[:3]
    assert restore_by_id._plan_summary(one) == "APP as APP_COPY: full APP_FULL.bak + diff APP_DIFF.bak + log APP_LOG_07.trn"


def test_the_check_and_the_certificate_are_not_numbered_among_the_chains_steps(run):
    go, said, _deleted = run()
    go()

    assert len([phase for phase, *_ in said if phase == "RESTORE_STEP"]) == 3
    done = next(extra for phase, _message, extra in said if phase == "RESTORE_DONE")
    assert done["steps"] == 3


def test_a_step_that_fails_is_named_with_its_number_and_its_backup(run):
    go, said, _deleted = run(fail_at="restore-diff")

    with pytest.raises(restore_by_id.RestoreByIdError) as failed:
        go()

    text = str(failed.value)
    assert text.startswith("R: step 2 of 3 failed - APP as APP_COPY: diff APP_DIFF.bak")
    assert text.endswith("Msg 3201: cannot open backup device")
    assert [extra["step"] for phase, _m, extra in said if phase == "RESTORE_STEP"] == ["restore-full"], \
        "the step that went in was said; the one that failed is the error's to say"


def test_a_failure_of_another_kind_is_raised_as_it_is(run, monkeypatch):
    go, _said, _deleted = run()
    monkeypatch.setattr(restore_by_id, "_execute", lambda op, request: (_ for _ in ()).throw(OSError("link down")))

    with pytest.raises(OSError, match="link down"):
        go()


# --------------------------------------------------------------------------- #
# 1.371 - the plain files, after a restore that worked
# --------------------------------------------------------------------------- #
PLAIN = [{"path": "/stage/APP/FULL/APP_FULL.bak", "size_bytes": 100, "sha256": "a" * 64, "key_id": 1},
         {"path": "/stage/APP/LOG/APP_LOG_07.trn", "size_bytes": 30, "sha256": "b" * 64, "key_id": 1}]
ENCRYPTED = {"copied": 2, "bytes_copied": 130, "skipped": 0, "encrypted": True, "files": PLAIN,
             "restore_run_id": None}


def test_a_restore_that_worked_deletes_the_files_it_decrypted_and_says_so(run):
    go, said, deleted = run(remote=True, transferred=ENCRYPTED)

    outcome = go()

    assert [call["files"] for call in deleted] == [PLAIN], "the files this restore decrypted, by the facts it proved"
    assert outcome["plain_deleted"] == {"plain_files": 2, "deleted": 2, "already_gone": 0, "bytes_deleted": 130}
    assert not [warning for warning in outcome["warnings"] if "decrypted" in warning]
    start = next(message for phase, message, _e in said if phase == "DELETE_START")
    done, extra = next((message, e) for phase, message, e in said if phase == "DELETE_DONE")
    assert "the 2 file(s) this restore staged - the databases are restored and checked" in start
    assert done.startswith("Restore R: 2 of 2 staged copy(ies) deleted (130 bytes); ")
    assert extra["plain_deleted"] == 2 and extra["plain_files"] == 2
    copied = next(message for phase, message, _e in said if phase == "COPY_DONE")
    assert "decrypted from the repository's encrypted copies" in copied


def test_it_deletes_only_after_the_databases_are_checked(run):
    go, said, deleted = run(remote=True, transferred=ENCRYPTED)
    go()

    phases = [phase for phase, *_ in said]
    assert phases.index("VERIFY_DONE") < phases.index("DELETE_START")
    assert len(deleted) == 1


def test_a_restore_that_failed_keeps_what_it_staged(run):
    """What a continued run reads (1.372): nothing is deleted for a restore that did not finish."""
    go, said, deleted = run(remote=True, transferred=ENCRYPTED, fail_at="restore-log")

    with pytest.raises(restore_by_id.RestoreByIdError):
        go()

    assert deleted == [] and "DELETE_START" not in [phase for phase, *_ in said]


def test_a_failed_check_keeps_them_too(run):
    go, _said, deleted = run(remote=True, transferred=ENCRYPTED, fail_at="verify-restore")

    with pytest.raises(restore_by_id.RestoreByIdError):
        go()

    assert deleted == []


def test_a_copy_that_was_never_encrypted_is_left_to_the_retention(run):
    go, said, deleted = run(remote=True, transferred={"copied": 2, "bytes_copied": 130, "skipped": 0,
                                                      "files": PLAIN, "restore_run_id": None})

    outcome = go()

    assert deleted == [] and outcome["plain_deleted"] is None
    done = next(message for phase, message, _e in said if phase == "DELETE_DONE")
    assert done.startswith("Restore R: 0 staged file(s) removed")


@pytest.mark.parametrize("delete, said", [
    ({"plain_files": 2, "deleted": 1, "already_gone": 0, "bytes_deleted": 100,
      "error": "1 decrypted file(s) could not be deleted and are still in the clear on TGT - /stage/x: mismatch - sha256 differs"},
     "staging: 1 decrypted file(s) could not be deleted and are still in the clear on TGT"),
    (OSError("the target did not answer"), "still in the clear on TGT - OSError: the target did not answer"),
])
def test_a_file_that_could_not_be_deleted_is_a_warning_on_a_restore_that_worked(run, delete, said):
    go, _said, _deleted = run(remote=True, transferred=ENCRYPTED, delete=delete)

    outcome = go()

    about_the_plain_files = [warning for warning in outcome["warnings"] if warning.startswith("staging:")]
    assert len(about_the_plain_files) == 1 and said in about_the_plain_files[0]
    assert outcome["verify"] == {"checked": 1, "failed": 0}, "the restore itself is done"


# --------------------------------------------------------------------------- #
# The delete itself: each file by the bytes and sha256 its decryption proved
# --------------------------------------------------------------------------- #
@pytest.fixture
def cleanup(monkeypatch):
    calls: list[dict] = []
    answer = {"deleted": [PLAIN[0]["path"], PLAIN[1]["path"]], "already_gone": [], "would_delete": [],
              "not_deleted": [], "kept": [], "forced": []}

    def delete_files(**kwargs):
        calls.append(kwargs)
        return answer

    monkeypatch.setattr(verified_cleanup, "delete_files", delete_files)
    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_: {"host": "t"})
    monkeypatch.setattr(restore_script, "_ssh_host_block", lambda login: {"host": "t", "access": "ssh"})
    job = SimpleNamespace(target_backup_dir="/stage/", target_server_id="TGT")
    return job, calls, answer


def test_each_file_goes_by_the_facts_its_decryption_proved_and_only_under_the_staging(cleanup):
    job, calls, _answer = cleanup

    result = restore_script.delete_decrypted_staging(job, target=object(), files=PLAIN)

    assert calls == [{"host": {"host": "t", "access": "ssh"}, "paths": [one["path"] for one in PLAIN],
                      "facts": {PLAIN[0]["path"]: {"size_bytes": 100, "sha256": "a" * 64},
                                PLAIN[1]["path"]: {"size_bytes": 30, "sha256": "b" * 64}},
                      "must_be_under": "/stage"}]
    assert result == {"plain_files": 2, "deleted": 2, "already_gone": 0, "bytes_deleted": 130}


def test_a_file_with_no_recorded_facts_is_never_handed_to_the_delete(cleanup):
    job, calls, answer = cleanup
    answer["deleted"] = [PLAIN[0]["path"]]

    result = restore_script.delete_decrypted_staging(
        job, target=object(), files=[PLAIN[0], {"path": "/stage/stray.bak"}, {"path": "/stage/half.bak", "sha256": ""}])

    assert calls[0]["paths"] == [PLAIN[0]["path"]] and result["plain_files"] == 1


def test_nothing_is_asked_of_the_host_when_there_is_nothing_to_delete(cleanup):
    job, calls, _answer = cleanup

    assert restore_script.delete_decrypted_staging(job, target=object(), files=[])["deleted"] == 0
    assert calls == []


def test_what_stayed_in_the_clear_is_named(cleanup):
    job, _calls, answer = cleanup
    answer["deleted"] = [PLAIN[0]["path"]]
    answer["not_deleted"] = [{"path": PLAIN[1]["path"], "status": "mismatch", "reason": "sha256 differs"}]

    result = restore_script.delete_decrypted_staging(job, target=object(), files=PLAIN)

    assert result["deleted"] == 1 and result["bytes_deleted"] == 100
    assert result["error"] == ("1 decrypted file(s) could not be deleted and are still in the clear on TGT - "
                               "/stage/APP/LOG/APP_LOG_07.trn: mismatch - sha256 differs")


def test_the_runs_own_rows_say_the_staged_copy_is_gone(cleanup):
    job, _calls, answer = cleanup
    answer["deleted"] = [PLAIN[0]["path"]]
    marked = []
    catalog = SimpleNamespace(
        run_files=lambda run_id: [{"run_file_id": 11, "target_host_path": PLAIN[0]["path"]},
                                  {"run_file_id": 12, "target_path": PLAIN[1]["path"], "target_host_path": None}],
        set_run_file=lambda run_file_id, **values: marked.append((run_file_id, values["target_on_disk"])))

    restore_script.delete_decrypted_staging(job, target=object(), files=PLAIN, restore_run_id=5, catalog=catalog)

    assert marked == [(11, 0)], "only the file that was deleted"


def test_a_store_that_fails_does_not_undo_the_cleanup(cleanup):
    job, _calls, _answer = cleanup

    def broken(_run_id):
        raise OSError("the store is down")

    result = restore_script.delete_decrypted_staging(job, target=object(), files=PLAIN, restore_run_id=5,
                                                     catalog=SimpleNamespace(run_files=broken))

    assert result["deleted"] == 2 and "error" not in result
