"""The source side of a ``cat`` transfer, in a process of its own (0.37.0).

The operator, 2026-11-10: *the speeds are fine on their own - make Python use more than one core*.
Measured on the store's 6 GB backup: read alone 69 MB/s, written alone 43 MB/s, and the two together
in ONE process 16 MB/s - both SSH connections' packet handling is Python and takes turns on one
core (the GIL); reading ahead in a thread did not help. So the reading connection runs in a child
process with a core of its own (:func:`stream`, started by
:class:`dbabrain.common.filecopy.ProcessReader` through ``multiprocessing`` - a function of this
module, not a command: ``common`` launches no CLI), and hands the bytes over a pipe; the parent
hashes them and writes the target - or, for an encrypted copy's proof, decrypts them.

The pipe carries, in order: ``("SIZE", n)``, the file's bytes in chunks, ``b""`` at its end, then
``("END", failure)`` - ``""`` when the read ended well. A failure before the size is
``("ERROR", reason)``. The request (a password among it) travels over the same pipe the spawn
uses, never on a command line.
"""

from __future__ import annotations

from typing import Any


def stream(request: dict[str, Any], conn: Any) -> None:
    """Read one file over SSH (on the host, or inside a container) and send it to ``conn``."""
    from dbabrain.common import filecopy

    try:
        session = filecopy._ssh(request["source"], role="source")
    except Exception as exc:  # noqa: BLE001 - the parent reports it; never a file
        conn.send(("ERROR", f"{type(exc).__name__}: {exc}"[:1000]))
        conn.close()
        return
    try:
        try:
            reader = filecopy._ExecReader(session.client, str(request.get("container") or ""), str(request["path"]),
                                          stall=int(request.get("stall") or filecopy.STALL_SECONDS))
        except Exception as exc:  # noqa: BLE001 - the parent reports it
            conn.send(("ERROR", f"{type(exc).__name__}: {exc}"[:1000]))
            return
        conn.send(("SIZE", reader.size))
        failure = ""
        try:
            while True:
                chunk = reader.read(filecopy.CHUNK)
                conn.send_bytes(chunk)
                if not chunk:
                    break
            failure = reader.finish()
        except (BrokenPipeError, EOFError, OSError) as exc:
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            reader.close()
        try:
            conn.send(("END", failure[:1000]))
        except (BrokenPipeError, EOFError, OSError):
            pass  # the parent stopped reading: its own error says why
    finally:
        session.close()
        conn.close()
