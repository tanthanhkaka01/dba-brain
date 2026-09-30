"""The one place a process is started to reach ``common.cli`` or ``db.cli`` (rules R40).

Everything about the call was decided before it arrived here - the argv, the stdin bytes, the
deadline, whether stderr streams (``lib.common_cli.CommandSpec``) - and everything about the answer
is decided after it leaves (``lib.common_cli.read_answer``). What is left is the one thing that
cannot be pure.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from db_ops.lib.common_cli import CommandSpec, decode


@dataclass(frozen=True)
class ProcessResult:
    """What the process did. ``error`` is set, and ``returncode`` is None, when it could not start
    or ran past its deadline - a result the caller can say, not an exception it has to know the
    type of."""

    returncode: int | None
    stdout: str
    stderr: str
    error: str = ""


def execute(spec: CommandSpec) -> ProcessResult:
    try:
        completed = subprocess.run(
            spec.argv, input=spec.stdin, stdout=subprocess.PIPE,
            # None = inherited: the child's progress reaches this process's stderr as it is written.
            stderr=None if spec.stream_stderr else subprocess.PIPE,
            timeout=spec.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        # `run` has already killed the child. Said as what it is: a command that ran and was
        # stopped reads differently in an alert from one that never started.
        return ProcessResult(returncode=None, stdout="", stderr="",
                             error=f"ran past its deadline of {spec.timeout_seconds}s and was stopped")
    except (OSError, subprocess.SubprocessError) as exc:
        return ProcessResult(returncode=None, stdout="", stderr="", error=f"could not run: {exc}")
    return ProcessResult(returncode=completed.returncode, stdout=decode(completed.stdout),
                         stderr=decode(completed.stderr))
