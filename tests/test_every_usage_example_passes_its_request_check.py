"""A command's own usage example is a request its reference accepts.

Since 0.27.0 a `common.cli` request with a value of the wrong kind is refused (rules R49), and a
refusal is only as right as the reference it checks against. Standing up the 0.27.0 soak node found
the first wrong one: `sql-target-add`'s usage shows the flat form `"logging_on_run": false,
"alert_on_error": true` - booleans, which is what its parser reads - and the reference described both
fields as objects, so the command refused the request its own help prints. The suite could not see
it: no test sends that form, and most app tests fake the transport.

So every command's usage is read here, every JSON example in it taken as a request (comments and
`...` placeholders as written), and held to the same check `common.cli` applies: a `value` finding is
the reference and the usage disagreeing, and one of the two is wrong.
"""

from __future__ import annotations

import contextlib
import io
import json
import re

import pytest

from db_ops.common import cli
from db_ops.lib import request_check
from db_ops.lib.paths import PACKAGED_CATALOGUE

REFERENCE = json.loads((PACKAGED_CATALOGUE / "shared_config_objects.json").read_text(encoding="utf-8-sig"))
COMMANDS = sorted({command for entry in REFERENCE["shared_config_objects"] if entry.get("kind") == "input"
                   for command in entry.get("commands") or []})


def _usage(command: str) -> str:
    text = io.StringIO()
    with contextlib.redirect_stdout(text), contextlib.redirect_stderr(io.StringIO()):
        try:
            cli.main([command, "--help"])
        except SystemExit:
            pass
    return text.getvalue()


def _examples(text: str) -> list[dict]:
    """Every JSON object the usage prints, `// comments` dropped."""
    found, lines, depth = [], [], 0
    for line in text.splitlines():
        code = re.sub(r"\s//.*$", "", line)
        if depth == 0 and not code.lstrip().startswith("{"):
            continue
        lines.append(code)
        depth += code.count("{") - code.count("}")
        if depth <= 0:
            try:
                example = json.loads("\n".join(lines))
            except ValueError:
                example = None
            if isinstance(example, dict):
                found.append(example)
            lines, depth = [], 0
    return found


def test_the_commands_are_read_from_the_reference():
    assert len(COMMANDS) > 60 and "sql-target-add" in COMMANDS


@pytest.mark.parametrize("command", COMMANDS)
def test_a_usage_example_is_a_request_its_reference_accepts(command):
    wrong = [f"{finding['field']}: {finding['detail']}"
             for example in _examples(_usage(command))
             for finding in request_check.check(command, example) if finding["kind"] == "value"]

    assert wrong == [], f"{command}'s usage and its reference disagree: {wrong}"


def test_sql_target_add_takes_the_flat_form_its_usage_shows():
    """The one found on 2026-10-06."""
    request = {"sql_id": 30, "server_id": "ACME-192-0-2-111", "logging_on_run": False, "alert_on_error": True}

    assert [f for f in request_check.check("sql-target-add", request) if f["kind"] == "value"] == []
