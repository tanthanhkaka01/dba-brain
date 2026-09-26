"""The bot finishes a ``common.cli`` action's request and sends it on stdin, never in argv (R09, R14).

The bot's actions are data: an argv with the request rendered into one argument, naming only a
``server_id``. Since 0.24.0 ``common.cli`` reads no configuration, so the bot has to finish that
request - the login, the policy, the price - before it runs. The finished request carries a
password, and an argument is readable by every process on the host (``ps``, ``/proc/*/cmdline``),
so it must travel on the child's stdin with ``-`` left where the JSON was.

``host-restart`` is the case worth holding: it is detached - started through the exit-code wrapper
and polled later - so its stdin is the wrapper's, handed on to the command. If that hand-off broke,
the restart would start with an empty request and be refused, after the bot had already said
"Restart requested".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from db_ops.lib.common_cli import common_invocation
from db_ops.telegram import command_processor

REPO = Path(__file__).resolve().parents[1]


def _shipped_action(command_text: str) -> dict:
    document = json.loads((REPO / "db_ops" / "telegram" / "catalogue" / "telegram_support_commands.json")
                          .read_text(encoding="utf-8"))
    records = document["telegram_support_commands"] if isinstance(document, dict) else document
    return next(dict(item["action_config"]) for item in records if item.get("command_text") == command_text)


# --------------------------------------------------------------------------- #
# Recognising a common.cli action in a configured argv
# --------------------------------------------------------------------------- #
def test_a_rendered_common_action_yields_its_command_and_request():
    argv = [sys.executable, "-m", "db_ops.common.cli", "kill-spid", '{"target": "LAB", "spid": 55}',
            "--config", "config.json"]

    invocation = common_invocation(argv)

    assert (invocation.command, invocation.request, invocation.request_index) == (
        "kill-spid", {"target": "LAB", "spid": 55}, 4)


@pytest.mark.parametrize("request_arg", ["-", "@data/request.json", "not json", "[1, 2]"])
def test_a_request_that_is_not_an_inline_object_is_not_one_to_finish(request_arg):
    """Stdin or a file is already how a password travels; a non-object is common.cli's to refuse."""
    assert common_invocation([sys.executable, "-m", "db_ops.common.cli", "kill-spid", request_arg]) is None


def test_a_line_that_runs_another_module_is_left_alone():
    assert common_invocation([sys.executable, "-m", "db_ops.reports.cli", "build", '{"a": 1}']) is None


# --------------------------------------------------------------------------- #
# The detached host-restart: finished, then handed to the wrapper's stdin
# --------------------------------------------------------------------------- #
class _Store:
    def __init__(self):
        self.messages, self.tasks = [], []

    def insert_telegram_send_message(self, **kwargs):
        self.messages.append(kwargs)
        return len(self.messages)

    def insert_telegram_background_task(self, **kwargs):
        self.tasks.append(kwargs)
        return len(self.tasks)


class _Stdin:
    def __init__(self):
        self.written, self.closed = b"", False

    def write(self, data: bytes) -> None:
        self.written += data

    def close(self) -> None:
        self.closed = True


class _Popen:
    started: list["_Popen"] = []

    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs, self.pid = list(argv), kwargs, 4242
        self.stdin = _Stdin() if kwargs.get("stdin") == subprocess.PIPE else None
        _Popen.started.append(self)

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(self.argv, timeout)  # a restart is still running


@pytest.fixture()
def restart(monkeypatch, tmp_path):
    """Run the shipped `spbot_restart_server` action once, with the filler and the start faked."""
    _Popen.started = []
    monkeypatch.setenv("DB_OPS_SECRET_KEY", "test-passphrase")
    monkeypatch.setattr(command_processor.subprocess, "Popen", _Popen)
    finished_for: list[tuple[str, dict]] = []

    def finish(command, request, **_kwargs):
        finished_for.append((command, dict(request)))
        return {**request, "access": {"method": "ssh", "host": "10.0.0.5", "username": "osadmin",
                                      "password": "a-password-only-stdin-may-carry"},
                "policy": {}, "rules": {"level": 100, "confirmations": 2, "challenge": "target_id"}}

    monkeypatch.setattr(command_processor, "finish_common_request", finish)
    command = command_processor.SupportCommand(
        command_id=9, command_text="spbot_restart_server", command_type=1, reply_default=0, reply_text="",
        is_group=1, is_private=1, need_file=0, action_type="cli_execute",
        action_config=_shipped_action("spbot_restart_server"))
    store = _Store()
    values = {"python": sys.executable, "server_id": "LAB-10-0-0-5", "confirm": "yes",
              "confirm_target": "LAB-10-0-0-5", "config_path": str(tmp_path / "config.json")}
    result = command_processor.execute_cli_background_command(
        store=store, row={"chat_id": "100", "message_id": 10, "user_id": "100"}, command=command,
        values=values, source_id="1")
    return {"result": result, "store": store, "finished_for": finished_for, "popen": _Popen.started[-1]}


def test_the_restart_request_is_finished_from_the_server_it_names(restart):
    [(command, request)] = restart["finished_for"]

    assert command == "host-restart"
    assert request["target"] == "LAB-10-0-0-5"


def test_the_password_is_on_the_wrappers_stdin_and_nowhere_in_its_argv(restart):
    popen = restart["popen"]

    assert "a-password-only-stdin-may-carry" not in " ".join(popen.argv)
    assert popen.stdin is not None and popen.stdin.closed
    sent = json.loads(popen.stdin.written.decode("utf-8"))
    assert sent["access"]["password"] == "a-password-only-stdin-may-carry"
    assert sent["target"] == "LAB-10-0-0-5"


def test_the_request_argument_becomes_a_dash_so_the_command_reads_stdin(restart):
    argv = restart["popen"].argv
    command_at = argv.index("host-restart")

    assert argv[command_at + 1] == "-"
    assert argv[:3] == [sys.executable, "-m", "db_ops.telegram.detached_exit"]


def test_a_restart_still_running_is_handed_to_the_poller(restart):
    assert restart["result"].get("status") != "FAILED_REQUEST"
    assert [task["pid"] for task in restart["store"].tasks] == [4242]


def test_a_server_the_node_cannot_finish_is_refused_before_anything_starts(monkeypatch, tmp_path):
    _Popen.started = []
    monkeypatch.setenv("DB_OPS_SECRET_KEY", "test-passphrase")
    monkeypatch.setattr(command_processor.subprocess, "Popen", _Popen)

    def cannot(command, request, **_kwargs):
        raise command_processor.TelegramCommandError("LAB-GONE: no such server_id", exit_code=2)

    monkeypatch.setattr(command_processor, "finish_common_request", cannot)
    command = command_processor.SupportCommand(
        command_id=9, command_text="spbot_restart_server", command_type=1, reply_default=0, reply_text="",
        is_group=1, is_private=1, need_file=0, action_type="cli_execute",
        action_config=_shipped_action("spbot_restart_server"))
    store = _Store()

    result = command_processor.execute_cli_background_command(
        store=store, row={"chat_id": "100", "message_id": 10, "user_id": "100"}, command=command,
        values={"python": sys.executable, "server_id": "LAB-GONE", "confirm": "yes",
                "confirm_target": "LAB-GONE", "config_path": str(tmp_path / "config.json")},
        source_id="1")

    assert result["status"] == "FAILED_REQUEST"
    assert _Popen.started == []
    assert "no such server_id" in store.messages[-1]["message_text"]
