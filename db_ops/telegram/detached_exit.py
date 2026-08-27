"""Run a command, wait for it, and write its exit code where the poller can read it.

A detached background command is deliberately **not** a child of the workflow that later reports
on it — that is what lets a two-hour SQL task outlive the Telegram poll cycle that started it.
The cost is that nothing can ask the operating system how it went: ``waitpid`` only works for
children on POSIX, and on Windows the PID is released the moment the process exits, so
``OpenProcess`` fails and there is no handle to read a code from.

Both platforms therefore need the process to record its own outcome. POSIX did this from the
start, with ``sh -c '<cmd>; printf %s $? > file'``. Windows did not, and
``_get_exit_code_windows`` filled the gap by returning ``1`` for "no handle" — a hardcoded
failure for a process that had, in every observed case, succeeded. On 2026-08-26 a
``/spbot_run_sql_task 24`` ran for 135 seconds, wrote ``sql_runs.status='done'``, delivered its
``.txt`` result to the chat, and was then reported as::

    ❌ SQL task #24 failed.
    Exit code: 1
    Error: CLI command failed; see runtime logs for details.

Nothing was wrong except the report. The estate never saw it because its worker is Linux.

**One wrapper for both platforms, in Python rather than in a shell.** The POSIX version had to
build a command line and quote it; doing the same for ``cmd.exe`` is the defect this repository
met twice in one day — ``APP-CONTROL`` broke on exactly that, because single quotes are POSIX
syntax and ``cmd`` hands them through literally. Argument lists passed to ``subprocess`` need no
quoting at all, so this module takes one and the question never arises.

The exit code is written **last**, after the child has finished, so a file that exists is a
finished process. A file that does not exist means "still running or died without recording" —
which the poller must treat as unknown, never as failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Separates this module's own arguments from the command it is asked to run. Everything after it
#: is passed through untouched, so a command containing ``--exit-code-path`` or any other flag of
#: ours cannot be misread as one.
ARGV_SEPARATOR = "--"

USAGE = (
    "usage: python -m db_ops.telegram.detached_exit <exit-code-path> -- <command> [args...]\n"
    "\n"
    "Runs the command, waits for it, and writes its exit code to <exit-code-path>.\n"
    "Exits with the command's own code, so a caller that *can* read it still gets the truth.\n"
)


def run(exit_code_path: str | Path, argv: list[str]) -> int:
    """Run *argv*, record its exit code at *exit_code_path*, and return that code."""
    try:
        completed = subprocess.run(argv, check=False)  # noqa: S603 - argv comes from the caller
        code = int(completed.returncode)
    except OSError as exc:
        # The command could not be started at all. That is a real failure and is recorded as one:
        # 127 is the shell's "command not found", which is the closest thing to a convention.
        print(f"detached_exit: could not start {argv[0]!r}: {exc}", file=sys.stderr)
        code = 127

    try:
        path = Path(exit_code_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(code), encoding="utf-8")
    except OSError as exc:
        # Losing the record is not worth losing the run over — the child has already done its
        # work. Said out loud, because the poller will now report the outcome as unknown.
        print(f"detached_exit: could not write {exit_code_path}: {exc}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    if ARGV_SEPARATOR not in arguments[1:]:
        print(f"detached_exit: missing {ARGV_SEPARATOR!r} separator.\n\n{USAGE}", file=sys.stderr)
        return 2
    exit_code_path = arguments[0]
    command = arguments[arguments.index(ARGV_SEPARATOR, 1) + 1:]
    if not command:
        print(f"detached_exit: no command after {ARGV_SEPARATOR!r}.\n\n{USAGE}", file=sys.stderr)
        return 2
    return run(exit_code_path, command)


if __name__ == "__main__":
    raise SystemExit(main())
