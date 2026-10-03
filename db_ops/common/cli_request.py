"""How a ``common.cli`` command reads its request: the JSON object (inline, ``@file`` or ``-``), the legacy flag form, and the key flags.

Split out of ``common/cli.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``cli`` re-exports every
name, so no import changes.
"""

from __future__ import annotations

import json
import os
import sys
from contextvars import ContextVar

from db_ops.lib.json_io import looks_like_json_request

#: The command ``cli.main`` is answering. The reader needs it to find the request's reference entry
#: (rules R49), and every handler calls the reader with only its source and usage text - some forty
#: call sites, a dozen of them in modules handed the reader as a parameter - so the name travels
#: beside the call rather than through it. Empty outside ``main``.
COMMAND: ContextVar[str] = ContextVar("common_cli_command", default="")

#: Where a measured finding (an unknown key, a deprecated spelling) is appended, one JSON line
#: each, when set - see :mod:`db_ops.lib.request_check`.
MEASURE_ENV = "DB_OPS_REQUEST_CHECK_LOG"


def _optional_json_request(argv: list[str], usage: str) -> tuple[dict | None, int]:
    """The JSON-object contract for commands whose payload used to be a flag or a bare word.

    Returns ``({}, 0)`` for no argument at all, so the commands that legitimately take no
    input (``list-targets`` with no argument) still work bare.

    ``add-sql``, ``metric-toggle`` and ``list-targets`` predate the "one JSON object in" rule and
    were among the six exceptions the 2026-08-06 audit found (``check-credentials`` was a fourth
    until it moved to ``db_ops/cli.py``). They now take the object like every other
    command. Their old argument forms still parse — an operator's muscle memory and the
    examples already pasted into runbooks keep working — but the object is the contract, and
    it is the only form a config file or a Telegram action can carry unchanged.

    A leading ``{``, ``@`` or ``-`` is what marks the JSON form. None of the legacy forms can
    start with those characters (levels are words, flags start with ``--``), so the two are
    distinguishable without a mode flag.

    Four outcomes, all as ``(request, exit_code)``:

    * ``({}, 0)``     — no argument; the command runs with its defaults.
    * ``(dict, 0)``   — a JSON object was given.
    * ``(None, 0)``   — the legacy argument form; the caller parses ``argv`` itself.
    * ``(None, >0)``  — malformed JSON or a non-object root; already reported, just return it.
    """
    if not argv:
        return {}, 0
    if looks_like_json_request(argv[0]):
        return _read_json_request(argv[0], usage)
    return None, 0


def _read_json_request(source: str, usage: str) -> tuple[dict | None, int]:
    """Read a JSON object request from an inline string, ``@file`` or stdin (``-``).

    Shared by the JSON-request commands so ``run-sql``, ``queue-telegram-message`` and
    ``rotate-password`` all accept the same three forms and report a bad payload identically.
    """
    from db_ops.lib.json_io import read_json_request_answered

    # The three forms moved to `lib.json_io` on 2026-09-14, when the app CLIs needed the same
    # reader; the answer to a bad one followed in 0.25.0, when `db.cli` stopped importing this
    # module (R03). Same exit codes, same envelope, one place.
    request, code = read_json_request_answered(source)
    if request is None:
        return request, code
    return _checked(request)


def _checked(request: dict) -> tuple[dict | None, int]:
    """The request, or its refusal: held to its reference before the command reads it (R49)."""
    command = COMMAND.get()
    if not command:
        # Not called through `main`: code that built the request and reads it back is not a
        # caller to refuse, and has no command to look the reference up by.
        return request, 0
    from db_ops.lib import errors, request_check, response

    findings = request_check.check(command, request)
    message = request_check.refusal(findings) if request_check.REFUSING else ""
    # Until the check refuses, every finding is measured - the would-be refusals above all.
    _measure(command, request_check.measured(findings) if request_check.REFUSING else findings)
    if message:
        # The same envelope and exit code as a payload that is not an object: both are the
        # request's own mistake, and `error_kind` says so.
        response.emit(response.fail(command, message, kind=errors.KIND_REQUEST))
        return None, 1
    return request, 0


def _measure(command: str, findings: list[dict[str, str]]) -> None:
    path = os.environ.get(MEASURE_ENV, "").strip()
    if not path or not findings:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            for finding in findings:
                handle.write(json.dumps({"command": command, **finding}, ensure_ascii=False) + "\n")
    except OSError:
        pass  # measuring must never fail the command it measures


def _read_key_flags(rest: list[str], usage: str, command: str) -> tuple[str | None, str | None, int]:
    """``--key`` / ``--key-base64`` from the tail of an argv, for the JSON-request commands.

    Every command here that needs the passphrase had its own copy of this loop, and the one that
    forgot to write it (``instance-add``) accepted the flag and ignored it. Returning the exit
    code rather than raising keeps the callers shaped like the ones that already inline it.
    """
    key = key_base64 = None
    rest = list(rest)
    while rest:
        flag = rest.pop(0)
        value = rest.pop(0) if rest else ""
        if flag == "--key":
            key = value
        elif flag in {"--key-base64", "--key_base64"}:
            key_base64 = value
        else:
            print(f"Unknown {command} option: {flag}\n\n{usage}", file=sys.stderr)
            return None, None, 2
    return key, key_base64, 0


def _unreadable_config(config_path: str, exc: BaseException) -> str:
    return (f"--config {config_path} cannot be read ({exc}). Nothing was done: the data dir comes "
            "from the config named, never from a default.")
