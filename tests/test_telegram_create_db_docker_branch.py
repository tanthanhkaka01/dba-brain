"""`/spbot_create_db_docker` asks the credential question once, and builds the same argv as before.

The command that produced this work asked fourteen questions every time, four of them about SSH
credentials that only apply to a remote deploy — so a worker deploy was answered with `-` three
times, and a remote deploy with a stored secret ref was *still* asked for a password and a key
file after the ref had been given.

The rewrite makes those a branch. What must not change is the command line the answers produce:
`conditional_args` decides every optional flag by testing the answer against `-`, so a step that
this run never asked has to resolve to `-` and not to the empty string. Empty would pass a
`not_equals: "-"` test and hand the CLI `--remote-password-ref` with nothing after it.
"""

from __future__ import annotations

import json

import pytest

from conftest import shipped_config
from db_ops.telegram import command_processor as cp

COMMAND_TEXT = "spbot_create_db_docker"


@pytest.fixture(scope="module")
def command():
    doc = json.loads(shipped_config("telegram_support_commands.json").read_text(encoding="utf-8"))
    raw = next(item for item in doc["telegram_support_commands"]
               if item["command_text"] == COMMAND_TEXT)

    class Command:
        action_config = raw["action_config"]
        command_id = raw["command_id"]
        command_text = raw["command_text"]
        action_type = raw["action_type"]

    return Command


def questions_asked(command, answers: list[str]) -> list[str]:
    """Walk the command the way the conversation does, feeding scripted answers."""
    args = [""] * 20
    asked: list[str] = []
    remaining = list(answers)
    while True:
        step = cp.first_missing_prompt_parameter(command, args)
        if step is None:
            return asked
        asked.append(str(step["name"]))
        if not remaining:
            return asked + ["<still waiting>"]
        args[int(step["position"]) - 1] = remaining.pop(0)


def argv_for(command, args: list[str]) -> list[str]:
    values = cp.cli_action_values(command=command, args=args, config_path="config.json",
                                  chat_id="1", user_id="1")
    return cp.build_cli_argv(command.action_config, values)


# --------------------------------------------------------------------------- #
# What gets asked
# --------------------------------------------------------------------------- #
def test_a_worker_deploy_is_never_asked_about_ssh(command) -> None:
    asked = questions_asked(command, ["lab01", "mssql", "2025-latest", "single",
                                      "-", "-", "-", "worker", "no"])

    assert asked == ["name", "engine", "version", "mode", "host_port", "password_env",
                     "password_text", "deploy_target", "recreate"]
    assert not [name for name in asked if name.startswith("remote_")]
    assert "install_docker" not in asked, "there is no remote VM to install docker on"


def test_a_stored_secret_ref_ends_the_credential_question(command) -> None:
    """The report: after giving the ref, the operator was asked for a password and a key file."""
    asked = questions_asked(command, ["lab01", "mssql", "2025-latest", "single", "-", "-", "-",
                                      "192.0.2.115", "dev", "secret_ref",
                                      "REMOTE_192_0_2_115_DEV", "no", "no"])

    assert "remote_password_ref" in asked
    assert "remote_password_text" not in asked
    assert "remote_key_name" not in asked


def test_choosing_a_key_file_asks_only_for_the_key(command) -> None:
    asked = questions_asked(command, ["lab01", "mssql", "2025-latest", "single", "-", "-", "-",
                                      "192.0.2.115", "dev", "key_file", "oracle-cloud.key",
                                      "no", "no"])

    assert "remote_key_name" in asked
    assert "remote_password_ref" not in asked and "remote_password_text" not in asked


def test_the_credential_choice_is_a_closed_list(command) -> None:
    """Free text here would be a typo that reaches the CLI as an auth mode it does not have."""
    step = next(item for item in command.action_config["parameters"]
                if item["name"] == "remote_auth")
    assert step["allow_text_input"] is False
    assert [option["value"] for option in step["options"]] == [
        "secret_ref", "password", "key_file"]


# --------------------------------------------------------------------------- #
# What is executed
# --------------------------------------------------------------------------- #
def test_a_worker_deploy_passes_no_remote_flags(command) -> None:
    argv = argv_for(command, ["lab01", "mssql", "2025-latest", "single", "", "", "",
                              "worker", "", "", "", "", "", "no", ""])

    assert "--remote-host" not in argv
    assert "--remote-password-ref" not in argv
    assert "--remote-key" not in argv
    assert "--force" not in argv, "recreate=no must not destroy anything"


def test_a_secret_ref_deploy_passes_the_ref_and_nothing_else(command) -> None:
    """The empty-string trap: a step never asked must resolve to `-`, not to ``."""
    argv = argv_for(command, ["lab01", "mssql", "2025-latest", "single", "1455", "MSSQL_LAB_SA",
                              "Secret123!", "192.0.2.115", "dev", "secret_ref",
                              "REMOTE_192_0_2_115_DEV", "", "", "no", "no"])

    assert argv[argv.index("--remote-host") + 1] == "192.0.2.115"
    assert argv[argv.index("--remote-user") + 1] == "dev"
    assert argv[argv.index("--remote-password-ref") + 1] == "REMOTE_192_0_2_115_DEV"
    assert "--remote-password-env" not in argv
    assert "--remote-key" not in argv
    assert "" not in argv, "a flag with an empty value is the failure this test exists for"


def test_recreate_yes_still_carries_both_destructive_flags(command) -> None:
    argv = argv_for(command, ["lab01", "mssql", "2025-latest", "single", "", "", "",
                              "worker", "", "", "", "", "", "yes", ""])

    assert "--force" in argv and "--overwrite-secret" in argv
