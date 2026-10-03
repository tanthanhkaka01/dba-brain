"""``common.cli`` and ``db.cli``, called: build (``lib``), start (:mod:`.process`), read (``lib``).

The names and behaviour are the ones ``lib.common_cli`` had until 0.24.0, so a caller changed one
import line. Two readers, because callers genuinely differ:

* :func:`run` raises when the command reports failure. Right wherever the failure is fatal to what
  the caller is doing - a restore step, a table load nobody can use half of.
* :func:`run_allowing_failure` hands it back as data. A **backup** that fails is a recorded outcome,
  not a stop: the app writes a ``job_runs`` row with the exit code, the stderr and the error text,
  and carries on - and the CLI does send them, answering ``success: false`` with ``data`` filled.

A command that could not run **at all** raises from both: that is not a failed backup, it is no
backup, and the two must not be recorded as the same thing.
"""

from __future__ import annotations

from typing import Any

from db_ops.lib.common_cli import (  # noqa: F401 - CommonCliError is part of this module's API
    DEFAULT_MODULE, CommonCliError, build_command, data_or_raise, read_answer)
from db_ops.transport.process import ProcessResult, execute


def spawn(command: str, request: dict[str, Any], *, module: str = DEFAULT_MODULE,
          timeout_seconds: int | None = None,
          stream_stderr: bool = False) -> tuple[ProcessResult | None, str]:
    """Start the command and return ``(result, "")``, or ``(None, why)`` when it could not run.

    The start without the reading, for a caller whose answer is not the response envelope.
    """
    result = execute(build_command(command, request, module=module,
                                   timeout_seconds=timeout_seconds, stream_stderr=stream_stderr))
    if result.error:
        return None, f"{command} {result.error}"
    return result, ""


def run_allowing_failure(command: str, request: dict[str, Any], *,
                         timeout_seconds: int | None = None,
                         stream_stderr: bool = False) -> tuple[bool, dict[str, Any], str]:
    """``(success, data, error)``; a failed command is data, a command that did not run raises."""
    result, error = spawn(command, request, timeout_seconds=timeout_seconds,
                          stream_stderr=stream_stderr)
    if result is None:
        raise CommonCliError(error, kind="internal")
    return read_answer(command, returncode=result.returncode, stdout=result.stdout,
                       stderr=result.stderr)


def run(command: str, request: dict[str, Any], *,
        timeout_seconds: int | None = None, stream_stderr: bool = False) -> dict[str, Any]:
    """The response's ``data``; a failed command raises :class:`CommonCliError`."""
    return data_or_raise(command, run_allowing_failure(
        command, request, timeout_seconds=timeout_seconds, stream_stderr=stream_stderr))
