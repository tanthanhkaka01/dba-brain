"""``check-credentials`` - does every configured target resolve to a real login.

A checker of the configuration, so it reads it (rules R09: a command whose subject *is* the
configuration reads it, and runs on an empty one). It was the one command the root ``db-ops``
answered itself until 0.24.0 (rules R41), because it asked two apps' resolvers - the metrics target
loader and the Telegram SQL-command credential - and ``common`` may import neither. Both are
``lib.data_sources``' now, and ``db-ops check-credentials`` is an alias of this command.
"""

from __future__ import annotations

import sys
from typing import Any

USAGE = (
    "usage: python -m db_ops.common.cli check-credentials [<json>|@<file>|-]\n"
    "       db-ops check-credentials [<json>]\n"
    "\n"
    "Verify every configured target resolves to a named credential, and that any secret its\n"
    "sql_access names is in the store. Exit 1 if any does not.\n"
    "\n"
    "Answers in the standard response envelope; the unresolvable targets are in data.problems.\n"
    'Pass {"format": "txt"} for the old plain-text listing (problems on stderr, summary on\n'
    "stdout), which is what pasted runbook lines expect.\n"
    '  {"data_dir": "data"}\n'
    "\n"
    "Fields:\n"
    "  data_dir   folder holding db_instances.json / users.json / sql_targets.json\n"
    "             (default: the folder data_sources resolves)\n"
    "\n"
    "The bare folder the root command took until 0.24.0 is refused with the JSON it became.\n"
)


def run(argv: list[str], *, read_request: Any = None) -> int:
    """Verify every configured target resolves to a named credential. Exit 1 if any does not.

    A legacy-Oracle target is checked too, and by what it actually needs: the credential its
    connect string is built from, plus the bridge's shared secret over ``api``. It used to be
    skipped here, which made the one command an operator runs to decide whether to look further
    report clean on a target that could not collect anything at all.

    A credential is required, never inferred (see
    :func:`db_ops.lib.data_sources.find_database_credential`), so a config edit that drops
    ``default_credential_name`` / ``credential_name`` now stops that target instead of quietly
    connecting as whatever entry happened to come first. Run this after editing
    ``db_instances.json`` / ``users.json`` / ``sql_targets.json`` and before a deploy.
    """
    from db_ops.common.cli import _optional_json_request

    if argv and argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    request, code = _optional_json_request(argv, USAGE)
    if request is None and code:
        return code
    if request is None:
        # The positional folder the root command took until 0.24.0, or a flag somebody expected this
        # command to accept - every other command takes `--key-base64`. A flag was once read as a
        # folder name, the check walked nothing and reported "0 without a resolvable credential",
        # which reads as a pass. **A verification command must never report success for having
        # looked at nothing** - so neither is guessed at: the request is a JSON object.
        given = argv[0]
        hint = ("It reads the secret store through the same resolution as everything else and "
                "needs no key." if given.startswith("-") else
                f'Give the folder in the request: check-credentials \'{{"data_dir": "{given}"}}\'.')
        print(f"check-credentials takes a JSON object, not {'a flag' if given.startswith('-') else 'a bare folder'}: "
              f"{given}\n{hint}\n\n{USAGE}", file=sys.stderr)
        return 2
    requested_dir = str(request.get("data_dir") or "")

    from db_ops.common import secret_check
    from db_ops.lib import response

    try:
        audit = secret_check.check_credentials(requested_dir)
    except FileNotFoundError as exc:
        print(f"check-credentials: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        return response.emit(response.fail("check-credentials", str(exc)))
    checked, problems = audit["checked"], audit["problems"]

    # The answer, in the one response shape (2026-08-16). This command used to put its *finding*
    # in the exit code and its detail on **stderr** — the precise split `lib/response.py`'s own
    # docstring forbids, and it made this one of two commands in the tree a program could not
    # consume at all. `success` is now "the check ran"; whether anything is wrong is
    # `data.problems`, which is a fact about the estate rather than about this process.
    summary = f"checked {checked} target(s); {len(problems)} without a resolvable credential"
    if str((request or {}).get("format") or "json").strip().lower() == "txt":
        for problem in problems:
            print(problem, file=sys.stderr)
        print(summary)
        return 1 if problems else 0
    response.emit(response.ok(
        "check-credentials", message=summary,
        data={"checked": checked, "problems": problems},
        metrics={"checked": checked, "problem_count": len(problems)},
    ))
    # The exit code still says "something is unresolvable", because a runbook and a scheduled
    # caller both check `$?` — and it agrees with the response rather than replacing it.
    return 1 if problems else 0
