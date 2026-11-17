"""A share-driven restore says each step in the flow format, and each file as it goes in (0.39.0, 1.370).

The operator, 2026-11-13: *restore is like backup - it must message every step of its flow the same
way; restore has restore full -> diff -> log as well, a message for each* - and 2026-11-14: *one
message for each log file*. The restore from the repository got its flow messages that day; the
share-driven SQL Server path (full, diff and each log applied by ``sqlcmd`` from a share) still sent
the older shape - a fixed header and ``key=value`` lines - and said nothing between the copy and the
check, however many files went in.

Now its copy, its restore, each file, the check and the clean-up carry ``flow_title`` /
``flow_lines`` (``restore_messages``), and each file of each database is a message of its own as it
goes in: applied, passed over (a log SQL Server refused that the restore did not need, with the
reason), or failed. A step's start that is not a message of its own stays off Telegram.
"""

from __future__ import annotations

import dataclasses
import subprocess

import pytest

from conftest import patch_restore
from test_backup_restore import make_config

from dbabrain.backup_restore import restore_messages
from dbabrain.backup_restore import restore_database as restore_module
from dbabrain.lib.config import DbaBrainConfig

TOO_EARLY = ("Msg 4326, Level 16, State 1. The log in this backup set terminates at LSN 90, "
             "which is too early to apply to the database.")


def test_a_file_is_said_with_its_number_its_result_and_why_it_was_passed_over():
    """Step 7 of the eleven (1.375): each log its own message, numbered under the step, and a file passed
    over said so with why."""
    facts = {"database": "APP", "landing": "APP", "files": ["l1.trn"], "log_number": 1, "log_total": 2}
    done = restore_messages.log_step("NIGHTLY", number="7.1", facts=facts, seconds=2)
    passed = restore_messages.log_step("NIGHTLY", number="7.2", facts={**facts, "files": ["l0.trn"], "log_number": 2},
                                       result="skipped", reason="Msg 4326: ends early")

    assert done == {"flow_title": "NIGHTLY · 7.1 Restore log l1.trn done",
                    "flow_lines": ["✅ APP: l1.trn - left restoring for the next step (1 of 2 logs) · 2 s"]}
    assert passed["flow_title"] == "NIGHTLY · 7.2 Restore log l0.trn skipped"
    assert passed["flow_lines"] == ["↷ APP: l0.trn - left restoring for the next step (2 of 2 logs)",
                                    "- Msg 4326: ends early"]


def test_the_cleanup_says_what_it_looked_at_and_removed():
    said = restore_messages.plain_step("NIGHTLY", target="TGT", cleaned={
        "plain": None, "pruned": {"pruned": 3, "skipped": "9 considered, 6 kept"}, "kept": 0})
    assert said["flow_title"] == "NIGHTLY · 10. Plain files cleared"
    assert said["flow_lines"] == ["- nothing was decrypted or staged plain for this restore",
                                  "- staged backups past retention: 3 removed (9 considered, 6 kept)"]


@pytest.fixture
def restore(tmp_path, monkeypatch):
    def run(*, log_answers=(), full_fails=False):
        config = dataclasses.replace(make_config(tmp_path), source_id="SRC", target_id="TGT", restore_id="NIGHTLY",
                                     restore_sql_instance_on_vm="localhost", checkdb=False)
        folder = config.vm_import_unc / "APPDB_Prod"
        backup = folder / "FULL" / "latest.bak"
        backup.parent.mkdir(parents=True)
        backup.write_text("backup", encoding="utf-8")
        logs = [folder / "LOG" / f"l{n}.trn" for n in range(1, len(log_answers) + 1)]
        for log in logs:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("log", encoding="utf-8")
        app_config = DbaBrainConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                                    sqlite_path=tmp_path / "runtime" / "dbabrain.sqlite")
        patch_restore(monkeypatch, "run_sqlcmd_query_command",
                      lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0, "complete", ""))
        patch_restore(monkeypatch, "log_event", lambda *_a, **_k: None)
        # Whether TGT may be restored onto is another file's (the production-target rule); here it is.
        patch_restore(monkeypatch, "assert_target_is_allowed", lambda **_k: None)
        patch_restore(monkeypatch, "find_restore_log_backups", lambda *_a, **_k: list(logs))
        if full_fails:
            def full(**_k):
                raise RuntimeError("Msg 3201: cannot open backup device")

            patch_restore(monkeypatch, "run_restore_full", full)
        replies = iter(log_answers)

        def log_step(**kwargs):
            if kwargs.get("step_name") != "restore-log":
                return {"step": kwargs.get("step_name"), "status": "SUCCESS", "stdout": "", "stderr": ""}
            reply = next(replies)
            if reply != "ok":
                raise RuntimeError(reply)
            return {"step": "restore-log", "status": "SUCCESS", "stdout": "", "stderr": ""}

        patch_restore(monkeypatch, "_run_restore_step", log_step)
        said: list[tuple[str, str, dict]] = []
        # The workflow calls each step of the database and says it (0.40.0, 1.375).
        from dbabrain.backup_restore import workflow

        monkeypatch.setattr(workflow, "emit_backup_restore_event",
                            lambda **k: said.append((k["phase"], k["message"], k["metadata"])))
        say = workflow._RestoreSay(app_config=None, logger=None, notify=None, metadata={}, restore_id="NIGHTLY",
                                   engine="sqlserver", target="TGT", source="SRC")
        say.remote = True  # a share-driven restore always copies onto its target
        try:
            workflow.restore_share_database(config=config, app_config=app_config, backup_file=backup,
                                            logger=object(), say=say)
        except Exception as exc:  # noqa: BLE001 - what was said up to the failure is the point
            said.append(("RAISED", f"{type(exc).__name__}: {exc}", {}))
        return said

    return run


def test_each_file_of_a_database_is_a_message_of_its_own_as_it_goes_in(restore):
    said = restore(log_answers=["ok", "ok"])

    # The eleven steps of 1.375: the full a start and a done (5.1), no diff said with why (6), each log its
    # own message under 7.
    assert [phase for phase, _m, _e in said] == ["FULL_START", "FULL_DONE", "STEP_SKIP", "LOG_DONE", "LOG_DONE"]
    titles = [extra["flow_title"].split(" · ", 1)[-1] for _p, _m, extra in said]
    assert titles == ["5.1 Restore full APPDB_Prod_DR started", "5.1 Restore full APPDB_Prod_DR done",
                      "6. Restore diff: none", "7.1 Restore log l1.trn done", "7.2 Restore log l2.trn done"]
    assert said[1][2]["flow_lines"][0].startswith(
        "✅ APPDB_Prod as APPDB_Prod_DR: latest.bak - left restoring for the next step")
    assert "(2 of 2 logs)" in said[4][2]["flow_lines"][0]


def test_a_log_passed_over_is_said_with_why(restore):
    said = restore(log_answers=[TOO_EARLY, "ok"])

    skipped = [extra for phase, _m, extra in said if phase == "LOG_DONE" and "skipped" in extra["flow_title"]]
    assert len(skipped) == 1 and skipped[0]["flow_title"] == "NIGHTLY · 7.1 Restore log l1.trn skipped"
    assert skipped[0]["flow_lines"][1].startswith("- Msg 4326: the log ends before the database's restore point")


def test_a_file_that_fails_is_said_failed_with_the_engines_error(restore):
    said = restore(log_answers=["ok"], full_fails=True)

    # The full started and was said; its failure is the error's - step 11 says it, at step 5.
    assert [phase for phase, _m, _e in said] == ["FULL_START", "RAISED"]
    assert "Msg 3201" in said[-1][1]


def test_a_restore_with_no_one_listening_restores_all_the_same(tmp_path, monkeypatch, restore):
    """A caller that passes nothing - a script, a test - still restores: saying is never the restore."""
    config = dataclasses.replace(make_config(tmp_path / "x"), source_id="SRC", target_id="TGT",
                                 restore_sql_instance_on_vm="localhost", checkdb=False)
    backup = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    patch_restore(monkeypatch, "run_sqlcmd_query_command",
                  lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0, "complete", ""))
    patch_restore(monkeypatch, "log_event", lambda *_a, **_k: None)
    patch_restore(monkeypatch, "assert_target_is_allowed", lambda **_k: None)

    result = restore_module.run_restore_database(
        config=config, dbabrain_config=DbaBrainConfig(log_dir=tmp_path / "l", runtime_dir=tmp_path / "r",
                                                      sqlite_path=tmp_path / "r" / "s.sqlite"),
        backup_file=backup, ensure_certificate=False, ensure_credential=False, logger=object())

    assert result["status"] == "SUCCESS"
