r"""A SQL task whose input is produced by a Python script.

Some data does not start in a database. An HTTP API, a vendor export, a device that speaks only
its own protocol — the rows exist, and the only thing standing between them and a table is a
program. Until this existed the answer was a script outside db_ops that opened its own connection,
held its own copy of the credential, and was scheduled by something else; which is the shape this
whole tool exists to replace.

So a task gains a second axis. ``script_type`` goes on saying what the SQL half is —
``single | array | folder``, unchanged — and **``input_type`` says where the rows come from**:

.. code-block:: json

    {
      "script_type": "single",
      "script_path": "assets/tasks/sqlserver/031_load_attendance.sql",
      "input_type": "python",
      "input": {
        "script": "assets/tasks/python/get_attendance.py",
        "args": ["--fromdate", "{fromdate}", "--todate", "{todate}"],
        "rows_path": "data",
        "parameter": "payload",
        "batch_rows": 2000
      },
      "parameters": [
        {"name": "payload",  "type": "nvarchar(max)"},
        {"name": "fromdate", "type": "date"},
        {"name": "todate",   "type": "date"}
      ]
    }

The two are deliberately separate. Folding "runs a python script" into ``script_type`` makes every
combination of the two a new word, and the first thing it forced was a python task pretending to be
an ``array`` — a spelling that is right about the files and silent about the part that matters.

The task then runs that program, reads **one JSON document from its stdout**, and hands the rows to
its own SQL as a bound parameter, in batches, through the same executor and the same credential as
every other SQL task. The SQL author writes ordinary T-SQL against ``@payload`` and does whatever
they want with it — ``OPENJSON`` into a staging table and ``MERGE`` is the obvious shape, and
nothing here knows or cares which.

**The contract is one document on stdout, not a file.** A file would need a path both sides agree
on, cleanup nobody does, and a decision about what happens when the previous run's file is still
there. stdout has none of that, and it is what a program that prints JSON already does. Progress
and diagnostics go to **stderr**, which is captured into the run row and never parsed.

Three things this refuses, each because the quiet version is worse:

* **A non-zero exit is a failure**, unless the command lists that code in
  ``input.accept_exit_codes``. A fetcher that gives up on half its pages and still prints what it
  got is not obviously wrong — it is a decision, and the config has to make it out loud, or a
  partial load looks exactly like a complete one.
* **stdout that is not one JSON object is a failure**, and the message shows the first bytes. A
  script with a stray ``print()`` in it produces a document that is *almost* JSON, and "expecting
  value: line 1 column 1" says nothing about the ``Traceback`` sitting above it.
* **The rows must be a list.** ``input.rows_path`` names where they are; if that path holds an
  object or a string the task stops rather than sending one row that is the whole document.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: How many rows go into one execution of the SQL. Not one big parameter: the estate's first user
#: of this pulls ten days of attendance scans - tens of thousands of rows, megabytes of JSON - and
#: an ``nvarchar(max)`` that size is a parameter the driver, the network and ``OPENJSON`` each get
#: to be slow about at once. Batching also bounds what a failure costs: the run stops on batch 7
#: of 30 having committed 6, and the log says so.
DEFAULT_BATCH_ROWS = 2000

#: A fetch is allowed longer than a query. The default here is the one the estate's first script
#: needs for ten days across two companies at its own concurrency; a task that needs more says so.
#:
#: Named for the fetch rather than `DEFAULT_TIMEOUT_SECONDS`, which `common/schema_copy.py` already
#: uses for its own unrelated deadline. The two share a number and nothing else, and one name over
#: two rules is how the second gets found by whoever is debugging why the first did not apply.
DEFAULT_FETCH_TIMEOUT_SECONDS = 900

#: How much of the script's stderr is kept in the run row. Enough to hold a traceback and the last
#: few progress lines, not enough to turn a chatty script into a megabyte per run.
STDERR_TAIL_CHARS = 4000

#: How much of an unparsable stdout is quoted back. The useful part of "this is not JSON" is
#: whatever the program printed instead, and that is nearly always at the top.
STDOUT_HEAD_CHARS = 600


class PythonSourceError(RuntimeError):
    """The task's Python step produced no rows, so the task's SQL never ran.

    It says nothing about what the *program* did. A fetch has written nothing; a program that
    does its own work - a stored procedure per row, say - may have done most of it before it
    failed, and db_ops cannot see that from here. The messages below are careful about the
    difference: a person reading that nothing reached the database stops looking.
    """


@dataclass(frozen=True)
class PythonSource:
    """The ``input`` block of an ``input_type: "python"`` command."""

    script_path: str
    args: tuple[str, ...] = ()
    #: Dot path to the row array inside the document, e.g. ``data`` or ``result.items``.
    rows_path: str = "data"
    #: The SQL parameter each batch is bound to. It must also appear in ``parameters`` with a type
    #: — ``nvarchar(max)`` — because that is what writes the ``DECLARE`` the script reads.
    parameter: str = "payload"
    batch_rows: int = DEFAULT_BATCH_ROWS
    timeout_seconds: int = DEFAULT_FETCH_TIMEOUT_SECONDS
    accept_exit_codes: tuple[int, ...] = (0,)


@dataclass(frozen=True)
class PythonResult:
    """What the script produced, and enough about the run to explain it afterwards."""

    rows: list[Any]
    exit_code: int
    duration_ms: int
    stdout_bytes: int
    stderr_tail: str
    #: Every top-level key of the document except the rows, so a script that reports its own
    #: ``status``/``total``/``errors`` has them on the run row rather than only in its stderr.
    envelope: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "rows": len(self.rows), "exit_code": self.exit_code,
            "duration_ms": self.duration_ms, "stdout_bytes": self.stdout_bytes,
            "envelope": self.envelope,
        }


def parse(block: dict[str, Any], *, command_name: str) -> PythonSource:
    """Read one ``input`` block (``input_type: "python"``), or say what is missing.

    The keys are unprefixed because the block already says what it is: ``input.script``, not
    ``python_path``. A prefix here would be doing the naming that ``input_type`` does properly.
    """
    script_path = str(block.get("script") or block.get("path") or "").strip()
    if not script_path:
        raise PythonSourceError(
            f"SQL command {command_name} input_type=python requires input.script - the program "
            "whose stdout is this task's input, e.g. assets/tasks/python/fetch_x.py.")

    raw_args = block.get("args") or []
    if not isinstance(raw_args, list):
        raise PythonSourceError(
            f"SQL command {command_name} input.args must be an array of strings.")

    raw_codes = block.get("accept_exit_codes")
    if raw_codes is None:
        accept = (0,)
    elif isinstance(raw_codes, list) and all(isinstance(code, int) for code in raw_codes):
        accept = tuple(raw_codes) or (0,)
    else:
        raise PythonSourceError(
            f"SQL command {command_name} input.accept_exit_codes must be an array of integers, "
            "e.g. [0, 1] for a fetcher that exits 1 when some pages failed but still prints what "
            "it got. Listing a code is a decision that a partial load is acceptable.")

    batch_rows = int(block.get("batch_rows") or DEFAULT_BATCH_ROWS)
    if batch_rows < 1:
        raise PythonSourceError(
            f"SQL command {command_name} input.batch_rows must be at least 1.")

    return PythonSource(
        script_path=script_path,
        args=tuple(str(value) for value in raw_args),
        rows_path=str(block.get("rows_path") or "data").strip(),
        parameter=str(block.get("parameter") or "payload").strip(),
        batch_rows=batch_rows,
        timeout_seconds=int(block.get("timeout_seconds") or DEFAULT_FETCH_TIMEOUT_SECONDS),
        accept_exit_codes=accept,
    )


def resolve_script(script_path: str, *, tool_root: Path) -> Path:
    """The script, as an absolute path under the tool root.

    Refused if it escapes the root: ``input.script`` is configuration, and configuration that can
    name any file on the machine is configuration that can run any file on the machine.
    """
    candidate = Path(script_path)
    resolved = (candidate if candidate.is_absolute() else tool_root / candidate).resolve()
    root = Path(tool_root).resolve()
    if root not in resolved.parents and resolved != root:
        raise PythonSourceError(
            f"input.script {script_path} resolves outside the tool root ({resolved}). A task's "
            "script lives with the task, under assets/tasks/python/.")
    if not resolved.is_file():
        raise PythonSourceError(f"input.script not found: {resolved}")
    return resolved


#: Placeholders the RUNNER fills from the sql_targets entry the task is running on, next to the
#: task's own parameters: its ``server_id`` and ``database_name``, as configured. A script that
#: must reach the target itself takes it from here, so one command runs on every tier it has a
#: target for.
TARGET_PLACEHOLDERS = ("target_server_id", "target_database")


def substitute(args: tuple[str, ...], values: dict[str, Any]) -> list[str]:
    """``{name}`` in an argument becomes that parameter's value.

    So one command can be scheduled for its default window and re-run for a named date range from
    Telegram, without a second entry. An unknown name is refused rather than left in place: a
    script handed a literal ``{fromdate}`` fails somewhere far from the config that caused it.
    The runner adds the reserved ``{target_*}`` names (:data:`TARGET_PLACEHOLDERS`) to ``values``.
    """
    out: list[str] = []
    for arg in args:
        text = str(arg)
        for start in range(len(text)):
            if text[start] != "{":
                continue
            end = text.find("}", start)
            if end < 0:
                continue
            name = text[start + 1:end]
            if name and name not in values:
                raise PythonSourceError(
                    f"input.args references {{{name}}}, which this command does not declare as a "
                    f"parameter. Declared: {sorted(values) or 'none'}.")
        out.append(text.format(**{key: "" if value is None else value
                                  for key, value in values.items()}))
    return out


def run(source: PythonSource, *, tool_root: Path,
        parameter_values: dict[str, Any] | None = None,
        target: dict[str, str] | None = None) -> PythonResult:
    """Run the script and return its rows. Raises :class:`PythonSourceError` with the reason.

    ``target`` fills the reserved ``{target_server_id}`` / ``{target_database}`` placeholders from
    the sql_targets entry the task runs on. A task parameter of the same name wins.
    """
    import time

    script = resolve_script(source.script_path, tool_root=tool_root)
    args = substitute(source.args, {**(target or {}), **dict(parameter_values or {})})

    environment = dict(os.environ)
    # Pinned for the same reason `lib.common_cli.spawn` pins it, and this end matters more: the
    # script prints JSON with `ensure_ascii=False`, so a Vietnamese name reaches stdout as UTF-8
    # bytes. Left to `locale.getpreferredencoding()` the child would encode cp1252 on this
    # estate's Windows hosts and die on the first accented character - under the daemon, and never
    # from the console somebody tests it in, because those two have different code pages.
    environment["PYTHONIOENCODING"] = "utf-8"

    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, str(script), *args],
            cwd=str(tool_root), env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=source.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise PythonSourceError(
            f"{script.name} did not finish within input.timeout_seconds "
            f"({source.timeout_seconds}s) and was killed, so this task ran no SQL of its own. "
            f"Whatever the program did before it was killed stands.") \
            from exc
    except OSError as exc:
        raise PythonSourceError(f"{script.name} could not run: {exc}") from exc
    duration_ms = int((time.monotonic() - started) * 1000)

    stderr_tail = (completed.stderr or "")[-STDERR_TAIL_CHARS:]
    if completed.returncode not in source.accept_exit_codes:
        raise PythonSourceError(
            f"{script.name} exited {completed.returncode}; accepted (input.accept_exit_codes): "
            f"{list(source.accept_exit_codes)}. This task ran no SQL of its own; whatever the "
            f"program did before it exited stands. "
            f"stderr tail: {stderr_tail or '(empty)'}")

    document = _load_document(completed.stdout or "", script_name=script.name)
    rows = _rows_at(document, source.rows_path, script_name=script.name)
    envelope = {key: value for key, value in document.items()
                if key != source.rows_path.split(".", 1)[0]}

    return PythonResult(
        rows=rows, exit_code=completed.returncode, duration_ms=duration_ms,
        stdout_bytes=len(completed.stdout or ""), stderr_tail=stderr_tail, envelope=envelope,
    )


def _load_document(stdout: str, *, script_name: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise PythonSourceError(
            f"{script_name} printed nothing to stdout. A python task's contract is one JSON "
            "object on stdout; progress and diagnostics belong on stderr.")
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise PythonSourceError(
            f"{script_name} did not print JSON to stdout ({exc}). First {STDOUT_HEAD_CHARS} "
            f"characters were: {text[:STDOUT_HEAD_CHARS]!r}") from exc
    if not isinstance(document, dict):
        raise PythonSourceError(
            f"{script_name} printed a {type(document).__name__}, not a JSON object. The rows live "
            "under a named key so the document can also carry status, totals and errors.")
    return document


def _rows_at(document: dict[str, Any], rows_path: str, *, script_name: str) -> list[Any]:
    node: Any = document
    for part in rows_path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise PythonSourceError(
                f"{script_name} printed no {rows_path!r} in its output. Top-level keys: "
                f"{sorted(document)}. Set input.rows_path to where the row array is.")
        node = node[part]
    if not isinstance(node, list):
        raise PythonSourceError(
            f"{script_name} has {rows_path!r} as a {type(node).__name__}, not a list. The task "
            "would otherwise send the whole document as one row.")
    return node


def batches(rows: list[Any], batch_rows: int) -> list[str]:
    """The rows as JSON arrays of at most ``batch_rows`` each — **always at least one**.

    An empty pull still runs the SQL once, with ``[]``. The alternative is a task that silently
    does nothing on a quiet day, and the SQL is then the only place that can say "nothing to load"
    — which is also where a caller would put anything else the run must do regardless.
    """
    if not rows:
        return ["[]"]
    return [json.dumps(rows[start:start + batch_rows], ensure_ascii=False, default=str)
            for start in range(0, len(rows), batch_rows)]
