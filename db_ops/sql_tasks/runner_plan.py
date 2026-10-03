"""A SQL task's run, planned: the steps it executes, the progress it reports, the notify rule it reads, and the workflow a task code names.

Split out of ``sql_tasks/runner.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``runner`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib.text_format import format_log_value, format_message_time  # noqa: F401 - one definition, see that module
from db_ops.lib.data_sources import _server_id_from_instance  # noqa: F401 - one definition, see that module
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from db_ops.lib.sql_task_catalog import (  # noqa: F401 - re-exported for this module's callers
    _AMBIGUOUS_CREDENTIAL, DEFAULT_INLINE_MAX_ROWS, DEFAULT_SQL_TIMEOUT_SECONDS, INPUT_TYPES,
    SQL_TARGET_NOTIFY_DEFAULTS, XLSX_MAX_ROWS, SqlCommand, SqlTarget, _opt_str, collect_sql_tasks,
    load_default_credential_names, load_input_definition, load_sql_access_by_server,
    load_sql_commands, load_sql_script_definition, load_sql_targets, resolve_sql_folder)
from db_ops.lib.notify import (
    NotifyRule,
    parse_notify_rule as common_parse_notify_rule,
)
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module


@dataclass(frozen=True)
class ExecutionStep:
    """One SQL file, run once, with the parameter values that run gets.

    A plain task has one step per file and this is only bookkeeping. A ``script_type: "python"``
    task has one step per (batch, file): the same SQL runs again for the next few thousand rows,
    with the batch bound to its parameter. Building the whole plan first is what keeps that a
    single loop with a single failure path, rather than a nested one where "which file failed"
    stops being a straight answer.
    """

    file_no: int
    total: int
    sql_path: Path
    configured_name: str
    #: ``[3/30]`` for a batched task, ``""`` for every other one, so a message reads
    #: ``[2/4] load.sql [3/30]`` only when there is a second axis to report.
    batch_label: str
    parameter_values: dict[str, Any]

    @property
    def label(self) -> str:
        base = f"[{self.file_no}/{self.total}] {self.sql_path.name}"
        return f"{base} {self.batch_label}" if self.batch_label else base


def build_execution_plan(
    *,
    sql_paths: list[Path],
    configured_names: tuple[str, ...],
    parameter_values: dict[str, Any] | None,
    payloads: list[str] | None = None,
    payload_parameter: str = "",
    final_paths: list[Path] | None = None,
    final_names: tuple[str, ...] = (),
) -> list[ExecutionStep]:
    """The ordered list of SQL executions this run performs.

    ``payloads`` is the batched JSON from a python source; without it the plan is what it has
    always been, one step per file in order. With it, the *files* stay the inner loop: batch 1
    runs every file, then batch 2 does, because a task's files are a sequence that belongs
    together (stage, then merge, then log) and running file 1 thirty times before file 2 has ever
    run would break every folder task's meaning of order.
    """
    base = dict(parameter_values or {})
    chunks = payloads if payloads is not None else [None]
    final_paths = list(final_paths or ())
    total = len(sql_paths) * len(chunks) + len(final_paths)
    steps: list[ExecutionStep] = []
    file_no = 0
    for batch_no, payload in enumerate(chunks, start=1):
        for index, sql_path in enumerate(sql_paths):
            file_no += 1
            values = dict(base)
            if payload is not None and payload_parameter:
                values[payload_parameter] = payload
            steps.append(ExecutionStep(
                file_no=file_no, total=total, sql_path=sql_path,
                configured_name=configured_names[index] if index < len(configured_names)
                else str(sql_path),
                batch_label=f"[batch {batch_no}/{len(chunks)}]" if payload is not None else "",
                parameter_values=values,
            ))
    # Once, after the last batch, and **without the payload bound**: a final step is not a row
    # consumer. Binding it anyway would hand it the last batch, which is the subtlest possible
    # way to write a step that looks like it saw everything and saw one batch of it.
    for index, sql_path in enumerate(final_paths):
        file_no += 1
        steps.append(ExecutionStep(
            file_no=file_no, total=total, sql_path=sql_path,
            configured_name=final_names[index] if index < len(final_names) else str(sql_path),
            batch_label="[final]" if payloads is not None else "",
            parameter_values=dict(base),
        ))
    return steps


def _progress_summary(file_results: list[dict[str, Any]], total_files: int) -> str:
    """What a failed multi-file run actually got done, for the last message it will ever send.

    A folder task that dies on file 3 of 5 used to end on the SQL error alone. That names the
    fault but not the state: whether files 1 and 2 committed, how many rows they wrote, whether
    anything ran at all. The done path has always ended with totals, and a failure is exactly when
    somebody needs them — the next decision is "re-run the whole folder, or resume from 3".

    Empty when the task is not multi-file, so a single-file failure stays one line.
    """
    if total_files <= 1:
        return ""
    done = len(file_results)
    if not done:
        return f"\nfiles done: 0/{total_files} (nothing completed)"
    rows = sum(int(item.get("row_count") or 0) for item in file_results)
    # `file_name` is the configured value, which for a folder task is an absolute path on the
    # machine that built the image. The stored record keeps it; the chat message wants the name.
    names = ", ".join(Path(str(item.get("file_name") or "")).name for item in file_results)
    return f"\nfiles done: {done}/{total_files} ({names}), {rows} row(s)"


def parse_notify_rule(value: Any, *, default_level: str) -> NotifyRule:
    """Parse one ``logging_on_run``/``alert_on_error`` switch on a SQL target.

    A thin adapter over :func:`db_ops.lib.notify.parse_notify_rule`, which owns the shape
    for every app. Kept for callers that hold a single rule rather than a whole entry.
    """
    return common_parse_notify_rule(
        value, default=NotifyRule(enabled=False, telegram_chat=default_level)
    )


@dataclass(frozen=True)
class SqlScanResult:
    due_count: int = 0
    success_count: int = 0
    error_count: int = 0
    #: Tasks a **claim** turned away because another run already holds them. Neither a success nor
    #: a failure, and counted apart from both: folding it into errors would alert on the scheduler
    #: working correctly, and folding it into successes would say work happened that did not.
    skipped_count: int = 0


def workflow_name_from_code(sql_task_code: str) -> str:
    code = sql_task_code.lower()
    if code.startswith("data_finalize"):
        return "data_finalize"
    if code.startswith("check_errorjob"):
        return "check_errorjob"
    return code
