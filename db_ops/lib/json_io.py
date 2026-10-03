"""Reading and writing a ``data/*.json`` file the one way the whole tool does it.

This is a four-line function, which is exactly why it kept being retyped: by the
2026-08-06 documentation/boundary audit there were three byte-identical copies — in
``common/sql_execution.py``, ``jobs/daemon.py`` and ``metrics/definitions.py``. Small
does not mean harmless. Two details here are decisions, not defaults, and a fourth copy
written from memory would get them wrong:

* ``utf-8-sig`` — every ``data/*.json`` in this repo may carry a BOM, because the files
  are routinely edited from Windows tooling. Plain ``utf-8`` fails on the first byte with
  a ``json.decoder.JSONDecodeError`` that names column 1 and explains nothing.
* **The root must be an object.** Config files here are objects whose keys are read by
  name; a list root means the file was hand-edited into a different shape, and failing
  loudly at load beats every caller reading an empty result and reporting "0 targets".

The write side (:func:`atomic_write_text`, :func:`dump_json_text`) moved here from
``common/config_admin.py`` when the web console became a second writer of these files. It carries
a production lesson in its body — see the docstring — and a second copy written from memory would
not carry it. One reader and one writer, in the module named for the file format.
"""

from __future__ import annotations

from db_ops.lib import errors
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


def read_json_request(source: str) -> dict[str, Any]:
    """The one JSON-request reader: ``<json>`` inline, ``@path/to/file.json``, or ``-`` for stdin.

    Every command that takes "one JSON object in" needs these three forms and needs them to fail
    identically, and there were two readers of them — ``common/cli.py`` for the ``common`` family
    and nothing at all for the app CLIs, which is why ``telegram route @file.json`` reads its
    argument as a level *named* ``@file.json``. One function, in the module already named for
    reading this project's JSON.

    ``-`` decodes the **bytes** as UTF-8 rather than trusting ``sys.stdin``: its encoding follows
    the machine's ANSI code page on Windows and its error handler is ``surrogateescape``, so a byte
    it cannot decode becomes a lone surrogate that travels silently into a SQL statement and is
    refused by the driver several layers later.

    Raises ``FileNotFoundError`` for a missing ``@file`` and ``ValueError`` for anything that is
    not a JSON object, so a caller can answer the two differently — a missing file is the caller's
    typo, a bad payload is the request.
    """
    if source == "-":
        payload_text = sys.stdin.buffer.read().decode("utf-8-sig")
    elif source.startswith("@"):
        path = Path(source[1:])
        if not path.exists():
            raise FileNotFoundError(f"Request file not found: {path}")
        payload_text = path.read_text(encoding="utf-8-sig")
    else:
        payload_text = source
    try:
        request = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"request is not valid JSON: {exc}") from exc
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object.")
    return request



def read_json_request_answered(source: str, *, operation: str = "") -> tuple[dict[str, Any] | None, int]:
    """:func:`read_json_request`, with the answer a command gives to a request it cannot read.

    A missing ``@file`` is the caller's typo: stderr, exit 2. A payload that is not a JSON object
    is the request itself, and comes back as the JSON envelope every caller parses: exit 1. Here
    since 0.25.0 - it was ``common/cli.py``'s, and ``db.cli`` imported it from there, which R03
    forbids.
    """
    try:
        return read_json_request(source), 0
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return None, 2
    except ValueError as exc:
        # The envelope every other answer is (rules R15), with the kind that says whose mistake
        # it is. It printed `{"ok": false, "error": ...}` - a shape of its own, no `success` key,
        # read by a caller as "no answer" rather than as "your request is not an object".
        from db_ops.lib import errors, response

        response.emit(response.fail(operation, str(exc), kind=errors.KIND_REQUEST))
        return None, 1

def looks_like_json_request(argument: str) -> bool:
    """Is this CLI argument the JSON-request form (``<json>`` / ``@file`` / ``-``)?

    The commands that predate the "one JSON object in" rule still accept their old argument
    form, so both have to be recognised from the same string. No legacy form can begin with
    these characters — levels and data-dir paths are words, flags start with ``--``.

    ``[`` is deliberately in the set even though an array is never a valid request: it makes
    the array *reach the parser and get rejected*. Left out, ``telegram-route '[1,2]'`` was
    read as a notify level literally named ``[1,2]`` and answered ``alert: false`` — the same
    answer a correctly-configured silent level gives.
    """
    return argument[:1] in {"{", "[", "@"} or argument == "-"


def load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise errors.InvalidConfig(f"JSON root must be an object: {path}")
    return data


def dump_json_text(data: dict[str, Any], *, indent: int = 4) -> str:
    """Serialise a config document the way ``data/*.json`` is written: 4-space, UTF-8, newline.

    ``ensure_ascii=False`` because these files carry Vietnamese host and service names, and
    escaping them to a backslash-u sequence makes a diff unreadable for the person reviewing a config change.
    """
    return json.dumps(data, ensure_ascii=False, indent=indent) + "\n"


def indent_of(path: Path, default: int = 4) -> int:
    """The file's own indent, so a rewrite of one field is a one-field diff."""
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines()[1:]:
            stripped = line.lstrip(" ")
            if stripped and stripped != line:
                return len(line) - len(stripped)
    except OSError:
        pass
    return default


def _umask() -> int:
    """This process's umask, read without changing it.

    ``os.umask`` can only be read by setting it, and for the moment between the two calls every
    file another thread creates is world-writable. Linux states it in ``/proc/self/status``; where
    that cannot be read, the ordinary 022.
    """
    try:
        with open("/proc/self/status", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    return 0o022


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + replace).

    The replacement inherits the **original file's mode and owner**, and that is not cosmetic.
    These files live on a bind mount shared between the worker container and its host. The
    container runs as root; ``mkstemp`` creates 0600 and ``os.replace`` keeps the temp file's
    metadata, so a single ``/spbot_metric_toggle`` turned ``db_instances.json`` from
    ``labuser 0600`` into ``root 0600`` — and the master, which reads the worker over SFTP as
    ``labuser``, could no longer open it. ``merge_worker_config`` reported it as "not on worker"
    and the deploy's copy step then overwrote the operator's toggle with the master's file.
    The write succeeded, the change was real, and the next deploy silently destroyed it.

    **A file that is new gets the mode a plain write would have given it** - by the umask, 0644 on
    an ordinary host - unless it is ``private``, which keeps ``mkstemp``'s 0600. The same 0600 was
    what every new file got, whatever it held: when the scaffold, the lab registry and the daemon's
    state file moved onto this writer to become atomic (review 0.25.0, B9.2), every file ``init``
    creates turned from 0644 into 0600 with them - a node whose daemon runs as another user than
    the one who ran ``init`` could not read its own configuration. Private is a statement about
    what a file holds, made by the caller that knows (:func:`db_ops.lib.secret_text.write_secret_file`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        original = path.stat()
    except OSError:
        original = None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if original is not None:
            os.chmod(tmp_name, stat.S_IMODE(original.st_mode))
            # Only root can give a file away, which is exactly the case that breaks things;
            # everywhere else (a non-root worker, Windows) chown is unavailable or a no-op, so
            # a failure here must not cost the write.
            try:
                os.chown(tmp_name, original.st_uid, original.st_gid)
            except (AttributeError, OSError):
                pass
        elif not private and os.name != "nt":
            os.chmod(tmp_name, 0o666 & ~_umask())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
