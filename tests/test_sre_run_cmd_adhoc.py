"""`sre run-cmd-adhoc` — one shell command on a host named by server_id, the login resolved by the app.

Restarting a lab database container needed a shell command on the lab VM. `common.cli run-cmd` runs
one command but takes the host login complete in its request and reads no configuration (rules R09);
`sre ssh` goes through a bastion that is not reachable from every node. The host's own `cmd_access`
(data/db_instances.json) is what the OS metrics already use. A script would be a missing command,
so this is the command — the host twin of `sql_tasks run-sql-adhoc`.

What is held here, with no database: the request is refused rather than half-read; sudo and format
are passed to the run-cmd request as they are, and invalid values are refused; a shell command
cannot be classified as read-only, so authorization is always required before anything runs; the
authorize gate's effects name the command text and mention sudo when requested; a refused
authorization executes nothing and answers refused; an authorized run spawns run-cmd once with
confirm and assume_yes true and the resolved access block; dry-run masks credentials and spawns
nothing; the password travels on stdin, never argv; the CLI path does not patch stdout;
--key-base64 reaches set_key_env; every shipped ladder prices run-cmd-adhoc at 50.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import types

import pytest

from dbabrain.lib import common_cli as lib_common_cli
from dbabrain.lib import errors
from dbabrain.lib.data_sources import request_fill
from dbabrain.sre import adhoc_cmd
from dbabrain.sre import cli as sre_cli
from dbabrain.transport import common_cli
from dbabrain.transport.process import ProcessResult

SERVER = "LAB-192-0-2-10-HOST"
PASSWORD = "s3cret-not-for-stdout"


@pytest.fixture
def calls(monkeypatch):
    """The transport faked: `authorize` answers as the test sets it, `run-cmd` answers an envelope."""
    seen: dict = {"spawn": [], "authorize": [], "authorized": True}

    def fill(command, request, **_kwargs):
        filled = dict(request)
        if command == "run-cmd":
            filled["access"] = {
                "method": "ssh",
                "host": "192.0.2.10",
                "port": 22,
                "username": "admin",
                "password": PASSWORD,
            }
        return filled

    def run_allowing_failure(command, request, **_kwargs):
        assert command == "authorize"
        seen["authorize"].append(request)
        if seen["authorized"]:
            return True, {"gates": [{"name": "confirm", "status": "OK"}]}, ""
        return False, {"gates": [{"name": "confirm", "status": "FAIL", "detail": "not confirmed"}]}, ""

    def spawn(command, request, **_kwargs):
        seen["spawn"].append((command, request))
        return ProcessResult(
            returncode=0,
            stderr="",
            stdout='{"success": true, "operation": "run-cmd"}\n',
        ), ""

    monkeypatch.setattr(request_fill, "fill_request", fill)
    monkeypatch.setattr(common_cli, "run_allowing_failure", run_allowing_failure)
    monkeypatch.setattr(common_cli, "spawn", spawn)
    return seen


def _run(capsys, request: dict, *, dry_run: bool = False, tmp_path=None) -> tuple[int, str, str]:
    code = adhoc_cmd.main(json.dumps(request), data_dir=tmp_path, dry_run=dry_run)
    out, err = capsys.readouterr()
    return code, out, err


# --------------------------------------------------------------------------- #
# The request: everything named, nothing guessed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("request_, reason", [
    ({"server_id": SERVER, "command": "uptime", "comit": True}, "unknown field"),
    ({"command": "uptime"}, "server_id is required"),
    ({"server_id": SERVER}, "exactly one of command and script"),
    ({"server_id": SERVER, "command": "uptime", "script": "uptime"}, "exactly one"),
    ({"server_id": SERVER, "command": "uptime", "assume_yes": "true"}, "true or false"),
    ({"server_id": SERVER, "command": "uptime", "timeout_seconds": -5}, "positive integer"),
    ({"server_id": SERVER, "command": "uptime", "confirm": 123}, '"yes" or true'),
])
def test_a_request_that_cannot_be_read_as_written_is_refused(request_, reason):
    with pytest.raises(errors.InvalidRequest, match=reason):
        adhoc_cmd.checked(request_)


def test_the_run_cmd_request_states_confirm_and_assume_yes_true():
    built = adhoc_cmd.run_cmd_request(adhoc_cmd.checked({
        "server_id": SERVER,
        "command": "systemctl restart mysql",
        "timeout_seconds": 30,
        "confirm": "yes",
        "reason": "maintenance",
        "assume_yes": True,
    }))

    assert built == {
        "target": SERVER,
        "command": "systemctl restart mysql",
        "timeout_seconds": 30,
        "confirm": True,
        "assume_yes": True,
    }


def test_a_multiline_script_is_passed_to_run_cmd_request():
    built = adhoc_cmd.run_cmd_request(adhoc_cmd.checked({
        "server_id": SERVER,
        "script": "echo start\necho finish\n",
    }))

    assert built == {
        "target": SERVER,
        "script": "echo start\necho finish\n",
        "confirm": True,
        "assume_yes": True,
    }


def test_sudo_and_format_are_passed_to_the_run_cmd_request_as_they_are():
    built = adhoc_cmd.run_cmd_request(adhoc_cmd.checked({
        "server_id": SERVER,
        "command": "systemctl restart mysql",
        "sudo": True,
        "format": "txt",
        "timeout_seconds": 45,
    }))

    assert built == {
        "target": SERVER,
        "command": "systemctl restart mysql",
        "timeout_seconds": 45,
        "format": "txt",
        "sudo": True,
        "confirm": True,
        "assume_yes": True,
    }


def test_a_non_boolean_sudo_and_a_non_string_format_are_refused():
    with pytest.raises(errors.InvalidRequest, match="true or false"):
        adhoc_cmd.checked({"server_id": SERVER, "command": "uptime", "sudo": "yes"})

    with pytest.raises(errors.InvalidRequest, match="format must be a string"):
        adhoc_cmd.checked({"server_id": SERVER, "command": "uptime", "format": 123})


# --------------------------------------------------------------------------- #
# Authorization, execution, and refusal
# --------------------------------------------------------------------------- #
def test_an_authorized_run_spawns_run_cmd_once_with_confirm_and_assume_yes_true_and_filled_access(
    calls, capsys, tmp_path
):
    code, out, _err = _run(
        capsys,
        {"server_id": SERVER, "command": "uptime", "confirm": "yes"},
        tmp_path=tmp_path,
    )

    assert code == 0
    assert out == '{"success": true, "operation": "run-cmd"}\n'
    assert len(calls["authorize"]) == 1
    [gate] = calls["authorize"]
    assert gate["operation"] == "run-cmd-adhoc"
    assert gate["target_id"] == SERVER
    assert gate["confirm"] == "yes"
    [(command, request)] = calls["spawn"]
    assert command == "run-cmd"
    assert request["confirm"] is True and request["assume_yes"] is True
    assert request["access"]["password"] == PASSWORD


def test_the_authorize_gate_effects_name_the_command_text_and_sudo_when_true(
    calls, capsys, tmp_path
):
    code, _out, _err = _run(
        capsys,
        {
            "server_id": SERVER,
            "command": "systemctl restart mysql",
            "sudo": True,
            "confirm": "yes",
        },
        tmp_path=tmp_path,
    )

    assert code == 0
    assert len(calls["authorize"]) == 1
    effects = calls["authorize"][0]["effects"]
    assert effects[0].endswith(", through sudo")
    assert "the command: systemctl restart mysql" in effects


@pytest.mark.parametrize("built, expected", [
    ({"target": SERVER, "access": {"host": "192.0.2.10", "container_name": "PG_LAB"}},
     "inside container PG_LAB on 192.0.2.10"),
    ({"target": SERVER, "runtime": "host",
      "access": {"host": "192.0.2.10", "container_name": "PG_LAB"}},
     "on the host 192.0.2.10"),
    ({"target": SERVER, "access": {"host": "192.0.2.10"}}, "on the host 192.0.2.10"),
])
def test_where_the_command_lands_follows_the_container_unless_runtime_says_host(built, expected):
    # On the lab a `docker restart` answered `docker: not found` as root: the instance's
    # container_name had put the command inside the very container it meant to restart.
    assert adhoc_cmd.lands_in(built) == expected


def test_the_question_and_the_rehearsal_say_where_the_command_runs(calls, capsys, monkeypatch, tmp_path):
    def fill(command, request, **_kwargs):
        filled = dict(request)
        if command == "run-cmd":
            filled["access"] = {"host": "192.0.2.10", "container_name": "PG_LAB", "password": PASSWORD}
        return filled

    monkeypatch.setattr(request_fill, "fill_request", fill)
    _code, out, _err = _run(capsys, {"server_id": SERVER, "command": "id"}, dry_run=True,
                            tmp_path=tmp_path)
    assert json.loads(out)["runs"] == "inside container PG_LAB on 192.0.2.10"

    _run(capsys, {"server_id": SERVER, "command": "id", "runtime": "host", "confirm": "yes"},
         tmp_path=tmp_path)
    [gate] = calls["authorize"]
    assert "it runs on the host 192.0.2.10" in gate["effects"]
    [(_command, request)] = calls["spawn"]
    assert request["runtime"] == "host"


def test_a_script_is_named_by_its_first_line_in_the_question(calls, capsys, tmp_path):
    _run(capsys, {"server_id": SERVER, "script": "set -e\ndocker ps\n", "confirm": "yes"},
         tmp_path=tmp_path)

    assert "the script (2 line(s)): set -e" in calls["authorize"][0]["effects"]


def test_a_refused_authorization_runs_nothing_and_answers_refused(calls, capsys, tmp_path):
    calls["authorized"] = False

    code, out, _err = _run(
        capsys,
        {"server_id": SERVER, "command": "systemctl restart mysql", "confirm": True},
        tmp_path=tmp_path,
    )
    answer = json.loads(out)

    assert code == 1
    assert calls["spawn"] == []
    # An answer on stdout like every other, so a caller parsing it reads the refusal.
    assert answer["success"] is False and answer["error_kind"] == "refused"
    assert "Nothing ran" in answer["error"]
    assert calls["authorize"][0]["confirm"] is True, "intent alone - the terminal still asks"


def test_the_authorization_is_priced_on_the_ladder_of_the_data_dir_the_login_came_from(
    calls, capsys, monkeypatch, tmp_path
):
    dirs = []
    fill = request_fill.fill_request

    def recording(command, request, **kwargs):
        dirs.append((command, kwargs.get("data_dir")))
        return fill(command, request, **kwargs)

    monkeypatch.setattr(request_fill, "fill_request", recording)
    _run(
        capsys,
        {"server_id": SERVER, "command": "uptime", "confirm": "yes"},
        tmp_path=tmp_path,
    )

    assert dirs == [("run-cmd", tmp_path), ("authorize", tmp_path)]


def test_a_rehearsal_resolves_the_login_masks_the_password_and_spawns_nothing(
    calls, capsys, tmp_path
):
    code, out, _err = _run(
        capsys,
        {"server_id": SERVER, "command": "uptime"},
        dry_run=True,
        tmp_path=tmp_path,
    )

    assert code == 0
    assert PASSWORD not in out
    shown = json.loads(out)
    assert shown["dry_run"] is True
    assert shown["request"]["access"]["password"] == "***"
    assert calls["spawn"] == [] and calls["authorize"] == []


def test_a_request_that_is_not_an_object_answers_the_envelope(calls, capsys, tmp_path):
    code = adhoc_cmd.main("[1, 2]", data_dir=tmp_path)
    answer = json.loads(capsys.readouterr().out)

    assert code == 1
    assert answer["success"] is False and answer["error_kind"] == "request"
    assert calls["spawn"] == []


def test_a_missing_host_in_configuration_answers_a_not_configured_envelope(
    calls, capsys, monkeypatch, tmp_path
):
    def fill_error(command, request, **_kwargs):
        if command == "run-cmd":
            raise request_fill.RequestFillError(f"{request.get('target')}: not in db_instances.json")
        return request

    monkeypatch.setattr(request_fill, "fill_request", fill_error)
    code = adhoc_cmd.main(
        json.dumps({"server_id": "LAB-MISSING-HOST", "command": "uptime"}),
        data_dir=tmp_path,
    )
    answer = json.loads(capsys.readouterr().out)

    assert code == 1
    assert answer["success"] is False and answer["error_kind"] == "not_configured"
    assert "LAB-MISSING-HOST" in answer["error"]
    assert calls["spawn"] == []


def test_the_password_travels_on_stdin_never_on_the_command_line():
    spec = lib_common_cli.build_command("run-cmd", {"access": {"password": PASSWORD}})

    assert PASSWORD not in " ".join(spec.argv)
    assert PASSWORD.encode() in spec.stdin


def test_the_cli_routes_run_cmd_adhoc_without_a_configuration(
    calls, capsys, monkeypatch, tmp_path
):
    def no_config(*_args, **_kwargs):
        raise OSError("no configuration in this test")

    monkeypatch.setattr(sre_cli, "load_config", no_config)

    code = sre_cli.main([
        "run-cmd-adhoc",
        json.dumps({"server_id": SERVER, "command": "uptime", "confirm": "yes"}),
        "--data-dir",
        str(tmp_path),
    ])

    assert code == 0
    assert len(calls["spawn"]) == 1
    assert "no log for this run" in capsys.readouterr().err


def test_the_cli_path_does_not_patch_stdout(calls, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(
        sre_cli,
        "load_config",
        lambda *_args, **_kwargs: types.SimpleNamespace(log_dir=str(tmp_path)),
    )
    monkeypatch.setattr(
        sre_cli,
        "setup_app_logger",
        lambda *_args, **_kwargs: logging.getLogger("test_sre_run_cmd_adhoc"),
    )
    patched = []
    monkeypatch.setattr(
        sre_cli,
        "patch_stdout",
        lambda *args, **kwargs: patched.append((args, kwargs)),
    )

    code = sre_cli.main([
        "run-cmd-adhoc",
        json.dumps({"server_id": SERVER, "command": "uptime", "confirm": "yes"}),
        "--data-dir",
        str(tmp_path),
    ])

    assert code == 0
    assert len(calls["spawn"]) == 1
    assert not patched


def test_key_base64_reaches_set_key_env(calls, capsys, monkeypatch, tmp_path):
    recorded = []
    monkeypatch.setattr(
        "dbabrain.lib.secret_text.set_key_env",
        lambda key, key_base64: recorded.append((key, key_base64)),
    )

    code = sre_cli.main([
        "run-cmd-adhoc",
        json.dumps({"server_id": SERVER, "command": "uptime", "confirm": "yes"}),
        "--data-dir",
        str(tmp_path),
        "--key-base64",
        "ZHVtbXk=",
    ])

    assert code == 0
    assert recorded == [(None, "ZHVtbXk=")]


# --------------------------------------------------------------------------- #
# What running an ad-hoc shell command costs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", [
    "dbabrain/common/catalogue/emergency_operations.json",
    "data/emergency_operations.json",
    "data/emergency_operations.example.json",
])
def test_every_shipped_ladder_prices_run_cmd_adhoc_at_50(relative):
    path = Path(__file__).resolve().parents[1] / relative
    operations = json.loads(path.read_text(encoding="utf-8-sig"))["operations"]

    assert operations[adhoc_cmd.OPERATION]["level"] == 50
    assert operations[adhoc_cmd.OPERATION]["effects"] == [
        "one shell command runs on the host as its configured OS login",
        "nothing classifies the command: what it does is what it says",
    ]
