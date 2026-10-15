"""What an app sends to `common.cli` is a request its reference accepts.

Since 0.27.0 a request carrying a value of the wrong kind is refused before anything runs (rules
R49), and a refusal is only as right as the reference it checks against. 0.27.0 went out with three
fields described wrongly, and each one refused work that had run for months:

- `run-sql`'s `prelude` was described as an array. The SQL task runner sends the `DECLARE` lines as
  text, so every SQL Server task with a parameter failed its first run on the upgraded worker.
- `authorize`'s `authorized_by` was described as text. A forced run over Telegram sends
  `{"channel": "telegram"}`: the operator's `yes` was collected and the run refused.
- The same field, on five more request shapes - the emergency operations and `run-cmd`. The bot's
  own `/spbot_kill_spid`, `/spbot_shrink_log`, `/spbot_start_job`, `/spbot_disable_job` and
  `/spbot_restart_server` write that object into every request, so each was refused. Nobody had
  needed one yet; it was found by reading the shipped commands against the check.

The suite could not see any of them, for one reason: **every test of a producer fakes the
transport**. The fake records the request and answers; nothing between the app and the fake asks
whether `common.cli` would have taken it. The usage-example guard
(`test_every_usage_example_passes_its_request_check.py`) reads what a command's help prints, which
is a third thing again.

So this file holds the producers themselves to the check `common.cli` applies:

- every request a shipped bot command writes - the parts the catalogue fixes, with what a person
  fills in set aside, because a placeholder has no type until somebody answers it;
- the `run-sql` request the SQL task runner builds, with and without parameters, on each engine;
- the `authorize` request a forced run builds;
- the request files the repository ships for an operator to pass as `@data/<file>.json`.

The last kind was added on 0.28.0's release day, when the showcase could not be refreshed:
`build-showcase`'s `extra_terms` was described as an array, `showcase.py` reads it as
`{term: kind}`, and `build-showcase @data/showcase.json` - the shipped example included - was
refused on 0.27.0 and the 0.28.0 candidate alike. A file the operator hands to a command is a
producer too; nothing had read one against the check.

A new producer of a `common.cli` request belongs here in the change that adds it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from dbabrain.lib import request_check
from dbabrain.lib.time_window import TimeWindow
from dbabrain.sql_tasks import runner

REPO_ROOT = Path(__file__).resolve().parents[1]
BOT_COMMANDS = REPO_ROOT / "dbabrain" / "telegram" / "catalogue" / "telegram_support_commands.json"

#: `{server_id}` - what the bot replaces with a person's answer before it runs the command.
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


# --------------------------------------------------------------------------- #
# The requests the shipped bot commands write
# --------------------------------------------------------------------------- #
def _parseable(template: str) -> str:
    """The template with every bare placeholder quoted, so it is JSON before anyone fills it in.

    `"spid":{spid}` is a number once the bot has substituted it and not JSON until then. Outside a
    string a `{` followed by a name and `}` can only be a placeholder - an object opens with `{"`
    or `{}` - so quoting those and nothing else leaves the rest of the request exactly as written.
    """
    out: list[str] = []
    in_string = False
    position = 0
    while position < len(template):
        character = template[position]
        if in_string:
            out.append(character)
            if character == "\\":
                out.append(template[position + 1])
                position += 2
                continue
            in_string = character != '"'
            position += 1
            continue
        if character == '"':
            in_string = True
        else:
            placeholder = _PLACEHOLDER.match(template, position)
            if placeholder:
                out.append(json.dumps(placeholder.group(0)))
                position = placeholder.end()
                continue
        out.append(character)
        position += 1
    return "".join(out)


def _what_the_catalogue_wrote(value: Any) -> Any:
    """The request without the values a person supplies: those have no type until answered."""
    if isinstance(value, dict):
        return {key: _what_the_catalogue_wrote(item) for key, item in value.items()
                if not (isinstance(item, str) and _PLACEHOLDER.search(item))}
    if isinstance(value, list):
        return [_what_the_catalogue_wrote(item) for item in value
                if not (isinstance(item, str) and _PLACEHOLDER.search(item))]
    return value


def _bot_requests() -> list[tuple[str, str, dict[str, Any]]]:
    """`(bot command, common.cli command, request)` for every shipped command that writes one."""
    document = json.loads(BOT_COMMANDS.read_text(encoding="utf-8-sig"))
    found = []
    for entry in document["telegram_support_commands"]:
        argv = (entry.get("action_config") or {}).get("command_argv") or []
        if "dbabrain.common.cli" not in argv:
            continue
        at = argv.index("dbabrain.common.cli")
        if len(argv) <= at + 2 or not str(argv[at + 2]).lstrip().startswith("{"):
            continue
        request = json.loads(_parseable(str(argv[at + 2])))
        found.append((str(entry.get("command_text")), str(argv[at + 1]),
                      _what_the_catalogue_wrote(request)))
    return found


def _wrong_values(command: str, request: dict[str, Any], **kwargs: Any) -> list[str]:
    return [f"{finding['field']}: {finding['detail']}"
            for finding in request_check.check(command, request, **kwargs)
            if finding["kind"] == "value"]


def test_the_sweep_finds_the_bot_commands_that_write_a_request() -> None:
    """A sweep that stopped matching would pass by checking nothing."""
    commands = {command for _, command, _ in _bot_requests()}

    assert {"kill-spid", "shrink-log", "start-job", "disable-job", "host-restart"} <= commands


def test_a_placeholder_is_set_aside_and_what_the_catalogue_wrote_is_kept() -> None:
    template = ('{"target":"{server_id}","spid":{spid},"reason":"telegram",'
                '"authorized_by":{"channel":"telegram"}}')

    request = _what_the_catalogue_wrote(json.loads(_parseable(template)))

    assert request == {"reason": "telegram", "authorized_by": {"channel": "telegram"}}


@pytest.mark.parametrize(("bot_command", "command", "request_written"), _bot_requests(),
                         ids=[bot_command for bot_command, _, _ in _bot_requests()])
def test_what_a_shipped_bot_command_writes_is_accepted(bot_command, command, request_written):
    wrong = _wrong_values(command, request_written)

    assert wrong == [], (
        f"/{bot_command} writes a `{command}` request that `common.cli` refuses (rules R49), so "
        f"the command cannot run from Telegram: {wrong}. The catalogue and the reference "
        "disagree, and one of the two is wrong."
    )


def test_the_check_would_have_caught_the_field_0_27_0_shipped_wrong() -> None:
    """The reference as 0.27.0 described `authorized_by`, held against today's `/spbot_kill_spid`.

    Pinned so this file cannot go quiet: if the sweep ever stops reaching that field, this fails
    where the parametrized test above would simply pass.
    """
    stale = json.loads(json.dumps(request_check.reference()))
    for entry in stale:
        if entry.get("kind") == "input" and "kill-spid" in (entry.get("commands") or []):
            for field in entry["fields"]:
                if field["field"] == "authorized_by":
                    field["type"] = "string"
                    field["constraint"] = {"kind": "string", "nullable": True, "allow_empty": True}
    written = next(request for _, command, request in _bot_requests() if command == "kill-spid")

    assert _wrong_values("kill-spid", written, entries=stale) == [
        "authorized_by: must be text; got dict"]


# --------------------------------------------------------------------------- #
# The requests the SQL task runner builds
# --------------------------------------------------------------------------- #
@pytest.fixture
def sent(monkeypatch):
    """Every request the runner hands to the transport, recorded where the process would start."""
    requests: list[tuple[str, dict[str, Any]]] = []

    def record(command, request, **_kwargs):
        requests.append((command, dict(request)))
        if command == "authorize":
            return True, {"gates": []}, ""
        return True, {"ok": True, "result_sets": [], "affected_rows": 0,
                      "result_sets_truncated": False}, ""

    monkeypatch.setattr(runner.common_cli, "run_allowing_failure", record)
    return requests


def _target(db_type: str) -> runner.SqlTarget:
    return runner.SqlTarget(
        sql_id=9, target_no=1, server_id="server", db_type=db_type, service_name="svc",
        instance_name="inst", credential_name="cred",
        time_window=TimeWindow(from_day=1, to_day=31, from_hour=0, to_hour=23, repeat_interval=60),
        active=True, database_name="Globex_Prod")


def _command(db_type: str, parameters: tuple[dict[str, Any], ...]) -> runner.SqlCommand:
    return runner.SqlCommand(
        sql_id=9, sql_code="TASK-009", sql_name="test", db_type=db_type, script_type="single",
        script_path="test.sql", script_paths=(), script_files=("test.sql",), active=True,
        autocommit=False, parameters=parameters)


@pytest.mark.parametrize(("db_type", "parameters", "values", "sql_text"), [
    pytest.param("sqlserver", (), None, "SELECT 1;", id="sqlserver, no parameters"),
    pytest.param("sqlserver", ({"name": "spid", "type": "int"},), {"spid": "55"},
                 "SELECT @spid;", id="sqlserver, a parameter - the prelude 0.27.0 refused"),
    pytest.param("postgresql", ({"name": "day", "type": "date"},), {"day": "2026-10-09"},
                 "SELECT :day;", id="postgresql, a named parameter"),
    pytest.param("oracle", ({"name": "day", "type": "date"},), {"day": "2026-10-09"},
                 "SELECT :day FROM dual", id="oracle, a named parameter"),
])
def test_the_run_sql_request_the_runner_builds_is_accepted(sent, db_type, parameters, values,
                                                         sql_text):
    runner.execute_on_target(
        command=_command(db_type, parameters), target=_target(db_type),
        database={"ip": "127.0.0.1", "port": 1433}, credential={"username": "user"},
        password="pass", sql_text=sql_text, parameter_values=values)

    command, request = sent[0]
    assert command == "run-sql"
    assert request_check.refusal(request_check.check(command, request)) == ""


def test_a_sql_server_parameter_travels_as_text_which_is_what_the_reference_says(sent):
    """The exact value 0.27.0 refused: `request.prelude: must be an array; got str`."""
    runner.execute_on_target(
        command=_command("sqlserver", ({"name": "spid", "type": "int"},)),
        target=_target("sqlserver"), database={"ip": "127.0.0.1", "port": 1433},
        credential={"username": "user"}, password="pass", sql_text="SELECT @spid;",
        parameter_values={"spid": "55"})

    _, request = sent[0]
    assert isinstance(request["prelude"], str) and request["prelude"].startswith("DECLARE @spid")
    assert _wrong_values("run-sql", request) == []


def test_the_authorize_request_a_forced_run_builds_is_accepted(sent, tmp_path):
    """The exact value 0.27.0 refused: `request.authorized_by: must be text; got dict`."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    delivery = {
        "output": {"format": "plain", "telegram_chat": "sql", "chat_id": ""},
        "notify": {"logging_on_run": {"enabled": False, "telegram_chat": "sql"},
                   "alert_on_error": {"enabled": False, "telegram_chat": "sql"}},
    }
    (data_dir / "sql_commands.json").write_text(json.dumps({"sql_commands": [{
        **delivery, "sql_id": 9, "sql_code": "PAYROLL", "sql_name": "recalculate today",
        "db_type": "sqlserver", "script_type": "single", "script_path": "sql/tasks/payroll.sql",
        "active": False}]}), encoding="utf-8")
    (data_dir / "sql_targets.json").write_text(json.dumps({"sql_targets": [{
        **delivery, "sql_id": 9, "target_no": 1, "server_id": "ACME-192-0-2-115",
        "db_type": "sqlserver", "service_name": "svc", "instance_name": "inst",
        "credential_name": "cred",
        "time_window": {"from_day": 1, "to_day": 1, "from_hour": 0, "to_hour": 0,
                        "repeat_interval": 999999},
        "active": True}]}), encoding="utf-8")

    runner.authorize_forced_run(sql_id=9, data_dir=data_dir, answer="yes", channel="telegram")

    command, request = sent[0]
    assert command == "authorize" and request["authorized_by"] == {"channel": "telegram"}
    assert request_check.refusal(request_check.check(command, request)) == ""


# --------------------------------------------------------------------------- #
# The request files the repository ships
# --------------------------------------------------------------------------- #
#: `common.cli <command> @<file>` - a request an operator passes whole, as the docs print it.
SHIPPED_REQUEST_FILES = {"build-showcase": REPO_ROOT / "data" / "showcase.example.json"}


@pytest.mark.parametrize("command, path", sorted(SHIPPED_REQUEST_FILES.items()))
def test_a_request_file_the_repository_ships_is_accepted(command, path):
    """`build-showcase @data/showcase.json` is the documented way to refresh the showcase; on
    0.27.0 the example it starts from was refused before anything was read."""
    request = json.loads(path.read_text(encoding="utf-8-sig"))
    assert _wrong_values(command, request) == []


def test_the_showcase_terms_are_read_as_term_to_kind() -> None:
    """`showcase.py` takes `extra_terms` as `{term: kind}` and iterates its items: an object is
    what the reference accepts, and a list - which the parser could not read - is refused."""
    assert _wrong_values("build-showcase", {"extra_terms": {"SITECODE": "database"}}) == []
    assert [finding.split(":")[0] for finding in
            _wrong_values("build-showcase", {"extra_terms": ["SITECODE"]})] == ["extra_terms"]
