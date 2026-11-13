"""A run started by hand goes by its entry's own configuration, and says what it is.

Two things read from the code on 2026-11-11, while the assistant's runbook was written (it hands
out exactly these lines, so what they do had to be known), and fixed on 2026-11-12 (1.340, 1.341):

* ``backup_restore offload`` had ``--limit`` with a default of 100, and the default was passed as
  the pass's override. So every run started from a terminal replaced each entry's
  ``offload_files_per_pass`` with 100 - the same entry took 5 files a pass from the scheduler and
  100 from a hand. The value an entry runs by is the entry's; a flag overrides it only when given.
* ``backup_restore restore-by-id`` with ``"dry_run": true`` announced *Restore X started.* and
  *Restore X finished: done.* to the entry's group. Whoever read that in Telegram read a database
  restored; nothing had been touched. A dry run is still bracketed - whoever starts a run reports
  it - but in its own words.
"""

from __future__ import annotations

import json

import pytest

from dbabrain.backup_restore import cli
from dbabrain.backup_restore import offload as offload_module
from dbabrain.backup_restore import restore_by_id as restore_by_id_module
from dbabrain.lib.config import DbaBrainConfig


@pytest.fixture
def node(tmp_path, monkeypatch):
    """An empty tool root, with what `main` does around a command stubbed: no log, no store."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _path: DbaBrainConfig(
        log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "dbabrain.sqlite"))
    monkeypatch.setattr(cli, "patch_stdout", lambda *a, **k: None)
    monkeypatch.setattr(cli, "setup_app_logger", lambda *a, **k: None)
    emitted: list[dict] = []
    monkeypatch.setattr(cli, "emit_backup_restore_event", lambda **k: emitted.append(k))
    return emitted


@pytest.fixture
def offload_calls(monkeypatch):
    calls: list[dict] = []

    def run_offloads(**kwargs):
        calls.append(kwargs)
        return {"entries": 0, "ran": 0, "succeeded": 0, "failed": 0, "jobs": []}

    monkeypatch.setattr(offload_module, "run_offloads", run_offloads)
    return calls


def test_an_offload_started_by_hand_takes_each_entrys_own_files_per_pass(node, offload_calls, capsys):
    assert cli.main(["offload", "--config", "config.json"]) == 0

    assert offload_calls[0]["limit"] is None, "no override: the pass reads offload_files_per_pass"
    capsys.readouterr()


def test_a_limit_given_on_the_command_line_is_that_runs_override(node, offload_calls, capsys):
    assert cli.main(["offload", "--config", "config.json", "--limit", "7"]) == 0

    assert offload_calls[0]["limit"] == 7
    capsys.readouterr()


@pytest.mark.parametrize("stated", ["0", "-3", "many"])
def test_a_limit_that_is_not_a_count_of_files_is_refused_before_anything_runs(node, offload_calls, capsys, stated):
    """Zero is not "no limit": a pass of no files would end done having copied nothing."""
    with pytest.raises(SystemExit) as refused:
        cli.main(["offload", "--config", "config.json", "--limit", stated])

    assert refused.value.code != 0
    assert offload_calls == []
    assert "--limit" in capsys.readouterr().err


def test_the_pass_reads_the_entrys_value_when_no_limit_is_given():
    """The half the command line relies on: `run_offloads` falls to the entry's own number."""
    import inspect

    source = inspect.getsource(offload_module.run_offloads)

    assert "int(limit or entry.offload.files_per_pass)" in source


def _restore_request(**fields) -> str:
    return json.dumps({"restore_id": "PG_DRILL", **fields})


@pytest.fixture
def restore_calls(monkeypatch):
    calls: list[dict] = []

    def restore_by_id(request, **kwargs):
        calls.append({"request": request, **kwargs})
        return {"restore_id": request["restore_id"], "db_type": "postgresql", "dry_run": bool(request.get("dry_run")),
                "warnings": []}

    monkeypatch.setattr(restore_by_id_module, "restore_by_id", restore_by_id)
    monkeypatch.setattr(cli, "_open_catalog", lambda *a, **k: None)
    return calls


def test_a_dry_run_of_a_restore_is_announced_as_a_dry_run(node, restore_calls, capsys):
    assert cli.main(["restore-by-id", "--config", "config.json", _restore_request(dry_run=True)]) == 0

    start, end = node
    assert (start["phase"], end["phase"]) == ("START", "END")
    assert start["message"] == "Restore PG_DRILL dry run started. Nothing will be restored."
    assert end["message"] == "Restore PG_DRILL dry run finished: the plan was read, nothing restored."
    assert "done" not in end["message"], "the word a real restore ends on"
    assert start["metadata"]["dry_run"] is True and end["metadata"]["dry_run"] is True
    answer = json.loads(capsys.readouterr().out)
    assert answer["success"] is True
    assert answer["message"] == "PG_DRILL dry run: the plan was read, nothing restored (postgresql)."


def test_a_real_restore_keeps_its_words(node, restore_calls, capsys):
    assert cli.main(["restore-by-id", "--config", "config.json", _restore_request()]) == 0

    start, end = node
    assert start["message"] == "Restore PG_DRILL started."
    assert end["message"] == "Restore PG_DRILL finished: done."
    assert "dry_run" not in start["metadata"]
    assert json.loads(capsys.readouterr().out)["message"] == "PG_DRILL restored (postgresql)."


def test_a_dry_run_that_fails_says_it_was_a_dry_run(node, monkeypatch, capsys):
    def refuses(request, **kwargs):
        raise RuntimeError("the chain has no full backup")

    monkeypatch.setattr(restore_by_id_module, "restore_by_id", refuses)

    assert cli.main(["restore-by-id", "--config", "config.json", _restore_request(dry_run=True)]) != 0

    error = node[-1]
    assert error["phase"] == "ERROR"
    assert error["message"] == "Restore PG_DRILL dry run FAILED."
    assert "no full backup" in error["error_text"]
    capsys.readouterr()


def test_a_request_that_does_not_parse_is_not_taken_for_a_dry_run():
    assert cli._request_is_dry_run("{not json") is False
    assert cli._request_is_dry_run('["dry_run"]') is False
    assert cli._request_is_dry_run('{"dry_run": false}') is False
    assert cli._request_is_dry_run('{"dry_run": true}') is True
