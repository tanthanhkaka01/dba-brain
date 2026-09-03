"""The one client for the ``db_ops.common`` CLI.

An app does not import ``common``; it hands ``common`` a JSON object and reads a JSON object
back. This module is the transport for that, and there is exactly one copy of it.

**It lives in ``lib`` rather than in each app**, and the distinction from ``queue_message.py`` —
which *is* duplicated per app — is worth stating, because the first attempt at this file copied it
into five app folders on that precedent. ``queue_message.py`` has to be app-side: it falls back to
an in-process insert and therefore imports ``db_ops.db``, which an app may do and ``lib`` may not.
This module imports **nothing** from ``db_ops`` at all — the module it runs appears in an argv
list, as a string. Nothing forces it into five folders, and five copies of a transport is exactly
how six copies of ``queue_message.py`` once drifted into three behaviours.

Three details are decisions, not defaults:

* **The payload goes in on stdin**, never argv. These requests carry resolved passwords, and argv
  is readable by anyone who can run ``ps`` on the machine.
* **No deadline by default.** A restore or a backup of a large database legitimately runs for
  hours. The app command that schedules the work carries the window and the daemon kills the
  parent — one deadline, at the level that knows the number. ``timeout_seconds`` exists for the
  caller that genuinely knows its own: the instance-metadata replay caps itself at 30 minutes.
* **Two shapes, because callers genuinely differ.** :func:`run` raises when the command reports
  failure; :func:`run_allowing_failure` hands the failure back as data. Which one is right is a
  property of the work, not a preference — see :func:`run_allowing_failure`.

**Two readers, not three.** There was a ``run_ok`` here for the commands that answered
``{"ok": …}`` instead of the six-key envelope; it was written as transitional and it is gone —
every ``common`` and ``db`` command answers in the envelope now, so there is one shape to read.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any


class CommonCliError(RuntimeError):
    """A ``common`` CLI command did not answer, or answered that the work failed."""


def run(command: str, request: dict[str, Any], *,
        timeout_seconds: int | None = None) -> dict[str, Any]:
    """Run ``db_ops.common.cli <command>`` with ``request`` and return the response's ``data``.

    The unwrapping is what keeps callers unchanged: the CLI wraps the very dict the in-process
    function used to return in ``data``, so a caller sees exactly what it saw before.

    A failed command raises. That is right wherever the failure is fatal to what the caller is
    doing — a restore step, a table load nobody can use half of — because letting it flow back as
    data would make it indistinguishable from a command that ran and found nothing to do.
    """
    success, data, error = _call(command, request, timeout_seconds=timeout_seconds)
    if not success:
        raise CommonCliError(f"{command} failed: {error or 'no reason given'}")
    return data


def run_allowing_failure(command: str, request: dict[str, Any], *,
                         timeout_seconds: int | None = None) -> tuple[bool, dict[str, Any], str]:
    """Like :func:`run`, but a failed command comes back as data instead of an exception.

    A **backup** that fails is a recorded outcome, not a stop: the app writes a ``job_runs`` row
    with the exit code, the stderr and the error text, reports it, and carries on to the next job.
    Raising would throw away exactly the fields that row is made of — and the CLI does send them,
    answering ``success: false`` with the full result still in ``data``.

    Returns ``(success, data, error)``. A command that could not run **at all** still raises: that
    is not a failed backup, it is no backup, and the two must not be recorded as the same thing.
    """
    return _call(command, request, timeout_seconds=timeout_seconds)


#: The dispatcher a command belongs to. ``db_ops.db.cli`` owns the three that open the runtime
#: store (ORD 01); everything else is ``common``. A parameter rather than a second copy of this
#: function: ``db/queue_message.py`` had its own ``subprocess.run([... "db_ops.db.cli" ...])``,
#: which is the same twenty lines with one string changed — and "spawn a db_ops CLI and read JSON
#: back" existing twice is how the two grow different answers for a command that printed nothing.
DEFAULT_MODULE = "db_ops.common.cli"


def spawn(command: str, request: dict[str, Any], *, module: str = DEFAULT_MODULE,
          timeout_seconds: int | None = None):
    """Run the command with the request on stdin. Returns ``(completed, error_text)``.

    Public because ``db/queue_message.py`` needs the spawn without the reading: it falls back to
    an in-process insert when the subprocess cannot deliver, which is a policy this module cannot
    hold (``lib`` may not import ``db``).
    """
    payload = json.dumps(request, ensure_ascii=False, default=str)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", module, command, "-"],
            input=payload, capture_output=True, text=True, timeout=timeout_seconds,
            # **Pinned, and it was not.** `text=True` alone encodes through
            # `locale.getpreferredencoding()`, which on Windows is the machine's ANSI code page.
            # One program talking to itself over a pipe then depends on the console it happened to
            # be started from — and the two ends do not always agree, because the child's
            # `sys.stdin` is opened with `errors="surrogateescape"`.
            #
            # The failure that found it: a task's SQL held an em dash. The parent wrote it as
            # cp1252 0x97; the child read UTF-8 and recovered the undecodable byte as the lone
            # surrogate U+DC97, which pyodbc then refused to encode to UTF-16LE — reported as
            # position 350 of a script whose own bytes hold no 0x97 anywhere. It reproduced under
            # the daemon and never from an Administrator console, because those two had different
            # code pages, which is what "depends on the console" costs.
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{command} could not run: {exc}"
    return completed, ""



def _call(command: str, request: dict[str, Any], *,
          timeout_seconds: int | None = None) -> tuple[bool, dict[str, Any], str]:
    completed, error = spawn(command, request, timeout_seconds=timeout_seconds)
    if completed is None:
        raise CommonCliError(error)

    stdout = (completed.stdout or "").strip()
    try:
        answer = json.loads(stdout)
    except ValueError:
        detail = (completed.stderr or stdout or "").strip()[:400]
        raise CommonCliError(
            f"{command} exited {completed.returncode} without a JSON response: {detail}") from None

    data = answer.get("data")
    return (bool(answer.get("success")),
            data if isinstance(data, dict) else {},
            str(answer.get("error") or ""))
