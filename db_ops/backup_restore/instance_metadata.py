"""This app's client for the instance-metadata commands in ``db_ops.common.cli``.

The work already lived in ``common`` (:mod:`db_ops.common.sqlserver_instance`) and already had a
CLI face — ``sqlserver-export-instance`` / ``sqlserver-replay-instance`` / ``verify`` — but this app
reached it by *importing* the function. That is one call short of the model: an app supplies data
and ``common`` performs, across the CLI boundary, with the whole request stated as JSON.

What stays on this side is the half that is genuinely the app's: which entry asked, which bundle
directory belongs to which ``server_id``, which phases to run and in what order. What crosses is a
finished request.

The request goes in on **stdin**, never argv. A replay resolves secret references, and this app
resolves them before they cross; putting that in a command line would publish them to the process
table.

Failures come back as a result, never as an exception. The user databases are the deliverable:
losing the logins and Agent jobs around them is worth a loud warning, not a restore that reports
failure when everything was in fact restored.
"""

from __future__ import annotations

from typing import Any

from db_ops.lib import common_cli

#: Long enough for a real instance: 145 KB of Agent job SQL against a remote server is not fast.
#: The deadline is stated here rather than in the transport because this caller is the one that
#: knows what it is waiting for.
_TIMEOUT_SECONDS = 1800


def run_metadata_command(command: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run one ``sqlserver-*-instance`` command and return the gate report it answered with.

    A failure is reported as ``{"ok": False, "error": ...}`` **around** that report, so a caller
    reads one structure whether the work failed or the process did — which is what
    :func:`db_ops.backup_restore.server_metadata.summarize` and the restore's ``PHASE=`` line both
    expect, and they expect it of every phase alike.

    The subprocess itself is ``lib.common_cli.run_allowing_failure``. This module carried its own copy of that
    twenty lines until 2026-08-15 — the second copy in the tree of "spawn the `common` CLI and
    read JSON back" — and the two had already grown different answers for a command that printed
    nothing.

    **These commands answer in the response envelope**, so the gate report is one level down in
    ``data``. Unwrapped here rather than at each reader: `status`, `blockers` and
    `evidence_file` are what an incident review looks for, and they should not move house because
    the transport grew a wrapper.
    """
    try:
        success, report, error = common_cli.run_allowing_failure(
            command, request, timeout_seconds=_TIMEOUT_SECONDS)
    except common_cli.CommonCliError as exc:
        # The command could not run *at all* — no answer to unwrap. Shaped like a failed report
        # rather than raised, because this module's contract is that a metadata step never fails
        # the restore it runs beside.
        return {"ok": False, "operation": command, "error": str(exc)}
    report = dict(report)
    report["ok"] = success
    if error:
        report["error"] = error
    return report


def export_instance(request: dict[str, Any]) -> dict[str, Any]:
    """Export one instance's server-level metadata into a bundle directory."""
    return run_metadata_command("sqlserver-export-instance", request)


def replay_instance(request: dict[str, Any]) -> dict[str, Any]:
    """Replay one phase of a bundle onto a target instance."""
    return run_metadata_command("sqlserver-replay-instance", request)
