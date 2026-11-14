"""A restore's dry run says what it would do on a target nothing was restored to (0.39.0, 1.356).

Read on 2026-11-13, mapping the restore half of the new flow: onto another machine, `restore-by-id`
with `dry_run` skipped the copy and planned from what was already staged - on a fresh target an empty
folder, so it failed; `restore-workflow --dry-run` answered `dry-run` and nothing else; and a chat
had no way to ask. So the operator could not see, before a restore from the repository, what it would
copy, from where, whether it fits, or what would stop it.

The dry run now reads the chain where the copy would read it (`prepare-staging` `plan_only`, no
`chmod` at the source), names each file with the bytes it will have on the target - a `.age` by the
plain size the store recorded - and asks the target, read only, whether the staging folder is usable
and the files fit (`prepare-staging` with `incoming` and `dry_run`). `/spbot_restore_plan` asks it from
a chat and reads back one sentence.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_a_removed_backup_set_leaves_no_husk import _Session, _Sftp

from dbabrain.backup_restore import cli as backup_cli
from dbabrain.backup_restore import workflow
from dbabrain.common import backup_copy, cli_backup_copy
from dbabrain.lib import restore_space
from dbabrain.telegram.command_cli import build_cli_argv

REPO_ROOT = Path(__file__).resolve().parents[1]
INCOMING = [("APP/FULL/APP_FULL_1.bak", 5000), ("APP/LOG/APP_LOG_2.trn", 300)]
RULE = restore_space.parse_space_check({"space_check": {"enabled": True, "factor": 2.0}})


def _measure(target, monkeypatch, free=None):
    if free is not None:
        monkeypatch.setattr(backup_copy, "_target_free_bytes", lambda session, path: free)
    return backup_copy.measure_target(target_session=_Session(target), target_dir="/stage", incoming=INCOMING,
                                      space_check=RULE if free is not None else None)


# --------------------------------------------------------------------------- #
# The target, read only
# --------------------------------------------------------------------------- #
def test_a_target_never_restored_to_is_said_to_be_made_by_the_restore_and_nothing_is_written(monkeypatch):
    target = _Sftp({}, {"/"})
    answer = _measure(target, monkeypatch, free=20000)
    assert answer["staging"].startswith("not there yet") and not answer["refused"]
    assert answer["pending_bytes"] == 5300 and answer["space_check"]["ok"] is True
    assert target.files == {} and target.dirs == {"/"}, "nothing made, nothing written"


@pytest.mark.parametrize("files, words, refused", [
    ({"/stage/" + backup_copy.STAGING_MARKER: 1}, "marked", False),
    ({}, "there and empty", False),
    ({"/stage/APP/FULL/APP_FULL_1.bak": 5000}, "earlier staging of these files", False),
    ({"/stage/payroll.xlsx": 10}, "REFUSED", True),
])
def test_the_staging_folder_is_judged_as_the_restore_would(files, words, refused, monkeypatch):
    dirs = {"/", "/stage"} | {p.rsplit("/", 1)[0] for p in files} | ({"/stage/APP"} if any("APP" in p for p in files) else set())
    answer = _measure(_Sftp(files, dirs), monkeypatch)
    assert words in answer["staging"] and answer["refused"] is refused


def test_files_that_would_not_fit_are_a_verdict_not_an_exception(monkeypatch):
    answer = _measure(_Sftp({}, {"/"}), monkeypatch, free=6000)
    assert answer["space_check"]["ok"] is False and "will not fit" in answer["space_check"]["detail"]


def test_a_dry_run_is_asked_with_incoming_only(monkeypatch):
    monkeypatch.setattr(cli_backup_copy, "_open", lambda *a, **k: pytest.fail("no host may be reached"))
    with pytest.raises(ValueError, match="dry_run goes with incoming"):
        cli_backup_copy._prepare_staging({"source_dir": "/r", "dry_run": True})


# --------------------------------------------------------------------------- #
# The plan of the copy: read where the copy reads, plain bytes for an encrypted file
# --------------------------------------------------------------------------- #
@pytest.fixture
def planned(monkeypatch, tmp_path):
    from dbabrain.backup_restore import restore_script as R
    from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore

    store = RepositoryEncryptionStore(tmp_path / "store.sqlite")
    calls = []
    listed = ["APP/FULL/APP_FULL_1.bak.age", "APP/LOG/APP_LOG_2.trn.age"]

    def run(command, request, **_k):
        calls.append((command, request))
        if command == "prepare-staging" and request.get("plan_only"):
            return {"files": [{"path": p, "size_bytes": 99} for p in listed], "include": ["APP/"]}
        if command == "prepare-staging" and request.get("dry_run"):
            return {"staging": "not there yet - made and marked by the restore", "refused": False,
                    "skipped": 0, "pending_bytes": sum(i["size_bytes"] for i in request["incoming"]),
                    "space_check": {"checked": True, "ok": True}}
        raise AssertionError(command)

    monkeypatch.setattr(R.common_cli, "run", run)
    monkeypatch.setattr(R, "_ssh_login", lambda target, **_k: {"host": target.server_id})
    monkeypatch.setattr(R, "_repository_source", lambda job, **_k: (
        SimpleNamespace(server_id="REPO", container_name=""), "/mnt/nas/SRC", {}))
    monkeypatch.setattr("dbabrain.lib.config.load_config", lambda *a, **k: None)
    monkeypatch.setattr(RepositoryEncryptionStore, "from_config", classmethod(lambda cls, _c: store))
    from dbabrain.lib import vault_file as V

    identity, recipient = V.new_identity()
    key = store.add_key(server_id="SRC", identity=identity, recipient=recipient,
                        created_at="2026-01-01T00:00:00Z", days=90, passphrase="pw", created_by="t")
    for name, plain in (("APP/FULL/APP_FULL_1.bak", 5000), ("APP/LOG/APP_LOG_2.trn", 300)):
        row = store.record_encrypted(file_id=1, backup_id="B", server_id="SRC", repository_server_id="REPO",
                                     plain_path=f"/mnt/nas/SRC/{name}", plain_size_bytes=plain, plain_sha256="a" * 64,
                                     encrypted_path=f"/mnt/nas/SRC/{name}.age", encrypted_size_bytes=99,
                                     encrypted_sha256="b" * 64, key_id=key["key_id"], age_version="1.2.1",
                                     encrypted_at="2026-01-02T00:00:00Z")
        store.mark_proven(row["encrypted_file_id"])
    job = SimpleNamespace(label="APP_TO_LAB", restore_from="repository", source_backup_host_dir="D:/SQLBK",
                          copy_selection="chain", copy_recent_hours=0, db_type="sqlserver", backup_dir="D:/SQLBK",
                          target_server_id="LAB", target_backup_dir="/stage", space_check=RULE, staging_marker="")
    return SimpleNamespace(R=R, calls=calls, job=job, listed=listed, store=store)


def _plan(p):
    return p.R.plan_transfer(p.job, source=SimpleNamespace(server_id="SRC", container_name=""),
                             target=SimpleNamespace(server_id="LAB"), point_in_time="")


def test_the_chain_is_read_on_the_repository_without_touching_it(planned):
    plan = _plan(planned)
    read = planned.calls[0][1]
    assert read["plan_only"] is True and read["make_readable"] is False, "no chmod at the source in a dry run"
    assert read["source_dir"] == "/mnt/nas/SRC" and read["chain"]["db_type"] == "sqlserver"
    assert plan["reads_from"] == "the repository" and plan["source_server_id"] == "REPO"


def test_each_encrypted_file_is_counted_by_its_plain_bytes_and_the_target_asked_read_only(planned):
    plan = _plan(planned)
    assert plan["files"] == [{"path": "APP/FULL/APP_FULL_1.bak", "size_bytes": 5000},
                             {"path": "APP/LOG/APP_LOG_2.trn", "size_bytes": 300}]
    assert plan["bytes"] == 5300 and plan["encrypted"] is True
    assert plan["decrypted_through"] == "this node, streamed to the target"
    asked = planned.calls[1][1]
    assert asked["dry_run"] is True and asked["target_dir"] == "/stage" and len(asked["incoming"]) == 2
    assert plan["would_refuse"] == [] and plan["space_check"]["ok"] is True


def test_a_file_no_offload_proved_is_named_as_what_the_restore_would_refuse(planned):
    planned.listed.append("APP/LOG/stray.trn.age")
    plan = _plan(planned)
    assert plan["would_refuse"] and "stray.trn.age" in plan["would_refuse"][0]


# --------------------------------------------------------------------------- #
# The workflow and the chat
# --------------------------------------------------------------------------- #
def test_the_workflows_dry_run_is_the_restores_plan_in_one_sentence(monkeypatch):
    plan = {"restore_id": "APP_TO_LAB", "db_type": "sqlserver", "dry_run": True, "would_refuse": [],
            "transfer_plan": {"file_count": 2, "bytes": 2 * 1024 ** 3, "reads_from": "the repository",
                              "source_server_id": "REPO", "target_server_id": "LAB", "target_dir": "/stage",
                              "encrypted": True, "decrypted_through": "this node, streamed to the target",
                              "staging": "not there yet - made and marked by the restore",
                              "space_check": {"checked": True, "ok": True}},
            "steps": ["restore-full per database - every file moved", "verify-restore - DBCC CHECKDB"]}
    monkeypatch.setattr("dbabrain.backup_restore.restore_by_id.restore_by_id", lambda request, **_k: plan)
    monkeypatch.setattr("dbabrain.backup_restore.cli._open_catalog", lambda *a: None)
    monkeypatch.setattr(workflow, "_entry_action", lambda job, copy_only: ("restore", None))
    answer = workflow._dry_run_plan(SimpleNamespace(restore_id="APP_TO_LAB", db_type="sqlserver"), copy_only=False,
                                    point_in_time=None, key=None, key_base64=None, app_config=None, logger=None)
    assert answer["said"] == ("would run: 2 file(s), 2.00 GB from the repository (REPO) to LAB:/stage, decrypted "
                              "through this node, streamed to the target; room: fits; staging: not there yet - made "
                              "and marked by the restore | then restore-full per database, verify-restore - nothing changed")


def test_a_refused_entry_is_said_refused_and_its_restore_is_not_asked(monkeypatch):
    monkeypatch.setattr(workflow, "_entry_action", lambda job, copy_only: (None, ValueError("an in-place entry")))
    monkeypatch.setattr("dbabrain.backup_restore.restore_by_id.restore_by_id",
                        lambda *a, **k: pytest.fail("a refused entry is not planned"))
    answer = workflow._dry_run_plan(SimpleNamespace(restore_id="X", db_type="sqlserver"), copy_only=False,
                                    point_in_time=None, key=None, key_base64=None, app_config=None, logger=None)
    assert answer["said"] == "refused - an in-place entry" and answer["would_refuse"] == ["an in-place entry"]


def _plan_command(copy="dbabrain/telegram/catalogue/telegram_support_commands.json"):
    records = json.loads((REPO_ROOT / copy).read_text(encoding="utf-8-sig"))["telegram_support_commands"]
    return {r["command_text"]: r for r in records}


@pytest.mark.parametrize("copy", ["dbabrain/telegram/catalogue/telegram_support_commands.json",
                                  "data/telegram_support_commands.example.json"])
def test_the_chat_command_asks_the_dry_run_and_the_cli_takes_its_line(copy):
    commands = _plan_command(copy)
    plan, restore = commands["spbot_restore_plan"], commands["spbot_restore"]
    config = plan["action_config"]
    argv = build_cli_argv(config, {"python": "python", "config_path": "config.json", "restore_id": "APP_TO_LAB",
                                   "point_in_time": "2026-11-12 21:00:00 +07:00"})
    args = backup_cli.parse_args(argv[3:])
    assert args.command == "restore-workflow" and args.dry_run is True and args.restore_id == "APP_TO_LAB"
    assert args.point_in_time == "2026-11-12 21:00:00 +07:00"
    assert "completion_probe" not in config, "a dry run writes no run row to wait for"
    assert plan["command_type"] < restore["command_type"], "reading is offered below the level that restores"
    assert plan["action_config"]["parameters"][1]["pattern"] == restore["action_config"]["parameters"][1]["pattern"]
    assert "{said}" in config["success_text"]


def test_the_dry_run_command_is_never_the_one_a_restore_is_held_to():
    """Found the day it was added: it runs `restore-workflow` too, sat first in the file, and the
    assistant's runbook took it for the restore - its level 2 would have handed the restore to level 2."""
    from dbabrain.lib import backup_runbook as R

    document = json.loads((REPO_ROOT / "dbabrain/telegram/catalogue/telegram_support_commands.json")
                          .read_text(encoding="utf-8"))
    twins = R.bot_twins(document)
    assert twins[R.GATE_RESTORE]["command"] == "/spbot_restore" and twins[R.GATE_RESTORE]["level"] == 10
    assert twins[R.RESTORE_PLAN]["command"] == "/spbot_restore_plan"
    records = document["telegram_support_commands"]
    records.sort(key=lambda r: r["command_text"] != "spbot_restore_plan")  # first, whatever the file says
    assert R.bot_twins(document)[R.GATE_RESTORE]["command"] == "/spbot_restore"
