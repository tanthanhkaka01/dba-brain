"""Every ``db_ops.common.cli`` command takes one JSON object, and that is checked, not trusted.

The contract exists so that a config file, a Telegram action and a shell caller can pass the
same payload through untranslated — it is the shape ``data/*.json`` already has. A command
that takes flags instead cannot be driven from any of the three without someone writing a
translation layer, and a translation layer is where the arguments start to differ.

It was written down long before it was enforced, and by the 2026-08-06 audit six of the
twenty-two commands did not follow it. Prose does not hold a contract; this file does. A new
command is covered automatically — it is discovered from the dispatcher, so adding one
without the object form fails here rather than a year later.

Only the read side is exercised: each command must accept ``<json>``, ``@file`` and ``-``,
and must refuse a JSON array. What the command then *does* is its own module's tests — these
would otherwise need a live database, a host over SSH, or a Telegram token.
"""

from __future__ import annotations

import json
import re

import pytest

from db_ops.common import cli


#: Commands that mutate something real. They are asked for their parse behaviour only, with
#: a payload guaranteed to fail validation before anything is touched.
ALL_COMMANDS = [
    # `telegram-group` / `telegram-route` were removed from this list on 2026-08-15: both moved to
    # `db_ops.telegram.cli` when the routing settings went back to their owner, and neither name
    # has reached a handler here since. They still *passed* every case below — an unknown command
    # falls through to `config_admin.main`, whose JSON-request branch rejects the malformed payload
    # before argparse ever sees the name. A parametrization that passes for a command that does not
    # exist is worse than no coverage: it reports a contract being kept by nothing.
    "add-sql", "metric-toggle", "list-targets",
    "run-sql", "run-cmd", "rotate-password",
    "check-secret", "check-identifiers", "check-secret-literals",
    "lift-example", "probe-host", "self-status", "metric-severity", "trace-session",
    "inventory-summary", "restore-database", "list-backup-files",
    "pack-backup", "pull-file", "push-file",
    "restore-full", "restore-diff", "restore-log", "restore-key", "restore-metadata",
    "verify-restore",
    "fetch-file", "send-file", "pack-files", "relay-file",
    "host-facts", "host-service", "host-restart",
    "shrink-log", "kill-spid", "start-job", "disable-job",
    # The gate without an operation attached, for a caller whose work is not in `common`.
    "authorize",
    "sqlserver-precheck", "sqlserver-apply-cu", "sqlserver-verify-build",
    # Uncovered until the drift guard below learned to read `argv[0] in {...}`: these three and the
    # two delete commands were dispatched in that form and checked by nothing.
    "sqlserver-export-instance", "sqlserver-replay-instance", "sqlserver-verify-instance",
    "delete-file", "delete-files", "backup-database", "prune-backup-files",
    "list-databases", "list-schemas", "list-jobs", "create-table-from-xlsx",
    "copy-schema",
    # Left this file on 2026-08-15 and covered elsewhere now, for the same reason in each case —
    # they were not shared-layer work:
    #   check-credentials    -> db_ops/cli.py  (needs two apps' resolvers)
    #   queue-telegram-message, ops-status, restore-drill-status
    #                        -> db_ops/db/cli.py, see tests/test_db_cli_json_contract.py
    #                           (they open the runtime store, which ORD 01 owns)
    # Leaving them listed here would not have failed: an unknown command falls through to
    # `config_admin.main`, whose JSON branch rejects the payload before argparse sees the name.
]


def test_the_command_list_matches_the_dispatcher() -> None:
    """A command added to the CLI but not to this file would otherwise skip every check.

    Both dispatch forms are read. The guard used to see only ``if argv[0] == "x"`` and carried a
    hardcoded list for the one ``in {...}`` branch that existed — so every later command written
    that way was invisible to it, which is exactly how ``delete-file`` and ``delete-files`` were
    added and covered by nothing. A guard with a shape it cannot see is not a guard.
    """
    source = (cli.__file__ and open(cli.__file__, encoding="utf-8").read()) or ""
    dispatched: set[str] = set()
    # `== "x"` and `in {"a", "b"}` alike, and a set spanning several lines, because that is how the
    # dispatcher is actually written.
    for branch in re.finditer(r'if argv\[0\] (?:==|in) (\{[^}]*\}|"[a-z0-9-]+")', source):
        dispatched |= set(re.findall(r'"([a-z0-9-]+)"', branch.group(1)))
    missing = sorted(dispatched - set(ALL_COMMANDS))
    assert not missing, f"New common CLI command(s) not covered by the JSON contract test: {missing}"


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_a_json_array_is_refused_by_every_command(command: str, capsys) -> None:
    """An array root is a malformed request and must be named as one.

    ``telegram-route '[1,2]'`` used to resolve a notify level literally named ``[1,2]`` and
    answer ``alert: false`` — indistinguishable from the real answer for a level that is
    configured not to notify.
    """
    code = cli.main([command, "[1, 2]"])
    output = capsys.readouterr()
    combined = output.out + output.err
    assert code != 0 or "must be a JSON object" in combined, (
        f"{command} accepted a JSON array root: exit={code} output={combined[:200]!r}"
    )
    assert "must be a JSON object" in combined, (
        f"{command} refused an array without saying why: {combined[:200]!r}"
    )


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_malformed_json_is_reported_as_malformed_json(command: str, capsys) -> None:
    code = cli.main([command, "{not json"])
    combined = "".join(capsys.readouterr())
    assert code != 0
    assert "not valid JSON" in combined, (
        f"{command} did not report a JSON syntax error: {combined[:200]!r}"
    )


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_a_missing_request_file_is_reported_by_path(command: str, tmp_path, capsys) -> None:
    """``@file`` must be a real form, not something that falls through to inline parsing."""
    missing = tmp_path / "no_such_request.json"
    code = cli.main([command, f"@{missing}"])
    combined = "".join(capsys.readouterr())
    assert code == 2, f"{command} did not exit 2 for a missing @file (exit={code})"
    assert "Request file not found" in combined and missing.name in combined


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_stdin_is_read_for_the_dash_form(command: str, stdin_holding, capsys) -> None:
    """``-`` must read stdin. An array on stdin proves the payload was read from there."""
    stdin_holding(json.dumps([1, 2]))
    cli.main([command, "-"])
    combined = "".join(capsys.readouterr())
    assert "must be a JSON object" in combined, (
        f"{command} did not read its request from stdin: {combined[:200]!r}"
    )
