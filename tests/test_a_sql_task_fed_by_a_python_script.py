"""Some rows do not start in a database, and the task that loads them should still be a SQL task.

An HTTP API, a vendor export, a device that speaks only its own protocol. Until 2026-09-14 the
answer was a script outside db_ops that opened its own connection, held its own copy of the
credential and was scheduled by something else — the shape this tool exists to replace. Asked for
on 2026-09-14: *"sql task gọi python code chạy, rồi python code lại return json về, rồi sql lấy
json đó push vào sql"*.

So a task gained a second axis. `script_type` goes on saying what the SQL half is — single, array
or folder, unchanged — and `input_type` says where the rows come from. The two were briefly one
field (`script_type: "python"`) and that was wrong within the hour: it made every combination of
the two a new word, and the first thing it forced was a python task pretending to be an `array`, a
spelling right about the files and silent about the part that matters.

What is held down here is mostly the refusals. A fetcher that half-works is the failure this
feature invites — it prints something, exits non-zero, and a load that took half the rows looks
exactly like one that took all of them.
"""

from __future__ import annotations

import json

import pytest

from db_ops.sql_tasks import python_source as ps
from db_ops.sql_tasks.runner import (INPUT_TYPES, build_execution_plan, load_input_definition,
                                     load_sql_script_definition)

FETCHER = (
    "import json, sys\n"
    "print('fetching page 1', file=sys.stderr)\n"
    "rows = [{'code': f'E{i:03d}', 'name': 'Nguyễn Văn A'} for i in range(int(sys.argv[1]))]\n"
    "print(json.dumps({'status': 'SUCCESS', 'total': len(rows), 'data': rows},\n"
    "                 ensure_ascii=False))\n"
)


@pytest.fixture
def root(tmp_path):
    """A tool root with the two folders a python-fed task uses."""
    (tmp_path / "assets" / "tasks" / "python").mkdir(parents=True)
    (tmp_path / "assets" / "tasks" / "sqlserver").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "assets" / "tasks" / "python" / "fetch.py").write_text(FETCHER, encoding="utf-8")
    (tmp_path / "assets" / "tasks" / "sqlserver" / "load.sql").write_text("SELECT 1",
                                                                          encoding="utf-8")
    return tmp_path


def command(**over):
    item = {
        "sql_id": 99, "sql_code": "TEST-099", "script_type": "single",
        "script_path": "assets/tasks/sqlserver/load.sql",
        "input_type": "python",
        "input": {"script": "assets/tasks/python/fetch.py", "args": ["7"],
                  "rows_path": "data", "parameter": "payload", "batch_rows": 3},
        "parameters": [{"name": "payload", "type": "nvarchar(max)"}],
    }
    item.update(over)
    return item


def write(root, name, body):
    path = root / "assets" / "tasks" / "python" / name
    path.write_text(body, encoding="utf-8")
    return f"assets/tasks/python/{name}"


# ------------------------------------------------------------------ the two axes stay separate

def test_the_sql_half_is_still_described_by_script_type():
    """`input_type` must not have taken anything away from the field that was already there."""
    definition = load_sql_script_definition(command(), data_dir=None)

    assert definition["script_type"] == "single"
    assert definition["script_files"] == ("assets/tasks/sqlserver/load.sql",)
    assert "python" not in INPUT_TYPES - {"python"}, "sanity: python is one of the input types"


def test_a_python_input_composes_with_every_script_type(root):
    """The point of splitting them. A fetch feeding a folder of scripts is a sentence you can say
    in this config; with one field it would have needed a fourth word, and a fifth for arrays."""
    for script_type, extra in (("single", {"script_path": "assets/tasks/sqlserver/load.sql"}),
                               ("array", {"script_paths": ["assets/tasks/sqlserver/load.sql"]})):
        item = command(script_type=script_type, **extra)
        item.pop("script_path", None) if script_type == "array" else None
        definition = load_sql_script_definition(item, data_dir=root / "data")
        assert definition["script_type"] == script_type
        assert load_input_definition(item, command_name="T")["input_type"] == "python"


def test_a_task_with_no_input_type_is_every_task_that_came_before():
    item = command()
    del item["input_type"]
    del item["input"]

    definition = load_input_definition(item, command_name="T")

    assert definition["input_type"] == "none"
    assert definition["python_source"] is None


# ------------------------------------------------------------------------- reading the config

def test_the_input_block_is_read_into_a_source(root):
    source = load_input_definition(command(), command_name="T")["python_source"]

    assert source.script_path == "assets/tasks/python/fetch.py"
    assert source.args == ("7",)
    assert (source.rows_path, source.parameter, source.batch_rows) == ("data", "payload", 3)
    assert source.accept_exit_codes == (0,)


def test_an_input_block_nobody_reads_is_refused():
    """`input_type: none` with an input block is a task somebody configured and nothing runs."""
    with pytest.raises(RuntimeError, match="nothing would read it"):
        load_input_definition(command(input_type="none"), command_name="T")


def test_an_unknown_input_type_names_the_ones_that_exist():
    with pytest.raises(RuntimeError, match=r"\['none', 'python'\]"):
        load_input_definition(command(input_type="csv"), command_name="T")


def test_the_batch_parameter_must_be_declared_or_the_declare_is_never_written():
    """`build_parameter_prelude` writes `DECLARE @payload ... = ?` from the `parameters` entry. No
    entry, no DECLARE, and the SQL fails on an undeclared variable at run time instead of here."""
    item = command(parameters=[{"name": "fromdate", "type": "date"}])

    with pytest.raises(RuntimeError, match="not in its parameters"):
        load_input_definition(item, command_name="T")


def test_a_missing_script_names_the_field_to_fill():
    with pytest.raises(ps.PythonSourceError, match="input.script"):
        load_input_definition(command(input={"parameter": "payload"}), command_name="T")


def test_accepting_a_non_zero_exit_has_to_be_written_down():
    item = command(input={**command()["input"], "accept_exit_codes": [0, 1]})

    source = load_input_definition(item, command_name="T")["python_source"]

    assert source.accept_exit_codes == (0, 1)


# ------------------------------------------------------------------------------ running it

def test_the_rows_and_the_envelope_both_come_back(root):
    source = load_input_definition(command(), command_name="T")["python_source"]

    produced = ps.run(source, tool_root=root)

    assert len(produced.rows) == 7
    assert produced.exit_code == 0
    # Everything the document said apart from the rows, so a script reporting its own status and
    # error count has them on the run row rather than only in a log nobody reads.
    assert produced.envelope == {"status": "SUCCESS", "total": 7}
    assert "fetching page 1" in produced.stderr_tail


def test_the_child_prints_utf8_whatever_code_page_the_host_has(root):
    """PYTHONIOENCODING is pinned for the child. Left to `locale.getpreferredencoding()` a script
    printing `ensure_ascii=False` dies on the first accented character on this estate's Windows
    hosts — under the daemon, and never from the console it was tested in."""
    produced = ps.run(load_input_definition(command(), command_name="T")["python_source"],
                      tool_root=root)

    assert produced.rows[0]["name"] == "Nguyễn Văn A"


def test_arguments_can_carry_the_task_s_own_parameters(root):
    path = write(root, "echo_args.py",
                 "import json, sys; print(json.dumps({'data': [{'a': sys.argv[1]}]}))")
    source = ps.PythonSource(script_path=path, args=("--from", "{fromdate}"))

    produced = ps.run(source, tool_root=root, parameter_values={"fromdate": "2026-09-01"})

    assert produced.rows == [{"a": "--from"}]


def test_an_argument_naming_a_parameter_that_does_not_exist_is_refused(root):
    """It would otherwise reach the script as the literal `{fromdate}` and fail a long way from
    the config that caused it."""
    source = ps.PythonSource(script_path="assets/tasks/python/fetch.py", args=("{nope}",))

    with pytest.raises(ps.PythonSourceError, match="does not declare"):
        ps.run(source, tool_root=root, parameter_values={"fromdate": "x"})


# ------------------------------------------------------------------------------- the refusals

@pytest.mark.parametrize("label,body,rows_path,expected", [
    ("a stray print", "print('oops')", "data", "did not print JSON"),
    ("a bare array", "import json; print(json.dumps([1, 2]))", "data", "not a JSON object"),
    ("nothing at all", "pass", "data", "printed nothing to stdout"),
    ("the wrong path", "import json; print(json.dumps({'data': []}))", "items", "no 'items'"),
    ("rows that are not rows", "import json; print(json.dumps({'data': {}}))", "data",
     "not a list"),
])
def test_stdout_that_is_not_one_json_document_of_rows_is_refused(root, label, body, rows_path,
                                                                 expected):
    source = ps.PythonSource(script_path=write(root, "bad.py", body), rows_path=rows_path)

    with pytest.raises(ps.PythonSourceError, match=expected):
        ps.run(source, tool_root=root)


def test_a_non_zero_exit_stops_the_task_even_when_it_printed_rows(root):
    """The failure this feature invites: a fetcher that gives up on half its pages, prints what it
    got, and exits 1. A load of half the rows looks exactly like a load of all of them."""
    body = ("import json, sys\n"
            "print('page 3 failed', file=sys.stderr)\n"
            "print(json.dumps({'data': [{'a': 1}]}))\n"
            "sys.exit(1)\n")
    source = ps.PythonSource(script_path=write(root, "partial.py", body))

    with pytest.raises(ps.PythonSourceError) as caught:
        ps.run(source, tool_root=root)

    assert "exited 1" in str(caught.value)
    assert "Nothing was sent to the database" in str(caught.value)
    # The reason the script gave, not just the exit code.
    assert "page 3 failed" in str(caught.value)


def test_the_same_partial_run_is_accepted_when_the_config_says_so(root):
    body = ("import json, sys\n"
            "print(json.dumps({'data': [{'a': 1}], 'errors': ['page 3']}))\n"
            "sys.exit(1)\n")
    source = ps.PythonSource(script_path=write(root, "partial.py", body),
                             accept_exit_codes=(0, 1))

    produced = ps.run(source, tool_root=root)

    assert produced.exit_code == 1
    assert produced.envelope["errors"] == ["page 3"]


def test_a_script_that_hangs_is_killed_and_says_it_loaded_nothing(root):
    source = ps.PythonSource(script_path=write(root, "hang.py", "import time; time.sleep(30)"),
                             timeout_seconds=1)

    with pytest.raises(ps.PythonSourceError, match="did not finish within"):
        ps.run(source, tool_root=root)


def test_a_script_outside_the_tool_root_is_refused(root):
    """`input.script` is configuration. Configuration that can name any file on the machine is
    configuration that can run any file on the machine."""
    with pytest.raises(ps.PythonSourceError, match="outside the tool root"):
        ps.resolve_script("../../../etc/passwd", tool_root=root)


# ------------------------------------------------------------------------------- the batching

def test_a_final_script_runs_once_after_every_batch(root):
    """A step that rolls the loaded rows onward is not a row consumer. Run per batch it fires 29
    times for a 29-batch window, which is neither what it means nor what it costs."""
    from pathlib import Path

    steps = build_execution_plan(
        sql_paths=[Path("load.sql")], configured_names=("load.sql",), parameter_values={},
        payloads=["[1]", "[2]", "[3]"], payload_parameter="payload",
        final_paths=[Path("sync.sql")], final_names=("sync.sql",))

    assert [s.label for s in steps] == [
        "[1/4] load.sql [batch 1/3]", "[2/4] load.sql [batch 2/3]",
        "[3/4] load.sql [batch 3/3]", "[4/4] sync.sql [final]"]
    # and it is NOT handed the last batch, which would be the subtlest way to write a step that
    # looks like it saw everything and saw one batch of it.
    assert steps[-1].parameter_values.get("payload") is None


def test_a_final_script_is_read_off_the_command(root):
    item = command(final_script_paths=["assets/tasks/sqlserver/load.sql"])

    definition = load_sql_script_definition(item, data_dir=root / "data")

    assert definition["final_script_files"] == ("assets/tasks/sqlserver/load.sql",)
    assert definition["script_files"] == ("assets/tasks/sqlserver/load.sql",),         "the per-batch list is untouched by it"


def test_a_task_with_no_final_script_is_unchanged(root):
    definition = load_sql_script_definition(command(), data_dir=root / "data")

    assert definition["final_script_files"] == ()


def test_final_script_paths_must_be_a_list(root):
    with pytest.raises(RuntimeError, match="final_script_paths"):
        load_sql_script_definition(command(final_script_paths="one.sql"), data_dir=root / "data")


def test_rows_are_split_into_batches_of_the_configured_size():
    payloads = ps.batches([{"i": i} for i in range(7)], 3)

    assert [len(json.loads(text)) for text in payloads] == [3, 3, 1]


def test_an_empty_pull_still_runs_the_sql_once():
    """Otherwise a quiet day is a task that silently does nothing, and the SQL — which is also
    where anything that must happen regardless would live — never runs to say so."""
    assert ps.batches([], 100) == ["[]"]


def test_every_batch_runs_the_whole_file_list_in_order():
    """The files stay the inner loop. A task's files are a sequence that belongs together — stage,
    merge, log — and running file 1 thirty times before file 2 has ever run breaks that."""
    steps = build_execution_plan(
        sql_paths=[__import__("pathlib").Path("a.sql"), __import__("pathlib").Path("b.sql")],
        configured_names=("a.sql", "b.sql"), parameter_values={},
        payloads=["[1]", "[2]"], payload_parameter="payload")

    assert [(s.sql_path.name, s.parameter_values["payload"]) for s in steps] == [
        ("a.sql", "[1]"), ("b.sql", "[1]"), ("a.sql", "[2]"), ("b.sql", "[2]")]
    assert [s.label for s in steps] == [
        "[1/4] a.sql [batch 1/2]", "[2/4] b.sql [batch 1/2]",
        "[3/4] a.sql [batch 2/2]", "[4/4] b.sql [batch 2/2]"]


def test_a_task_with_no_python_input_plans_exactly_as_it_always_did():
    """The counterweight. Every existing task goes through this function now, and none of them
    may gain a batch label, an extra step, or a parameter it did not have."""
    from pathlib import Path

    steps = build_execution_plan(
        sql_paths=[Path("a.sql"), Path("b.sql")], configured_names=("a.sql", "b.sql"),
        parameter_values={"session_id": 7})

    assert [s.label for s in steps] == ["[1/2] a.sql", "[2/2] b.sql"]
    assert all(s.parameter_values == {"session_id": 7} for s in steps)
    assert all(s.batch_label == "" for s in steps)
