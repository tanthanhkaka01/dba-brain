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
        raise RuntimeError(f"JSON root must be an object: {path}")
    return data


def dump_json_text(data: dict[str, Any], *, indent: int = 4) -> str:
    """Serialise a config document the way ``data/*.json`` is written: 4-space, UTF-8, newline.

    ``ensure_ascii=False`` because these files carry Vietnamese host and service names, and
    escaping them to a backslash-u sequence makes a diff unreadable for the person reviewing a config change.
    """
    return json.dumps(data, ensure_ascii=False, indent=indent) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + replace).

    The replacement inherits the **original file's mode and owner**, and that is not cosmetic.
    These files live on a bind mount shared between the worker container and its host. The
    container runs as root; ``mkstemp`` creates 0600 and ``os.replace`` keeps the temp file's
    metadata, so a single ``/spbot_metric_toggle`` turned ``db_instances.json`` from
    ``tuser 0600`` into ``root 0600`` — and the master, which reads the worker over SFTP as
    ``tuser``, could no longer open it. ``merge_worker_config`` reported it as "not on worker"
    and the deploy's copy step then overwrote the operator's toggle with the master's file.
    The write succeeded, the change was real, and the next deploy silently destroyed it.
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
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
