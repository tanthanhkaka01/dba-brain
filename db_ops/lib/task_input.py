"""What a scheduled SQL task's **input** may say — the reserved placeholder names.

The mirror of :mod:`db_ops.lib.task_output`, which names what a task does with its result set. This
names the one part of its *input* that neither the task nor the operator chooses: the placeholders
the runner fills from the ``sql_targets`` entry it is running for.

**Why it is here and not in either caller.** Two places need it and neither may import the other:
``common.sql_task_admin`` validates ``input.args`` while registering a command, and
``sql_tasks.python_source`` substitutes the values while running one. ``common`` is the API layer and
does not import an app (ORD 13); an app does not import ``common`` either — it invokes its CLI. So
the constant was spelled out twice, with a comment in ``common`` explaining that it had to be, and
``tests/test_no_duplicate_definitions.py`` found the two copies on 2026-09-19. The layer rule was
right and the conclusion was wrong: a value both layers need is exactly what ``lib`` is for, and
both of them may import it.

The cost of two copies is not theoretical. Adding a third placeholder to the runner without adding
it here would make ``sql-command-add`` refuse an ``input.args`` the runner can fill perfectly well,
and the refusal would name the argument rather than the list it was checked against.
"""

from __future__ import annotations

from db_ops.lib import errors
from dataclasses import dataclass
from typing import Any

#: The names ``input.args`` may use **without declaring them as parameters**: the runner adds them to
#: the substitution values from the target it is running for — its ``server_id`` and
#: ``database_name``, as configured. A script that must reach the target itself takes them from here,
#: which is what lets one command run on every tier it has a target for.
TARGET_PLACEHOLDERS: tuple[str, ...] = ("target_server_id", "target_database")

__all__ = ["TARGET_PLACEHOLDERS", "DEFAULT_BATCH_ROWS", "DEFAULT_FETCH_TIMEOUT_SECONDS", "PythonSource",
           "PythonSourceError", "parse"]


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


class PythonSourceError(errors.DbOpsError, RuntimeError):
    """The task's Python step produced no rows, so the task's SQL never ran.

    It says nothing about what the *program* did. A fetch has written nothing; a program that
    does its own work - a stored procedure per row, say - may have done most of it before it
    failed, and db_ops cannot see that from here. The messages below are careful about the
    difference: a person reading that nothing reached the database stops looking.
    """

    kind = errors.KIND_CONFIG


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
