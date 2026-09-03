"""The three store commands keep the JSON-object contract after leaving ``common``.

``queue-telegram-message``, ``ops-status`` and ``restore-drill-status`` moved from
``db_ops.common.cli`` to ``db_ops.db.cli`` on 2026-08-15, because they open the runtime store and
`common` writes to no database. The contract they were held to did not move with them: they stayed
listed in ``tests/test_common_cli_json_contract.py``, where they went on *passing* — an unknown
command falls through to ``config_admin.main``, whose JSON branch rejects a malformed payload
before argparse ever sees the name. Three commands, checked by nothing, reported as checked.

That is the same vacuous pass ``telegram-group`` and ``telegram-route`` sat in for months, found in
the same audit. Moving a command means moving its guard in the same commit; this file is the guard.

Only the read side is exercised — each command must accept ``<json>``, ``@file`` and ``-``, and
must refuse an array root. What each then *does* needs a live store and is tested in
``test_ops_status.py`` and ``test_store_declaration.py``.
"""

from __future__ import annotations

import json

import pytest

from db_ops.db import cli


#: The JSON-object commands `db.cli` dispatches by bare word, before argparse. Its other commands
#: (``store-info``, ``migrate-sqlite-to-postgres``, …) are argparse subcommands with flags and are
#: deliberately not in scope: they provision and migrate a store, they are not the tool's API.
STORE_COMMANDS = ["queue-telegram-message", "ops-status", "restore-drill-status",
                  "sql-run-history", "sync-config", "config-items", "export-config", "run-app"]


def test_the_command_list_matches_the_dispatcher() -> None:
    """A new JSON command added to `db.cli` must not skip every check below.

    Read from ``_JSON_COMMANDS`` itself, not from a regex over the source. The regex form
    (``argv[0] == "x"``) stopped matching the moment the dispatcher was rewritten to find the
    command past the daemon's injected flags — and a guard that matches nothing passes, which is
    the third time in this file's history that a check has quietly become a no-op.
    """
    missing = sorted(set(cli._JSON_COMMANDS) - set(STORE_COMMANDS))
    assert not missing, f"New db.cli JSON command(s) not covered by this contract test: {missing}"
    stale = sorted(set(STORE_COMMANDS) - set(cli._JSON_COMMANDS))
    assert not stale, f"This file lists commands the dispatcher no longer has: {stale}"


@pytest.mark.parametrize("command", STORE_COMMANDS)
def test_a_json_array_is_refused(command: str, capsys) -> None:
    cli.main([command, "[1, 2]"])
    combined = "".join(capsys.readouterr())
    assert "must be a JSON object" in combined, (
        f"{command} accepted a JSON array root: {combined[:200]!r}"
    )


@pytest.mark.parametrize("command", STORE_COMMANDS)
def test_malformed_json_is_reported_as_malformed_json(command: str, capsys) -> None:
    code = cli.main([command, "{not json"])
    combined = "".join(capsys.readouterr())
    assert code != 0
    assert "not valid JSON" in combined, (
        f"{command} did not report a JSON syntax error: {combined[:200]!r}"
    )


@pytest.mark.parametrize("command", STORE_COMMANDS)
def test_a_missing_request_file_is_reported_by_path(command: str, tmp_path, capsys) -> None:
    missing = tmp_path / "no_such_request.json"
    code = cli.main([command, f"@{missing}"])
    combined = "".join(capsys.readouterr())
    assert code == 2, f"{command} did not exit 2 for a missing @file (exit={code})"
    assert "Request file not found" in combined and missing.name in combined


@pytest.mark.parametrize("command", STORE_COMMANDS)
def test_stdin_is_read_for_the_dash_form(command: str, stdin_holding, capsys) -> None:
    """``-`` must read stdin. An array on stdin proves the payload was read from there."""
    stdin_holding(json.dumps([1, 2]))
    cli.main([command, "-"])
    combined = "".join(capsys.readouterr())
    assert "must be a JSON object" in combined, (
        f"{command} did not read its request from stdin: {combined[:200]!r}"
    )


#: The exact shape the daemon builds: it inserts the forwarded passphrase immediately after
#: `python -m db_ops.db.cli`, so the subcommand is NOT argv[0].
DAEMON_PREFIX = ["--key-base64", "Zm9yd2FyZGVkLWJ5LXRoZS1kYWVtb24="]


@pytest.mark.parametrize("command", STORE_COMMANDS)
def test_the_command_is_found_when_the_daemon_injects_the_key_first(command: str) -> None:
    """The daemon puts ``--key-base64 <value>`` *before* the subcommand, and that broke production.

    ``jobs/daemon.py::forwarded_key_insert_index`` inserts at token 3 — straight after
    ``python -m db_ops.db.cli`` — for any child CLI whose source declares ``add_key_argument``.
    These three commands were key-unaware while they lived in ``common/cli.py`` (the key reached
    them through ``DB_OPS_SECRET_KEY``), so argv[0] was always the command name and the dispatcher
    could just read it. Moving them into this key-aware module changed that silently: argparse
    received the passphrase as the subcommand and exited 2, every 30 seconds, 529 times, and
    ``ops-status`` — the command whose job is to notice failures — was the thing failing.

    Nothing in the suite covered it because the argv shape only exists when the daemon builds it.
    This test builds it.
    """
    # The payload stays in `rest`: the flags are stripped of the command name, nothing else.
    assert cli._split_json_command(DAEMON_PREFIX + [command, "{}"]) == (
        command, DAEMON_PREFIX + ["{}"])


@pytest.mark.parametrize("command", STORE_COMMANDS)
def test_the_command_is_found_with_no_flags_at_all(command: str) -> None:
    assert cli._split_json_command([command, "{}"]) == (command, ["{}"])


def test_an_argparse_subcommand_is_not_swallowed() -> None:
    """The store's own argparse commands must still reach argparse, flags or not."""
    assert cli._split_json_command(["store-info"]) is None
    assert cli._split_json_command(["--config", "config.json", "check"]) is None
    assert cli._split_json_command([]) is None


# --------------------------------------------------------------------------- #
# The output half — added 2026-08-16 with the same guard over `common.cli`
# --------------------------------------------------------------------------- #
#
# `docs/13_common.md` states the response contract for the whole tool, not for one dispatcher, so
# the three commands that moved here are held to it here. All three answer in an ad-hoc
# `{"ok": …}` today and are listed as a **shrinking baseline**, exactly like
# `NOT_YET_ENVELOPE` in `tests/test_common_cli_response_shape.py`: the test fails if the set grows
# *and* if an entry has been fixed.
#
# `queue-telegram-message` converted on 2026-08-16, with its one client — `db/queue_message.py`,
# which reads the id out of `data` now and still accepts the old top-level key, because a worker
# one deploy behind must not silently stop returning ids. (An earlier note here said the client was
# byte-identical across six apps: that was true of an older tree and stale by the time it was
# written — the six collapsed into `db/queue_message.py` before this file existed.)
#
# `ops-status` and `restore-drill-status` followed, and the baseline is empty. Both answer
# `success: true` for *the check ran*: an app that is failing is exactly what `ops-status` exists
# to report, and a database whose restore drill is overdue is a fact about the estate. Answering
# `success: false` for either would make "db_ops noticed a problem" and "db_ops could not look"
# the same answer — for the two commands whose whole job is that difference. Their exit codes
# still say 1, because a runbook reads `$?`.

ENVELOPE_KEYS = frozenset({"success", "operation", "message", "error", "data", "metrics"})

#: An empty object: valid JSON that satisfies no command's own validation, so each answers from
#: its own path without a store or a key. `ops-status` and `restore-drill-status` fail on the
#: store connection instead, which is the same thing for shape purposes and needs nothing live.
NOT_YET_ENVELOPE: frozenset[str] = frozenset()


def _answer(command: str, capsys) -> object:
    cli.main([command, "{}"])
    text = capsys.readouterr().out.strip()
    # `restore-drill-status` prints a `[db_ops.config]` banner before its JSON; take the object.
    brace = text.find("{")
    try:
        return json.loads(text[brace:]) if brace >= 0 else None
    except ValueError:
        return None


@pytest.mark.parametrize("command", [c for c in STORE_COMMANDS if c not in NOT_YET_ENVELOPE])
def test_a_store_command_answers_in_the_response_envelope(command: str, capsys) -> None:
    parsed = _answer(command, capsys)
    assert isinstance(parsed, dict), f"{command} printed no JSON object."
    missing = sorted(ENVELOPE_KEYS - set(parsed))
    assert not missing, (
        f"{command} is missing {missing}. Build the answer with db_ops.lib.response.ok()/fail(). "
        "Do not add it to NOT_YET_ENVELOPE: that list only shrinks."
    )


@pytest.mark.parametrize("command", sorted(NOT_YET_ENVELOPE))
def test_a_baseline_store_command_is_still_unconverted(command: str, capsys) -> None:
    parsed = _answer(command, capsys)
    assert not (isinstance(parsed, dict) and ENVELOPE_KEYS <= set(parsed)), (
        f"{command} now answers in the envelope — delete it from NOT_YET_ENVELOPE."
    )


def test_the_baseline_names_only_commands_that_exist() -> None:
    unknown = sorted(NOT_YET_ENVELOPE - set(STORE_COMMANDS))
    assert not unknown, f"NOT_YET_ENVELOPE names commands this dispatcher does not have: {unknown}"
