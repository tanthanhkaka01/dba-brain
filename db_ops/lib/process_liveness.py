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


def process_start_marker(pid: int) -> str | None:
    """When this PID's process started, as an opaque string - ``None`` when it cannot be read.

    A PID alone is not an identity. Once its process ends the number is handed to the next one -
    soon, in a container whose PIDs start low again after a restart - and the background-task
    poller then saw *another* process "still running", waited out the task's timeout, and killed
    whatever held the number (review 0.25.0, B1.6). The start time read at launch and again before
    trusting or killing the PID tells the two apart: the same PID with a different start time is a
    different process.
    """
    if sys.platform == "win32":
        return _windows_creation_time(pid)
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[-1].split()
    except (OSError, IndexError):
        return None
    # Field 22 of /proc/<pid>/stat is the start time in clock ticks since boot; the split above
    # starts at field 3 (state), so it is index 19.
    return fields[19] if len(fields) > 19 else None


def _windows_creation_time(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


#: Windows: a child started in a group of its own, so the whole tree can be stopped.
CREATE_NEW_PROCESS_GROUP = 0x00000200


def own_group_kwargs() -> dict:
    """``Popen`` arguments that start a child as the head of a process tree of its own.

    So that stopping it stops everything it started: on POSIX a session (its PID is the group id
    ``killpg`` takes), on Windows a new process group (``taskkill /T`` walks it).
    """
    if sys.platform == "win32":
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def child_start_marker(pid: int) -> str | None:
    """:func:`process_start_marker` of ``pid`` - but only when ``pid`` is a child of this process.

    Read right after a ``Popen`` and kept with it: it is what :func:`stop_process_tree` checks
    before it walks a tree. The parent check is what makes it safe to take: a PID that is not ours
    - a test's made-up number, a slot the OS has handed to someone else - gets no marker, and so
    can never be the root of a tree walk.
    """
    if _parent_pid(pid) != os.getpid():
        return None
    return process_start_marker(pid)


def stop_process_tree(pid: int, *, started: str | None, process=None,
                      grace_seconds: float = 5.0) -> None:
    """Stop ``pid`` and every process under it - a child started with :func:`own_group_kwargs`.

    Stopping the head alone left the rest running: the daemon's ``terminate()`` reached ``/bin/sh``
    or ``cmd.exe`` and the app under it kept working after its run was closed as a timeout, with
    its claim released for a duplicate (review 0.25.0, B2.3); the app's own ``common.cli`` child
    survived a killed parent the same way (F1.1); and the bot's poller killed a wrapper and not
    the command it ran (B1.6).

    ``started`` is the head's start marker, taken when it was launched (:func:`child_start_marker`,
    or the bot's recorded ``pid_started``). The tree is walked only while ``pid`` still carries it.
    A walk from a PID alone ends whoever holds that number now and everything under them - the
    first cut of this did exactly that from the test suite's fake processes. Without a match only
    ``process``, the ``Popen`` - which holds its own handle - is stopped, as it always was.

    POSIX: SIGTERM to the group, ``grace_seconds`` for it to go, then SIGKILL. Windows: every
    descendant from a process snapshot, terminated through the API - not ``taskkill``: ``lib``
    launches nothing. Never raises: a tree already gone is stopped.
    """
    ours = bool(started) and process_start_marker(pid) == str(started)
    if sys.platform == "win32":
        if ours:
            members = _windows_tree(pid)
            # The head through its Popen when there is one, so the handle it holds is the one used.
            for member in (members[1:] if process is not None else members):
                _windows_terminate(member)
        if process is not None:
            _stop_head(process, grace_seconds)
        return
    import signal

    if ours:
        _signal_group(pid, signal.SIGTERM)
    if process is not None:
        _stop_head(process, grace_seconds)
    if ours and _group_lingers(pid, grace_seconds):
        _signal_group(pid, signal.SIGKILL)


def stop_process_and_children(pid: int, *, started: str | None, grace_seconds: float = 5.0) -> bool:
    """Stop a process this one did **not** start, with everything under it. ``True`` when it is gone.

    For a run another scan of this node left past its timeout (0.26.0 §1.70): the process to stop is
    somebody else's child, so it is no group this process leads and :func:`stop_process_tree` cannot
    reach it. It is found the way that function's Windows half finds a tree - by who started whom -
    on both platforms.

    ``started`` is the start marker the run recorded when it claimed (``claim_started``). Without
    one, or with one the process holding ``pid`` no longer carries, **nothing is touched** and the
    answer is ``False``: a pid alone is whoever holds the number now. A process already gone is
    ``True``. This process and its own ancestors are never in the list.
    """
    if not is_pid_alive(pid):
        return True
    if not started or process_start_marker(pid) != str(started) or pid == os.getpid():
        return False
    members = _windows_tree(pid) if sys.platform == "win32" else _posix_tree(pid)
    if os.getpid() in members:
        # The overdue run is an ancestor of the process asking: stopping it would stop the asker.
        return False
    if sys.platform == "win32":
        for member in members:
            _windows_terminate(member)
        return not _lingers(members, grace_seconds)
    import signal

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for member in members:
            try:
                os.kill(member, sig)
            except OSError:
                pass
        if not _lingers(members, grace_seconds):
            return True
    return False


def _lingers(pids: list[int], grace_seconds: float) -> bool:
    """Is any of ``pids`` still running once the grace is spent?"""
    import time

    deadline = time.monotonic() + grace_seconds
    while True:
        if not any(is_pid_alive(pid) for pid in pids):
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.05)


def _posix_tree(pid: int) -> list[int]:
    """``pid`` and its descendants, head first, read from ``/proc``."""
    children: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return [pid]
    for name in names:
        if not name.isdigit():
            continue
        parent = _parent_pid(int(name))
        if parent is not None:
            children.setdefault(parent, []).append(int(name))
    tree, queue = [pid], [pid]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in tree:
                tree.append(child)
                queue.append(child)
    return tree


def _stop_head(process, grace_seconds: float) -> None:
    """``terminate``, then ``kill`` if it is still there after the grace. Never raises.

    A process stuck in uninterruptible I/O (a hung SMB/NFS mount) outlives even SIGKILL until the
    I/O returns; the caller must not wait on it, and ``subprocess`` reaps the zombie later.
    """
    for stop in (process.terminate, process.kill):
        try:
            stop()
        except OSError:
            pass
        try:
            process.wait(timeout=grace_seconds)
            return
        except Exception:  # noqa: BLE001 - TimeoutExpired; `lib` does not import subprocess.
            continue


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except OSError:
        pass


def _group_lingers(pid: int, grace_seconds: float) -> bool:
    import time

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except OSError:
            return False
        time.sleep(0.05)
    return True


def _parent_pid(pid: int) -> int | None:
    if sys.platform == "win32":
        return _windows_parents().get(pid)
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[-1].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError):
        return None


def _windows_parents() -> dict[int, int]:
    """Every process's parent PID, from one Toolhelp snapshot. ``{}`` when no snapshot is taken."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    parents: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def _windows_tree(pid: int) -> list[int]:
    """``pid`` and its descendants, head first.

    A process whose parent has exited keeps that parent's PID as its parent, and the number can
    since belong to someone else - so a process counts as a child only if it started after the
    parent it names.
    """
    children: dict[int, list[int]] = {}
    for child, parent in _windows_parents().items():
        children.setdefault(parent, []).append(child)
    tree, queue = [pid], [pid]
    while queue:
        parent = queue.pop()
        parent_started = _windows_creation_time(parent)
        for child in children.get(parent, []):
            started = _windows_creation_time(child)
            if child in tree or (parent_started and started and int(started) < int(parent_started)):
                continue
            tree.append(child)
            queue.append(child)
    return tree


def _windows_terminate(pid: int) -> None:
    import ctypes

    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)
