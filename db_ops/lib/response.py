"""The one response shape every `common` CLI command returns.

`common` is a library whose callers are programs: the db_ops daemon, another app, a shell script,
a Telegram action. None of them should have to learn a different result shape per command, and
none of them should ever have to read stderr or an exit code to find out what happened - the exit
code says "did the process run", the response says "did the work succeed".

So every command prints exactly this, on stdout, whether it worked or not::

    {"success": true,
     "operation": "restore-full",
     "message": "Restored SALESDB_STG to 2026-08-07 01:40:00.",
     "error": null,
     "data": {...},
     "metrics": {"duration_ms": 41230, ...}}

The five keys are always present, even when empty. A caller writing ``result["data"]`` must not
have to guard for a key that some commands omit - an absent key and an empty one are the same
fact, and making them look different is how callers grow branches nobody tests.

* ``success`` - the work, not the process. ``false`` with a filled ``error`` is a normal outcome.
* ``operation`` - which command answered. Kept in the body so a stored or forwarded response is
  still self-describing after it has been separated from the command line that produced it.
* ``message`` - one line for a human. This is what a Telegram message or a log line quotes.
* ``error`` - ``null`` on success; the reason on failure, in the words the operator needs.
* ``data`` - the result proper. Shape is per command and documented there.
* ``metrics`` - numbers about the run: durations, counts, bytes. Separate from ``data`` because
  the caller charts these and rarely charts the result.

Secrets never appear in any of them; redact before building the response, not after.
"""

from __future__ import annotations

import json
from typing import Any


def ok(
    operation: str,
    *,
    message: str = "",
    data: Any = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A successful response. ``data`` defaults to an empty object, never to a missing key."""
    return {
        "success": True,
        "operation": str(operation),
        "message": str(message),
        "error": None,
        "data": {} if data is None else data,
        "metrics": dict(metrics or {}),
    }


def fail(
    operation: str,
    error: str,
    *,
    message: str = "",
    data: Any = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A failed response. Still a response - the caller reads it the same way it reads success."""
    text = str(error)
    return {
        "success": False,
        "operation": str(operation),
        # Defaulting the human line to the error saves every caller a `or` when it renders one.
        "message": str(message) if message else text,
        "error": text,
        "data": {} if data is None else data,
        "metrics": dict(metrics or {}),
    }


def exit_code(response: dict[str, Any]) -> int:
    """0 when the work succeeded, 1 when it did not.

    The exit code is a summary *of the response*, never a second opinion: a shell caller that only
    checks ``$?`` and a program that parses the JSON must never reach different conclusions.
    """
    return 0 if response.get("success") else 1


def emit(result: dict[str, Any]) -> int:
    """Print a response and return the exit code that matches it.

    Two lines, and they were written out five times — once in each ``common/cli_*.py`` facade —
    because two lines never look worth sharing. They are: the exit code is a *summary of the
    response*, never a second opinion, and a copy is where the two start disagreeing. The one
    place that must never drift is the place a shell caller checking ``$?`` and a program parsing
    the JSON both depend on.

    **Indented, because a person reads this too.** Four Telegram emergency commands render the
    raw stdout into a chat reply (``success_text: "{stdout}"``), and a ``host-restart`` gate report
    on one line is unreadable on a phone — which is what compact output did to them the moment the
    gate commands moved onto this function. Every consumer parses the whole stream with
    ``json.loads``, so the whitespace costs nothing; ``indent=1`` is what those commands and
    ``cli_catalog`` already printed before they shared this one.
    """
    print(json.dumps(result, ensure_ascii=False, default=str, indent=1))
    return exit_code(result)
