"""`deploy --type ...` must not build an image or replace the container.

That is the entire reason the flag exists. A config edit and a new `.sql` reach the worker's
bind mounts and are read by the next scheduled run — the scheduler re-loads `app_commands.json`
on every scan and every app command is a fresh process — so the build, the 700 MB upload and the
container replacement are pure cost for that change.

The risk in adding a fast path is that it quietly stops being fast, or quietly starts doing more
than it says: a stray `build_image` call inside the push would still *work*, and nobody would
notice for months except by the clock. So the three expensive steps are held down here by making
them fail if they are called at all.

The other half is the direction. A push is master -> worker for the files it names; it must never
reach for `--merge`, which pulls the worker's config back and rebuilds everything from it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.control import cli, deploy as deploy_ops

MANIFEST = {
    "data_files": [
        {"file": "sql_targets.json", "app_code": "SQL", "kind": "config", "transfer": "merge"},
    ]
}


@pytest.fixture()
def master(tmp_path: Path, monkeypatch) -> Path:
    """A master tree with one config file and one task script, and no way to reach a worker."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "data_files.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (data / "sql_targets.json").write_text('{"sql_targets": []}', encoding="utf-8")
    (tmp_path / "assets" / "tasks" / "oracle").mkdir(parents=True)
    (tmp_path / "assets" / "tasks" / "oracle" / "019_get_job_details.sql").write_text(
        "select 1 from dual", encoding="utf-8")
    monkeypatch.setattr(cli, "DB_OPS_ROOT", tmp_path)

    def refuse(name):
        def _refuse(*_args, **_kwargs):
            raise AssertionError(f"a push must not call {name}()")
        return _refuse

    for step in ("build_image", "copy_bundle", "start_daemon", "reclaim_worker_files"):
        monkeypatch.setattr(deploy_ops, step, refuse(step))
    return tmp_path


def test_a_dry_run_push_builds_nothing_and_connects_to_nothing(master, capsys) -> None:
    assert cli.main(["deploy", "--type", "assets/tasks", "--dry-run", "--user", "u"]) == 0
    printed = capsys.readouterr().out
    assert "019_get_job_details.sql" in printed
    assert "nothing was uploaded" in printed


def test_a_push_uploads_the_named_files_and_returns_the_count(master, monkeypatch) -> None:
    """The transport is one SFTP batch; what matters is that it is handed exactly the selection."""
    sent: list[tuple[str, str]] = []

    def fake_put(_client, pairs, *, on_file=None):
        sent.extend((str(local), remote) for local, remote in pairs)

    monkeypatch.setattr(deploy_ops, "sftp_put_files", fake_put)
    monkeypatch.setattr(deploy_ops, "ssh_connect", lambda *_a, **_k: _NoopClient())
    monkeypatch.setattr(deploy_ops, "reclaim_worker_files", lambda **_k: None)

    from db_ops.lib.deploy_selection import select_push_files

    selected = select_push_files(tool_root=master, push_type="config",
                                 names=["sql_targets.json"], data_dir=master / "data")
    count = deploy_ops.push_selected(host="h", user="u", password="p",
                                     remote_dir="/opt/db_ops", files=selected)

    assert count == 1
    assert [remote for _local, remote in sent] == ["/opt/db_ops/data/sql_targets.json"]


def test_a_config_push_is_gated_on_the_files_it_ships(master, monkeypatch, capsys) -> None:
    """The store/data gate still runs on a push - scoped to the file being pushed."""
    asked: list[dict] = []
    monkeypatch.setattr(cli, "_gate_config_drift",
                        lambda args, **kwargs: asked.append(kwargs))

    assert cli.main(["deploy", "--type", "config", "--file_name", "sql_targets.json",
                     "--dry-run", "--user", "u"]) == 0
    assert asked == [{"files": ["sql_targets.json"], "report_only": True}]
    assert "data/sql_targets.json" in capsys.readouterr().out


def test_merge_and_type_are_refused_together(master, capsys) -> None:
    """--merge is a whole-estate operation in the other direction; a push cannot honour both."""
    assert cli.main(["deploy", "--type", "assets/tasks", "--merge", "--dry-run",
                     "--user", "u"]) == 2
    assert "different operations" in capsys.readouterr().err


def test_an_assets_push_says_the_config_gate_does_not_apply(master, capsys) -> None:
    """No `data/*.json` is shipped, so there is nothing for the store to disagree with.

    Said out loud rather than passed over in silence: the gate is what stops a deploy reverting a
    console edit, and an operator who has learned to expect it should be told why it is absent.
    """
    cli.main(["deploy", "--type", "assets/tasks", "--dry-run", "--user", "u"])
    assert "drift gate does not apply" in capsys.readouterr().out


class _NoopClient:
    def close(self) -> None:
        pass


def test_dry_run_without_a_type_is_refused_rather_than_ignored(master, capsys) -> None:
    """"Let me see the plan first" must never turn into a build, an upload and a restart."""
    assert cli.main(["deploy", "--dry-run", "--user", "u"]) == 2
    assert "applies to a push" in capsys.readouterr().err
