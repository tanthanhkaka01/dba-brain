"""Routing has two halves now, and they are tested where each one lives.

The **transport** is per app: ``<app>/telegram_route.py`` runs ``db_ops.telegram.cli`` and must
parse, cache, and fail closed. Every app's copy is the same file, so one of them stands in for all
— ``backup_restore`` here, because it is the heaviest producer of events.

The **policy** is shared and pure: :mod:`db_ops.lib.notify_route` is handed the answer and
decides where a message goes. It must not read config, open a store, or start a process — that is
the whole point of the split, and the import assertion at the bottom is what keeps it true.

Failing closed matters more than it looks: a lookup that quietly returned "do not alert" would
suppress exactly the error somebody is waiting for, so a broken lookup must produce no chat *and*
say so on stderr.
"""

import json

import pytest

from db_ops.lib import telegram_route as client
from db_ops.lib import notify_route


@pytest.fixture(autouse=True)
def _clear_cache():
    client.clear_cache()
    yield
    client.clear_cache()


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _stub_run(monkeypatch, result, calls=None):
    def fake_run(cmd, **_kwargs):
        if calls is not None:
            calls.append(cmd)
        return result if not callable(result) else result(cmd)
    monkeypatch.setattr(client.subprocess, "run", fake_run)


# --------------------------------------------------------------------------- #
# The transport: run the Telegram app's CLI, parse it, cache it, fail closed.
# --------------------------------------------------------------------------- #
def test_the_route_comes_from_the_telegram_apps_cli(monkeypatch):
    """Not from common's CLI, and not from config read here: the settings have one owner."""
    calls = []
    _stub_run(monkeypatch, _Completed(stdout=json.dumps(
        {"level": "error", "enabled": True, "alert": True, "chat_id": "-100"}
    )), calls=calls)

    assert client.telegram_route("error") == {"enabled": True, "alert": True, "chat_id": "-100"}
    assert calls[0][1:] == ["-m", "db_ops.telegram.cli", "route", "error"]


def test_a_level_that_must_not_alert_yields_no_chat(monkeypatch):
    _stub_run(monkeypatch, _Completed(stdout=json.dumps(
        {"level": "logging", "enabled": True, "alert": False, "chat_id": ""}
    )))

    assert notify_route.chat_from_route(client.telegram_route("logging")) == ""


def test_the_route_is_cached_per_level(monkeypatch):
    """A run emitting a burst of events must not pay a subprocess for each one."""
    calls = []
    _stub_run(monkeypatch, _Completed(stdout=json.dumps({"enabled": True, "alert": True, "chat_id": "-1"})), calls=calls)

    client.telegram_route("error")
    client.telegram_route("error")

    assert len(calls) == 1


def test_different_levels_are_looked_up_separately(monkeypatch):
    def by_level(cmd):
        return _Completed(stdout=json.dumps(
            {"enabled": True, "alert": True, "chat_id": f"-{cmd[-1]}"}))
    _stub_run(monkeypatch, by_level)

    assert client.telegram_route("error")["chat_id"] == "-error"
    assert client.telegram_route("warning")["chat_id"] == "-warning"


@pytest.mark.parametrize("result", [
    pytest.param(_Completed(returncode=1, stderr="boom"), id="nonzero_exit"),
    pytest.param(_Completed(stdout="not json"), id="bad_json"),
    pytest.param(_Completed(stdout=""), id="empty_output"),
], )
def test_a_broken_lookup_fails_closed(monkeypatch, result, capsys):
    _stub_run(monkeypatch, result)

    assert client.telegram_route("error") == notify_route.NO_ROUTE
    # Reported, not swallowed: a silent "do not alert" hides the error being reported.
    assert capsys.readouterr().err.strip() != ""


def test_a_failed_lookup_is_not_cached(monkeypatch):
    """Otherwise one blip mutes the level for the whole TTL."""
    calls = []
    _stub_run(monkeypatch, _Completed(returncode=1), calls=calls)

    client.telegram_route("error")
    client.telegram_route("error")

    assert len(calls) == 2


def test_a_subprocess_that_cannot_start_is_survivable(monkeypatch):
    def raise_oserror(cmd, **_kwargs):
        raise OSError("no such executable")
    monkeypatch.setattr(client.subprocess, "run", raise_oserror)

    assert client.telegram_route("error")["alert"] is False


def test_groups_parses_the_json_map(monkeypatch):
    _stub_run(monkeypatch, _Completed(stdout=json.dumps({"logging": "-1", "Warning": "-2"})))

    assert client.telegram_groups() == {"logging": "-1", "warning": "-2"}


def test_groups_costs_one_process_for_the_whole_map(monkeypatch):
    calls = []
    _stub_run(monkeypatch, _Completed(stdout=json.dumps({"logging": "-1"})), calls=calls)

    client.telegram_groups()
    client.telegram_groups()

    assert len(calls) == 1


def test_an_empty_level_needs_no_lookup(monkeypatch):
    calls = []
    _stub_run(monkeypatch, _Completed(stdout="{}"), calls=calls)

    assert client.telegram_route("")["alert"] is False
    assert calls == []


# --------------------------------------------------------------------------- #
# The policy: pure, and it has to stay that way.
# --------------------------------------------------------------------------- #
def test_a_malformed_answer_becomes_no_route_rather_than_a_partial_one():
    """``{"alert": true}`` with no chat would send nowhere and report success."""
    assert notify_route.parse_route({"alert": True}) == {
        "enabled": False, "alert": True, "chat_id": "",
    }
    assert notify_route.parse_route("nonsense") == notify_route.NO_ROUTE
    assert notify_route.parse_groups(None) == {}


def test_the_shared_layer_starts_no_process_and_reads_no_config():
    """The split only holds while this is true, so it is asserted rather than trusted.

    ``common`` sits below every app: importing the config layer or spawning a CLI here is what
    the routing move existed to remove, and both are easy to reintroduce by reaching for the
    nearest helper.
    """
    import ast

    # Read the imports, not the prose: the module's docstring explains the subprocess it used to
    # run, and a substring search on the source calls that a violation.
    tree = ast.parse(open(notify_route.__file__, encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "subprocess" not in imported, "notify_route is starting processes again"
    # `db_ops` here would be the config layer or an app; `typing`/`__future__` are fine.
    assert imported <= {"typing", "__future__"}, f"notify_route grew dependencies: {imported}"
