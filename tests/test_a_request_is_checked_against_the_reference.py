"""A ``common.cli`` request is held to its reference before the command reads it (rules R49).

Until 0.26.0 the reference described every request field and nothing held a request to it: each
command checked what it remembered to, in words of its own. A wrong type surfaced three calls deep
as a ``TypeError``; a required field left out surfaced as whatever the command did without it. The
caller could not tell its own mistake from the command's, and ``error_kind`` said ``internal``.

The check refuses what the reference says is wrong - a value of the wrong kind or out of range, a
required field absent - with the field named and the kind ``request``. It does not refuse what the
reference only measures: an unknown key, a deprecated spelling, a blank optional field.

**It refuses only once ``request_check.REFUSING`` is on.** The first node to run it refused every
SQL task within a minute: the reference called ``run-sql``'s ``capture`` a boolean, and the runner
sends ``"all"``. The reference was wrong, and the suite could not see it - most app tests fake the
transport. Until a node's measurement of real requests is empty, every finding is measured and
nothing is refused; the refusal tests below switch it on.
"""

from __future__ import annotations

import json

import pytest

from db_ops.common import cli, cli_request
from db_ops.lib import request_check


@pytest.fixture
def refusing(monkeypatch):
    monkeypatch.setattr(request_check, "REFUSING", True)


def _answer(capsys, argv: list[str]) -> tuple[int, dict]:
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_a_value_of_the_wrong_kind_is_refused_naming_the_field(capsys, refusing) -> None:
    code, answer = _answer(capsys, ["ask", json.dumps({"prompt": "Go on?", "tries": "twice"})])

    assert code != 0 and answer["success"] is False
    assert answer["error_kind"] == "request"
    assert "request.tries" in answer["error"] and "whole number" in answer["error"]


def test_a_value_out_of_its_range_is_refused(capsys, refusing) -> None:
    code, answer = _answer(capsys, ["ask", json.dumps({"prompt": "Go on?", "tries": 0})])

    assert answer["error_kind"] == "request"
    assert "request.tries" in answer["error"] and ">= 1" in answer["error"]


def test_a_required_field_left_out_is_refused_as_missing(capsys, refusing) -> None:
    code, answer = _answer(capsys, ["ask", json.dumps({"choices": ["yes", "no"]})])

    assert answer["error_kind"] == "request"
    assert "request.prompt" in answer["error"] and "required" in answer["error"]


def test_a_legacy_spelling_is_still_read_and_not_refused() -> None:
    findings = request_check.check("run-sql", {"sql": "SELECT 1", "database": "master"})

    assert not request_check.refusal(findings)
    assert {f["field"] for f in request_check.measured(findings)} == {"sql", "database"}


def test_a_blank_optional_field_means_not_given() -> None:
    """``"database_name": target.db_name or ""`` is how code leaves a field out."""
    findings = request_check.check("run-sql", {"sql_text": "SELECT 1", "database_name": "", "sql_file": "  "})

    assert findings == []


def test_a_blank_required_field_is_still_refused() -> None:
    """No shipped entry has one today - ``ask``'s ``prompt`` may be blank - so the entry is made here."""
    entries = [{"object": "input_demo", "kind": "input", "commands": ["demo"],
                "fields": [{"field": "name", "required": True, "constraint": {"kind": "string"}}]}]

    assert request_check.refusal(request_check.check("demo", {"name": " "}, entries=entries))
    assert request_check.refusal(request_check.check("demo", {}, entries=entries))
    assert not request_check.refusal(request_check.check("demo", {"name": "x"}, entries=entries))


def test_an_unknown_key_is_measured_and_not_refused(capsys, tmp_path, monkeypatch) -> None:
    log = tmp_path / "request_check.jsonl"
    monkeypatch.setenv("DB_OPS_REQUEST_CHECK_LOG", str(log))
    request = {"prompt": "Go on?", "answer": "yes", "colour": "blue"}

    token = cli_request.COMMAND.set("ask")
    try:
        read, code = cli_request._read_json_request(json.dumps(request), "")
    finally:
        cli_request.COMMAND.reset(token)

    assert code == 0 and read == request
    measured = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(m["command"], m["kind"], m["field"]) for m in measured] == [("ask", "unknown", "colour")]


def test_a_secret_in_a_request_is_not_refused_as_it_would_be_in_a_config_file() -> None:
    """A request states its login (rules R09); the same password in data/*.json is refused."""
    findings = request_check.check("run-sql", {"sql_text": "SELECT 1",
                                               "connection": {"password": "stated-here"}})

    assert not request_check.refusal(findings)


def test_the_reader_called_outside_a_command_checks_nothing() -> None:
    """Code that builds a request and calls the reader itself is not a caller to refuse."""
    read, code = cli_request._read_json_request(json.dumps({"tries": "twice"}), "")

    assert code == 0 and read == {"tries": "twice"}


@pytest.mark.parametrize("entry", [e for e in request_check.reference() if e.get("kind") == "input"],
                         ids=lambda e: str(e["object"]))
def test_every_input_entry_checks_an_empty_request_without_raising(entry) -> None:
    """A reference entry the checker cannot read would refuse every call of its commands."""
    for command in entry.get("commands") or []:
        request_check.check(command, {})


def test_the_reference_is_the_packaged_copy_never_a_node_s_data() -> None:
    from db_ops.lib.paths import PACKAGED_CATALOGUE
    from db_ops.lib import shared_objects

    assert request_check.reference() == shared_objects.load(PACKAGED_CATALOGUE)


def test_until_it_refuses_a_wrong_value_is_measured_and_the_command_runs(capsys, tmp_path, monkeypatch) -> None:
    log = tmp_path / "request_check.jsonl"
    monkeypatch.setenv("DB_OPS_REQUEST_CHECK_LOG", str(log))
    request = {"prompt": "Go on?", "tries": "twice"}

    token = cli_request.COMMAND.set("ask")
    try:
        read, code = cli_request._read_json_request(json.dumps(request), "")
    finally:
        cli_request.COMMAND.reset(token)

    assert request_check.REFUSING is False
    assert code == 0 and read == request
    measured = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(m["kind"], m["field"]) for m in measured] == [("value", "tries")]


def test_what_the_sql_task_runner_sends_is_not_refused(refusing) -> None:
    """The request that refused every SQL task on the first node to run the check (2026-10-03)."""
    findings = request_check.check("run-sql", {"sql_text": "SELECT 1", "capture": "all",
                                               "commit": True, "database_name": "LABTEST"})

    assert not request_check.refusal(findings)
    assert request_check.refusal(request_check.check("run-sql", {"sql_text": "SELECT 1", "capture": "every"}))


@pytest.mark.parametrize("command,request_", [
    ("list-backup-files", {"db_type": "sqlserver", "path": "/var/opt/mssql/backup/import",
                           "target": {"host": "192.0.2.252", "port": 1433, "username": "sa",
                                      "password": "stated-here"}}),
    ("verify-restore", {"db_type": "postgresql", "database_names": ["labtest"],
                        "host": {"host": "192.0.2.252", "port": 22, "username": "root",
                                 "password": "stated-here", "runtime": "linux"}}),
], ids=["list-backup-files target", "verify-restore host"])
def test_what_the_restore_sends_is_not_refused(refusing, command, request_) -> None:
    """Measured on the node the next morning (2026-10-03): both fields arrive as objects, which
    their parsers read (``backupfiles.sqlserver``, ``hostcmd.parse_host``), while the reference
    called them text. Switched on, the check would have refused every scheduled restore."""
    findings = request_check.check(command, request_)

    assert [f for f in findings if f["kind"] == "value"] == []
