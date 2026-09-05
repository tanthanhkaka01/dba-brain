"""Is this PID a *running* process — the question, and the two ways it is asked wrongly.

Lived in the Telegram app, where the background-task poller needed it. `self-status` needs it too,
to say whether the daemon whose start it reads about is still there, and `common` may not import an
app — so the rule moved here and the app re-exports it, the same shape as ``telegram_severity``.

Two traps, one per platform, and each cost a real incident:

* **POSIX**: ``os.kill(pid, 0)`` succeeds on a **zombie**. A detached CLI is started with
  ``start_new_session``, so when the process that spawned it exits the child is re-parented to
  PID 1 — inside the container that is the db_ops daemon, which does not reap orphans. The process
  has *exited* and still has a PID. Reading it as alive is what made a failed `create-db-docker`
  reply nothing for half an hour and then report a timeout instead of the real error.
* **Windows**: a failed ``OpenProcess`` means the process is gone, which is the right reading for
  *liveness* — it was only wrong as an *exit code*. Liveness and outcome are two questions and were
  once answered by one call.
"""

from __future__ import annotations

import os
import sys


def is_zombie(pid: int) -> bool:
    """A finished process whose parent has not reaped it."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[-1].split()
    except (OSError, IndexError):
        return False
    return bool(fields) and fields[0] == "Z"


def is_windows_pid_alive(pid: int) -> bool:
    """Is this PID a running process on Windows?"""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            # The handle opened but the state cannot be read. Treated as alive so a caller waits
            # rather than declaring an outcome it does not have.
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def is_pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return is_windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # The signal reached it, but a zombie is not running: it is an exit status nobody collected.
    # Reading /proc tells the difference; os.kill cannot.
    return not is_zombie(pid)
