"""A task that runs its SQL once per batch must still deliver one table, in one place.

A load that fetches ten days of scans and writes them 2,000 rows at a time runs its SQL once per
batch, and each run returns a one-row summary: 69 result sets for one run. With `plain` output they
were rendered as 69 separate tables, each with its own header. Switching to a file format did not help either: the file held only the first result set,
so a `txt` export carried one batch of sixty-nine.

The messages were not only the table. With `logging_on_run` on, the runner reported progress per
step, and every batch was a step: 70 messages for one run.

The rules these tests hold: consecutive result sets with the same columns are one table, both in
the message and in the file; a file never silently drops a set; and a batch is not a file, so it
does not get a progress message of its own.
"""

from __future__ import annotations

import json

import pytest

from db_ops.lib.task_output import TaskOutputError, merge_result_sets, parse_output
from db_ops.sql_tasks import runner

from test_sql_task_claims import sql_command, sql_target

from conftest import patch_sql_runner

COLUMNS = ["batch_rows", "inserted", "window_from"]


def _batched_result(batches: int = 69) -> dict:
    """One file per batch, each with a one-row result set, then a final step with none."""
    files = [{"result_sets": [{"columns": COLUMNS, "rows": [[2000, n, "2026-09-10"]]}]}
             for n in range(batches)]
    files.append({"batch": "[final]", "result_sets": []})
    return {"row_count": batches, "files": files}


def test_consecutive_sets_with_the_same_columns_become_one():
    merged = merge_result_sets([
        {"columns": ["a"], "rows": [[1]]},
        {"columns": ["a"], "rows": [[2]]},
        {"columns": [], "rows": []},
        {"columns": ["b"], "rows": [[3]]},
        {"columns": ["a"], "rows": [[4]]},
    ])

    assert merged == [
        {"columns": ["a"], "rows": [[1], [2]]},
        {"columns": ["b"], "rows": [[3]]},
        {"columns": ["a"], "rows": [[4]]},
    ]


def test_sixty_nine_batches_are_one_table_in_the_message():
    text = runner.format_result_sets_markdown(_batched_result())

    assert text.count("result set ") == 1
    assert text.startswith("result set 1: 69 row(s)")


def test_a_txt_export_carries_every_batch_not_the_first(tmp_path):
    path = runner.write_sql_task_output(
        command=sql_command(), target=sql_target(output_format="txt"),
        result=_batched_result(), sql_run_id=1, output_dir=tmp_path)

    body = path.read_text(encoding="utf-8")
    assert body.count("2026-09-10") == 69
    assert "69 row(s)" in body or "69 rows" in body


def test_a_txt_export_keeps_every_shape_when_the_script_returns_two(tmp_path):
    result = _batched_result(3)
    result["files"].append({"result_sets": [{"columns": ["synced"], "rows": [[42]]}]})

    path = runner.write_sql_task_output(
        command=sql_command(), target=sql_target(output_format="txt"),
        result=result, sql_run_id=1, output_dir=tmp_path)

    body = path.read_text(encoding="utf-8")
    assert "window_from" in body and "synced" in body
    assert runner.unexported_result_sets(result, "txt") == 0


def test_a_json_export_of_two_shapes_is_a_list_of_both(tmp_path):
    result = _batched_result(2)
    result["files"].append({"result_sets": [{"columns": ["synced"], "rows": [[42]]}]})

    path = runner.write_sql_task_output(
        command=sql_command(), target=sql_target(output_format="json"),
        result=result, sql_run_id=1, output_dir=tmp_path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [item["columns"] for item in payload] == [COLUMNS, ["synced"]]


def test_a_one_table_format_says_how_many_sets_it_left_out():
    result = _batched_result(2)
    result["files"].append({"result_sets": [{"columns": ["synced"], "rows": [[42]]}]})

    assert runner.unexported_result_sets(result, "xlsx") == 1
    assert runner.unexported_result_sets(_batched_result(), "xlsx") == 0


def test_the_output_block_is_parsed_in_one_place_with_one_answer():
    assert parse_output({"format": "TXT", "telegram_chat": "sql"}) == {
        "format": "txt", "telegram_chat": "sql", "chat_id": "", "max_rows": 0}
    assert parse_output({})["format"] == "none"
    for bad in ({"format": "pdf"}, {"format": "plain", "max_rows": 0},
                {"format": "plain", "max_rows": 20000}, "txt"):
        with pytest.raises(TaskOutputError):
            parse_output(bad)


def _run_batched_task(tmp_path, monkeypatch, *, progress_per_file=None, batches=5):
    """Run a Python-fed task of ``batches`` batches and return the status of every message sent."""
    import dataclasses

    from test_sql_task_claims import RecordingSqlRunStore, inventory_and_credentials

    from db_ops.sql_tasks.python_source import PythonSource

    data_dir = tmp_path / "data"
    (data_dir / "sql").mkdir(parents=True)
    (data_dir / "sql" / "load.sql").write_text("SELECT 1;", encoding="utf-8")
    command = dataclasses.replace(
        sql_command(script_files=("sql/load.sql",)),
        python_source=PythonSource(script_path="fetch.py"), progress_per_file=progress_per_file)
    patch_sql_runner(monkeypatch, "_run_python_source",
                        lambda **_: [json.dumps([{"n": n}]) for n in range(batches)])
    patch_sql_runner(monkeypatch, "execute_sql", lambda **_: {"row_count": 1, "result_sets": []})
    patch_sql_runner(monkeypatch, "log_event", lambda *args, **kwargs: None)
    sent = []
    patch_sql_runner(monkeypatch, "enqueue_sql_task_message",
                        lambda **kwargs: sent.append(kwargs["status"]))
    inventory, credentials = inventory_and_credentials()

    assert runner.run_one_sql_task(
        store=RecordingSqlRunStore(), data_dir=data_dir, telegram_groups={}, command=command,
        target=sql_target(), inventory=inventory, credentials=credentials, secrets={},
        logger=None) is True
    return sent


def test_a_batched_task_does_not_send_a_message_per_batch(tmp_path, monkeypatch):
    sent = _run_batched_task(tmp_path, monkeypatch)

    assert "running" not in sent[1:], sent
    assert len(sent) <= 3, sent


def test_per_batch_progress_is_still_there_for_a_command_that_asks_for_it(tmp_path, monkeypatch):
    sent = _run_batched_task(tmp_path, monkeypatch, progress_per_file=True)

    assert sent.count("running") >= 5, sent
