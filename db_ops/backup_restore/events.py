from __future__ import annotations

import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any

from db_ops.lib.notify import NotifyConfig
from db_ops.config import DbOpsConfig
from db_ops.db.store import DbOpsStore, utc_now_text
from db_ops.db.job_runs import JobRun
from db_ops.levels import normalize_level
from db_ops.logging_ops import log_event


# Which id identifies a run. A backup job is keyed by backup_id; everything else in this
# app (restore-latest, restore-workflow, copy-backup, delete-backup, verify-restore) is a
# step of a restore entry and is keyed by restore_id.
BACKUP_COMMAND_PREFIX = "backup"
UNKNOWN_RUN_ID = "<unknown>"


def run_id_key(command: str) -> str:
    """The id field that identifies a run of ``command``: ``backup_id`` or ``restore_id``."""
    head = str(command or "").split(".", 1)[0].strip().lower()
    return "backup_id" if head == BACKUP_COMMAND_PREFIX else "restore_id"


def resolve_run_id(command: str, metadata: dict[str, Any] | None) -> tuple[str, str]:
    """Find the run id for this event: ``(key, value)``, value ``"<unknown>"`` if absent.

    Every backup/restore message an operator receives must name the run it belongs to —
    without it a Telegram alert cannot be tied back to a config entry, a `job_runs` row, or
    a log file. The id is looked for in the obvious single-value key first, then in the
    plural key, then inside the per-entry lists a multi-entry workflow carries. It is never
    silently dropped: an event that cannot name its run says ``<unknown>`` out loud.
    """
    key = run_id_key(command)
    values = _collect_run_ids(key, metadata or {})
    return key, (",".join(values) if values else UNKNOWN_RUN_ID)


def _collect_run_ids(key: str, metadata: dict[str, Any]) -> list[str]:
    ordered: list[str] = []

    def _add(value: Any) -> None:
        # A resolved id is written back into the metadata, and several ids are joined with
        # commas — so splitting here keeps re-resolving the same event idempotent instead of
        # compounding "R1,R2" into "R1,R2,R1,R2".
        for part in str(value or "").split(","):
            text = part.strip()
            if text and text not in ordered:
                ordered.append(text)

    _add(metadata.get(key))
    for item in metadata.get(f"{key}s") or []:
        _add(item)
    # A multi-entry restore workflow names its entries only inside these lists.
    for list_key in ("mappings", "per_restore_results", "restores", "jobs"):
        for item in metadata.get(list_key) or []:
            if isinstance(item, dict):
                _add(item.get(key))
    return ordered


def emit_backup_restore_event(
    *,
    app_config: DbOpsConfig,
    command: str,
    phase: str,
    level: str,
    message: str,
    logger: logging.Logger | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
    error_text: str | None = None,
    metadata: dict[str, Any] | None = None,
    notify: NotifyConfig | None = None,
) -> None:
    """Record one backup/restore event to the log file, ``job_runs`` and Telegram.

    ``notify`` is the emitting job's :class:`db_ops.lib.notify.NotifyConfig` — the shared
    ``notify`` object every app's config carries. Only the Telegram push consults it; the file
    log and the run row are the record of what happened and are always written, whatever the
    alerting choice.
    """
    logical_level = normalize_level(level)
    # The run id goes into the message itself, not only the metadata: the file log, the
    # job_runs row and the Telegram header are all read as plain text, and each of them has
    # to answer "which restore/backup was this?" on its own.
    id_key, id_value = resolve_run_id(command, metadata)
    id_text = f"{id_key}={id_value}"
    event_message = f"backup_restore.{command} {phase}: {message}"
    if id_text not in event_message:
        event_message = f"backup_restore.{command} {phase}: {id_text} {message}"
    event_metadata = {
        "app": "backup_restore",
        "command": command,
        "phase": phase,
        **(metadata or {}),
    }
    event_metadata.setdefault(id_key, id_value)

    if logger:
        _safe_log_file(logger=logger, level=logical_level, message=event_message)
    _safe_insert_sqlite(
        sqlite_path=app_config.store,
        command=command,
        phase=phase,
        level=logical_level,
        message=event_message,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        error_text=error_text,
        metadata=event_metadata,
    )
    _safe_push_telegram(
        app_config=app_config,
        level=logical_level,
        message=event_message,
        metadata=event_metadata,
        notify=notify,
    )


def _safe_log_file(*, logger: logging.Logger, level: str, message: str) -> None:
    try:
        log_event(logger, level=level, message=message)
    except Exception as exc:  # noqa: BLE001 - event side effects must not fail the operation.
        print(f"backup_restore event file log failed: {exc}", file=sys.stderr)


def _safe_insert_sqlite(
    *,
    sqlite_path: str | Path,
    command: str,
    phase: str,
    level: str,
    message: str,
    started_at: str | None,
    finished_at: str | None,
    duration_ms: int | None,
    error_text: str | None,
    metadata: dict[str, Any],
) -> None:
    try:
        now = utc_now_text()
        store = DbOpsStore(sqlite_path)
        store.insert_job_run(
            JobRun(
                job_code=f"backup_restore.{command}.{phase.lower()}",
                level=level,
                status=phase.upper(),
                message=message,
                started_at=started_at or now,
                finished_at=finished_at,
                duration_ms=duration_ms,
                error_text=error_text,
                host_name=socket.gethostname(),
                metadata=metadata,
            )
        )
    except Exception as exc:  # noqa: BLE001 - SQLite event logging must not fail the operation.
        print(f"backup_restore event sqlite log failed: {exc}", file=sys.stderr)


def _safe_push_telegram(
    *, app_config: DbOpsConfig, level: str, message: str, metadata: dict[str, Any],
    notify: NotifyConfig | None = None,
) -> None:
    try:
        # The app fetches, the shared layer decides. Routing belongs to the Telegram app and is
        # read through its CLI (telegram_route below); combining that answer with this entry's
        # notify object is the one rule in common. This app applies no routing rules of its own —
        # it only says which entry the event came from.
        from db_ops.db.queue_message import queue_message, store_block
        from db_ops.lib.telegram_route import telegram_groups, telegram_route
        from db_ops.lib.notify_route import resolve_chat_id

        route = telegram_route(level)
        # The groups map is only needed by a rule that redirects to a *different* level, so it is
        # fetched only then: the common case costs one subprocess, not two.
        rule = notify.rule_for_level(level) if notify is not None else None
        redirects = bool(rule and rule.enabled and not rule.chat_id
                         and str(rule.telegram_chat or "").strip().lower()
                         not in ("", str(level or "").strip().lower()))
        chat_id = resolve_chat_id(
            level, notify, route=route, groups=telegram_groups() if redirects else None,
        )
        if not chat_id:
            return
        text = _format_telegram_message(level=level, message=message, metadata=metadata)
        command = str(metadata.get("command") or "")
        _, run_id = resolve_run_id(command, metadata)
        # A complete request: common resolves nothing on the other side. The chat is decided, the
        # text rendered, both halves of the type stated, and the store named by connection rather
        # than by a config file for the other end to read.
        queue_message({
            "store": store_block(app_config),
            "chat_id": chat_id,
            "text": text,
            # This app is the one producer that holds both halves: the phase says where in the
            # run the event sits (START/END/ERROR) and the level says how loud it is. Passing
            # both is what lets a start and a clean finish — identical at `logging` level —
            # arrive as ▶️ and ✅ instead of the same symbol.
            "phase": str(metadata.get("phase") or ""),
            "level": level,
            "note": "Backup restore event notification",
            "source_type": "backup_restore_events",
            # source_id identifies the run this message belongs to, so a queued message can be
            # traced back to its restore/backup entry without parsing the text.
            "source_id": f"{command}:{run_id}" if command else run_id,
            "metadata": metadata,
        })
    except Exception as exc:  # noqa: BLE001 - Telegram push must not fail the operation.
        print(f"backup_restore event telegram queue failed: {exc}", file=sys.stderr)


def _format_restore_workflow_telegram_message(*, level: str, message: str, metadata: dict[str, Any]) -> str:
    phase = metadata.get("phase", "")
    restore_mode = metadata.get("restore_mode", "LATEST")
    point_in_time_utc = metadata.get("point_in_time_utc")
    point_in_time_original = metadata.get("point_in_time_original")
    mappings = metadata.get("mappings")

    if phase == "START":
        header = "Restore workflow started."
    elif phase == "END":
        output = metadata.get("output") or {}
        status = output.get("status", "") if isinstance(output, dict) else ""
        if not status:
            status = str(metadata.get("status") or "")
        header = f"Restore workflow finished. status={status}"
    elif phase == "ERROR":
        header = "Restore workflow FAILED."
    else:
        header = message

    lines = [f"{level.upper()}|{socket.gethostname()}|{header}"]
    # The run id comes first, before any detail: it is what an operator needs to act on the
    # message (find the config entry, the job_runs row, the log file).
    id_key, id_value = resolve_run_id(str(metadata.get("command") or ""), metadata)
    lines.append(f"{id_key}={id_value}")
    lines.append(f"restore_mode={restore_mode}")
    if restore_mode == "POINT_IN_TIME":
        if point_in_time_original:
            lines.append(f"point_in_time={point_in_time_original}")
        if point_in_time_utc:
            lines.append(f"point_in_time_utc={point_in_time_utc}")
    trace_id = metadata.get("trace_id") or metadata.get("run_id")
    if trace_id:
        lines.append(f"trace_id={trace_id}")
    current_phase = metadata.get("current_phase")
    if current_phase:
        lines.append(f"current_phase={current_phase}")

    if mappings:
        lines.append(f"restore_count={len(mappings)}")
        if phase == "END":
            for field in ("databases_considered", "success", "failed", "skipped", "duration_seconds"):
                val = metadata.get(field)
                if val is not None:
                    lines.append(f"{field}={val}")
        per_restore_results: list[Any] = metadata.get("per_restore_results") or []
        if phase == "END" and per_restore_results:
            for per_r in per_restore_results:
                if not isinstance(per_r, dict):
                    continue
                lines.append("")
                lines.append(f"restore_id={per_r.get('restore_id', '')}")
                lines.append(f"source_id={per_r.get('source_id', '')}")
                lines.append(f"target_id={per_r.get('target_id', '')}")
                if per_r.get("target_host"):
                    lines.append(f"target_host={per_r['target_host']}")
                if per_r.get("status"):
                    lines.append(f"status={per_r['status']}")
                for field in ("databases_considered", "success", "failed", "skipped"):
                    val = per_r.get(field)
                    if val is not None:
                        lines.append(f"{field}={val}")
        else:
            for m in mappings:
                if not isinstance(m, dict):
                    continue
                lines.append("")
                lines.append(f"restore_id={m.get('restore_id', '')}")
                lines.append(f"source_id={m.get('source_id', '')}")
                lines.append(f"target_id={m.get('target_id', '')}")
                if m.get("target_host"):
                    lines.append(f"target_host={m['target_host']}")
                if m.get("target_os_type"):
                    lines.append(f"target_os_type={m['target_os_type']}")
    else:
        target_id = metadata.get("target_id", "")
        target_host = metadata.get("target_host", "")
        target_parts = [p for p in [target_id, target_host] if p]
        if target_parts:
            lines.append(f"target={' / '.join(target_parts)}")

    if phase == "ERROR":
        for field in (
            "database_name",
            "exception_type",
            "exception_message",
            "stdout_tail",
            "stderr_tail",
            "sql_error_text",
            "error_tail",
            "log_file_path",
        ):
            value = metadata.get(field)
            if value:
                lines.append(f"{field}={value}")

    # No clip: db_ops.telegram.api splits an over-long body across messages, so the tail of a
    # restore's field list survives instead of being cut off mid-word.
    return "\n".join(lines)


def _format_telegram_message(*, level: str, message: str, metadata: dict[str, Any]) -> str:
    if metadata.get("command") == "restore-workflow":
        return _format_restore_workflow_telegram_message(level=level, message=message, metadata=metadata)
    # Own line for the run id: it used to be reachable only inside the JSON payload, which was
    # clipped at 3900 characters — so the id was cut off exactly when the message mattered most.
    # The clip is gone (the send layer splits instead) but the line stays: an id on its own line
    # survives a split intact, while one inside a long JSON blob lands in whichever part it falls.
    id_key, id_value = resolve_run_id(str(metadata.get("command") or ""), metadata)
    payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    return f"{level.upper()}|{socket.gethostname()}|{message}\n{id_key}={id_value}\n{payload}"


# A run's stored output keeps both ends, not just the tail. The phase lines a script prints
# first - "PHASE=copy-backup ... copied=N skipped=N pruned=N", the chain it selected, the
# staging decision - are the ones that explain what the run actually did, and a tail-only
# excerpt loses exactly those: SQL Server alone prints hundreds of upgrade lines that push the
# head out. The tail still matters because that is where a failure says why, so keep both and
# say how much was dropped in between.
STDOUT_EXCERPT_HEAD = 900
STDOUT_EXCERPT_TAIL = 1500


def stdout_excerpt(text: str, *, head: int = STDOUT_EXCERPT_HEAD, tail: int = STDOUT_EXCERPT_TAIL) -> str:
    """The stored excerpt of a script's output: its first lines and its last, never the middle."""
    text = text or ""
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    marker = f"\n... [{dropped} characters omitted] ...\n"
    return text[:head] + marker + text[-tail:]


def announce(on_phase: Any, phase: str, message: str, extra: dict[str, Any] | None = None) -> None:
    """Report a step boundary, and never let reporting break the restore.

    Two guarantees, and both are about what must *not* happen. A caller that passes nothing — a
    script, a test — still restores. And an emit that raises (a Telegram queue that is down, a
    store that cannot be reached) must not turn a good restore into a failed one: the run row and
    the log still record what happened, so the loud failure would be about the *reporting*.

    One copy, from 2026-08-16. It was four — ``cli._say``, ``restore_by_id._announce``,
    ``restore_script._announce`` and ``server_metadata._announce`` — and the fourth had **already
    drifted**: it named its parameters ``announce``/``event`` rather than ``on_phase``/``phase``,
    so the three that looked interchangeable were not quite. That is the whole argument for the
    rule, found in the code that motivated writing it down.
    """
    if on_phase is None:
        return
    try:
        on_phase(phase, message, extra or {})
    except Exception:  # noqa: BLE001 - announcing is best-effort by design.
        pass
