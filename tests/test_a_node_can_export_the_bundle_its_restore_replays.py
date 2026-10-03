"""A node can export the instance bundle its own restore replays - `export-instance-bundle`.

The bundle (logins, Agent jobs ...) is written beside a backup by the node that runs that backup.
A node that restores a source whose backup another node runs never has one, and its restore
replays no login: the 0.26 soak node's 100.250 drill left 23 users orphaned (1.88). Twice the bundle
was exported by a script calling the app's functions (1.78, 1.88); this is that call as a command,
for any backup entry - active on this node or not - read-only on the source.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from db_ops.backup_restore import cli


def _job(backup_id="SRC_FULL", *, enabled=True):
    return SimpleNamespace(backup_id=backup_id, server_id="SRC-1433",
                           server_metadata=SimpleNamespace(enabled=enabled))


def _run(monkeypatch, capsys, jobs, exported=None):
    monkeypatch.setattr(cli, "load_backup_jobs", lambda _path: jobs)
    seen = {}

    def export(plan, *, server_id, bundle_dir, data_dir=None):
        seen.update(server_id=server_id, bundle_dir=str(bundle_dir))
        return exported

    monkeypatch.setattr("db_ops.backup_restore.server_metadata.export_for_backup", export)
    code = cli._export_instance_bundle("config.json", "SRC_FULL")
    return code, json.loads(capsys.readouterr().out), seen


def test_the_bundle_is_exported_for_the_entry_named(monkeypatch, capsys):
    code, answer, seen = _run(monkeypatch, capsys, [_job()], exported={"ok": True, "logins": 18})

    assert code == 0 and answer["success"] is True
    assert seen["server_id"] == "SRC-1433"
    assert seen["bundle_dir"].replace("\\", "/").endswith("instance_bundles/SRC-1433")


def test_an_entry_with_server_metadata_off_is_refused_as_not_configured(monkeypatch, capsys):
    code, answer, seen = _run(monkeypatch, capsys, [_job(enabled=False)])

    assert code != 0 and answer["error_kind"] == "not_configured"
    assert seen == {}, "nothing is exported for an entry that never asked"


def test_an_unknown_backup_id_is_the_request_s_mistake(monkeypatch, capsys):
    code, answer, _seen = _run(monkeypatch, capsys, [_job("OTHER")])

    assert code != 0 and answer["error_kind"] == "request"


def test_a_failed_export_is_a_failed_answer(monkeypatch, capsys):
    code, answer, _seen = _run(monkeypatch, capsys, [_job()], exported={"ok": False, "error": "login failed"})

    assert code != 0 and "login failed" in answer["error"]


def test_the_command_is_offered():
    args = cli.parse_args(["export-instance-bundle", "--backup-id", "SRC_FULL"])

    assert args.command == "export-instance-bundle" and args.backup_id == "SRC_FULL"
