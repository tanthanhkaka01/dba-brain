"""A call to ``common.cli`` or ``db.cli``: the command built, and the answer read. Both pure.

An app does not import ``common``; it hands ``common`` a JSON object and reads a JSON object back.
**Starting the process is ``transport``'s** (docs/15_transport.md): ``lib`` builds the command,
``transport`` runs it, ``lib`` reads the answer. Until 0.24.0 this module also started the process,
which put an operation inside the layer everything imports (rules R07). What stayed here is every
decision about the call - and each of them was a finding before it was a rule:

* **The payload goes in on stdin**, never argv. These requests carry resolved passwords, and argv
  is readable by anyone who can run ``ps`` on the machine (R14).
* **No deadline by default.** A restore or a backup of a large database legitimately runs for
  hours. The app command that schedules the work carries the window and the daemon kills the
  parent — one deadline, at the level that knows the number. ``timeout_seconds`` exists for the
  caller that genuinely knows its own: the instance-metadata replay caps itself at 30 minutes.
* **stderr is captured unless the caller streams it.** Most commands answer in seconds and their
  stderr belongs in an error message. Building a lab database takes minutes, and a person watching
  ``sre.cli create-db-docker`` (or the Telegram chat relaying it) saw nothing until the end when
  that work moved into ``common`` (0.23.0). ``stream_stderr=True`` passes the child's progress
  straight through; stdout is still the answer.
* **Bytes, pinned to UTF-8, strict going out and forgiving coming back** - see :func:`decode`.
* **Two readers**, because callers genuinely differ: ``transport.common_cli.run`` raises when the
  command reports failure, ``run_allowing_failure`` hands the failure back as data.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

from db_ops.lib import errors


class CommonCliError(errors.DbOpsError, RuntimeError):
    """A ``common`` CLI command did not answer, or answered that the work failed.

    ``kind`` is the answer's ``error_kind`` (``lib.errors.KINDS``), so the app that called decides
    by it - ``exc.kind == "unreachable"`` - instead of matching the sentence. A command that gave no
    answer at all is ``internal``: nothing on the other side said what went wrong.
    """

    def __init__(self, message: str = "", *, kind: str = errors.KIND_FAILED) -> None:
        super().__init__(message)
        self.kind = kind if kind in errors.KINDS else errors.KIND_INTERNAL


class Answer(tuple):
    """``(success, data, error)`` - unpacked as three, as every caller does - and ``.kind``.

    A tuple of three rather than a fourth element, so the callers that unpack it are unchanged; the
    answer's ``error_kind`` rides along as an attribute for the ones that want it.
    """

    kind: str | None

    def __new__(cls, success: bool, data: dict[str, Any], error: str,
                kind: str | None = None) -> "Answer":
        answer = super().__new__(cls, (success, data, error))
        answer.kind = kind
        return answer


#: The dispatcher a command belongs to. ``db_ops.db.cli`` owns the commands that open the runtime
#: store (ORD 01); everything else is ``common``.
DEFAULT_MODULE = "db_ops.common.cli"
DB_MODULE = "db_ops.db.cli"
#: The only two CLIs a command is built for (rules R38).
MODULES = (DEFAULT_MODULE, DB_MODULE)


@dataclass(frozen=True)
class CommandSpec:
    """Everything needed to start one call, and nothing that starts it."""

    command: str
    executable: str
    args: tuple[str, ...]
    stdin: bytes
    timeout_seconds: int | None = None
    stream_stderr: bool = False

    @property
    def argv(self) -> list[str]:
        return [self.executable, *self.args]


def build_command(command: str, request: dict[str, Any], *, module: str = DEFAULT_MODULE,
                  timeout_seconds: int | None = None, stream_stderr: bool = False) -> CommandSpec:
    """The call ``python -m <module> <command> -`` with ``request`` on stdin.

    The request is encoded strictly: a payload that cannot be encoded is a caller's bug and must not
    be delivered with a character silently swapped.
    """
    if module not in MODULES:
        raise ValueError(f"{module!r} is not a CLI a command is built for; expected one of {MODULES}.")
    payload = json.dumps(request, ensure_ascii=False, default=str).encode("utf-8")
    return CommandSpec(command=command, executable=sys.executable, args=("-m", module, command, "-"),
                       stdin=payload, timeout_seconds=timeout_seconds, stream_stderr=stream_stderr)


def decode(raw: bytes | None) -> str:
    """What came back, as text: UTF-8, with any byte that is not replaced rather than raised.

    The child's stdout is not only the JSON answer - a native tool it shells out to writes there
    too, in whatever code page the machine has. One cp1252 byte (0x97, an em dash from a Windows
    console) used to kill the reader and the answer never arrived: the gate had run, exited 0 and
    printed valid JSON, and the caller was told "authorize exited 0 without a JSON response". A
    replaced byte costs one character of an error message; a raised decode costs the answer.

    The pipe carries bytes for the same reason in the other direction: ``text=True`` encodes through
    the machine's ANSI code page on Windows, so one program talking to itself depended on the
    console it happened to be started from (an em dash in a task's SQL once arrived as the lone
    surrogate U+DC97, reported at a position the script did not have).
    """
    return (raw or b"").decode("utf-8", errors="replace")


def _envelope(text: str) -> Any:
    """The JSON object the command printed - the whole of stdout, or the last object in it.

    The envelope is printed last, but not always alone: a native tool the command shells out to
    writes to the same stdout, and one line ahead of the JSON made the whole answer unreadable - a
    command that had answered was recorded as one that had not (review 0.25.0, F1.2). So when the
    whole text is not JSON, the last object that starts a line and runs to the end is taken. ``None``
    when there is none.
    """
    try:
        return json.loads(text)
    except ValueError:
        pass
    starts = [0] + [index + 1 for index, char in enumerate(text) if char == "\n"]
    for start in reversed(starts):
        if text[start:start + 1] != "{":
            continue
        try:
            return json.loads(text[start:])
        except ValueError:
            continue
    return None


def read_answer(command: str, *, returncode: int | None, stdout: str,
                stderr: str) -> tuple[bool, dict[str, Any], str]:
    """``(success, data, error)`` from the envelope a command printed.

    A command that printed no JSON at all raises :class:`CommonCliError`: that is not a failed
    command, it is no answer, and the two must not be recorded as the same thing.
    """
    text = (stdout or "").strip()
    answer = _envelope(text)
    if not isinstance(answer, dict):
        detail = (stderr or text or "").strip()[:400]
        raise CommonCliError(f"{command} exited {returncode} without a JSON response: {detail}",
                             kind=errors.KIND_INTERNAL) from None
    data = answer.get("data")
    success = bool(answer.get("success"))
    # An answer from before `error_kind` existed (0.26.0) carries none; its failure is `failed`.
    kind = None if success else str(answer.get("error_kind") or errors.KIND_FAILED)
    return Answer(success, data if isinstance(data, dict) else {}, str(answer.get("error") or ""), kind)


def data_or_raise(command: str, answer: tuple[bool, dict[str, Any], str]) -> dict[str, Any]:
    """The answer's ``data``, or :class:`CommonCliError` when the command reported failure.

    The unwrapping is what keeps callers unchanged: the CLI wraps the very dict the in-process
    function used to return in ``data``, so a caller sees exactly what it saw before.
    """
    success, data, error = answer
    if not success:
        raise CommonCliError(f"{command} failed: {error or 'no reason given'}",
                             kind=getattr(answer, "kind", None) or errors.KIND_FAILED)
    return data


@dataclass(frozen=True)
class Invocation:
    """A configured command line that runs ``python -m db_ops.common.cli <command> <json>``."""

    command: str
    request: dict[str, Any]
    #: Where the JSON sits in the argv, so a caller can put ``-`` there and send the request on
    #: stdin instead - a request that carries a password must never be an argument.
    request_index: int


def common_invocation(argv: list[str]) -> Invocation | None:
    """What a configured argv asks ``common.cli`` for, or ``None`` when it runs something else.

    The bot's actions are data: an argv with the request rendered into one argument. Since 0.24.0
    ``common.cli`` reads no configuration (rules R09), so the app has to finish such a request
    before it runs - which needs the command and the request out of the argv, read here once.
    A request argument that is ``-`` or ``@file``, or not a JSON object, is not one to finish.
    """
    parts = [str(part) for part in argv]
    for index in range(len(parts) - 2):
        if parts[index] == "-m" and parts[index + 1] == DEFAULT_MODULE:
            command_index, request_index = index + 2, index + 3
            if request_index >= len(parts):
                return None
            try:
                request = json.loads(parts[request_index])
            except ValueError:
                return None
            if not isinstance(request, dict):
                return None
            return Invocation(parts[command_index], request, request_index)
    return None
