"""Routing has two halves, and they are tested where each one lives.

The **lookup** is ``lib.telegram_route``: the route is a function of the configuration, read
in-process (0.24.0) - it used to cost a process per level, running the Telegram app's CLI from the
layer everything imports (rules R07, R42). It must cache, and fail closed.

The **policy** is shared and pure: :mod:`db_ops.lib.notify_route` is handed the answer and decides
where a message goes. It must not read config, open a store, or start a process - the import
assertion at the bottom keeps that true.

Failing closed matters more than it looks: a lookup that quietly returned "do not alert" would
suppress exactly the error somebody is waiting for, so a broken lookup must produce no chat *and*
say so on stderr.
"""

import pytest

from db_ops.lib import telegram_route as client
from db_ops.lib import notify_route


@pytest.fixture(autouse=True)
def _clear_cache():
    client.clear_cache()
    yield
    client.clear_cache()


def _settings(monkeypatch, answer, calls=None):
    """Stand in for the one read of the Telegram settings: (enabled, level_chat_map, levels)."""
    def fake():
        if calls is not None:
            calls.append(1)
        return answer() if callable(answer) else answer
    monkeypatch.setattr(client, "telegram_settings", fake)


# --------------------------------------------------------------------------- #
# The lookup: read the configuration, cache it, fail closed.
# --------------------------------------------------------------------------- #
def test_the_route_comes_from_the_configuration_with_no_process(monkeypatch):
    """The settings have one reader, in-process - nothing is started to learn a chat id."""
    import subprocess

    def no_process(*_args, **_kwargs):
        raise AssertionError("routing started a process")
    monkeypatch.setattr(subprocess, "run", no_process)
    _settings(monkeypatch, (True, {"error": "-100"}, []))

    assert client.telegram_route("error") == {"enabled": True, "alert": True, "chat_id": "-100"}


def test_a_level_that_must_not_alert_yields_no_chat(monkeypatch):
    _settings(monkeypatch, (True, {}, []))

    assert notify_route.chat_from_route(client.telegram_route("logging")) == ""


def test_telegram_switched_off_alerts_nobody_even_with_a_chat(monkeypatch):
    _settings(monkeypatch, (False, {"error": "-100"}, []))

    assert client.telegram_route("error")["alert"] is False


def test_the_route_is_cached_per_level(monkeypatch):
    """A run emitting a burst of events must not read the configuration for each one."""
    calls = []
    _settings(monkeypatch, (True, {"error": "-1"}, []), calls=calls)

    client.telegram_route("error")
    client.telegram_route("error")

    assert len(calls) == 1


def test_different_levels_are_looked_up_separately(monkeypatch):
    _settings(monkeypatch, (True, {"error": "-error", "warning": "-warning"}, []))

    assert client.telegram_route("error")["chat_id"] == "-error"
    assert client.telegram_route("warning")["chat_id"] == "-warning"


def test_a_broken_lookup_fails_closed_and_says_so(monkeypatch, capsys):
    def broken():
        raise ValueError("config.json is not valid JSON")
    _settings(monkeypatch, broken)

    assert client.telegram_route("error") == notify_route.NO_ROUTE
    # Reported, not swallowed: a silent "do not alert" hides the error being reported.
    assert "config.json is not valid JSON" in capsys.readouterr().err


def test_a_failed_lookup_is_not_cached(monkeypatch):
    """Otherwise one blip mutes the level for the whole TTL."""
    calls = []

    def broken():
        raise OSError("config.json is being written")
    _settings(monkeypatch, broken, calls=calls)

    client.telegram_route("error")
    client.telegram_route("error")

    assert len(calls) == 2


def test_groups_parses_the_map(monkeypatch):
    _settings(monkeypatch, (True, {"logging": "-1", "Warning": "-2"}, []))

    assert client.telegram_groups() == {"logging": "-1", "warning": "-2"}


def test_groups_is_one_read_for_the_whole_map(monkeypatch):
    calls = []
    _settings(monkeypatch, (True, {"logging": "-1"}, []), calls=calls)

    client.telegram_groups()
    client.telegram_groups()

    assert len(calls) == 1


def test_a_broken_map_is_empty_and_said(monkeypatch, capsys):
    def broken():
        raise ValueError("unreadable")
    _settings(monkeypatch, broken)

    assert client.telegram_groups() == {}
    assert "unreadable" in capsys.readouterr().err


def test_an_empty_level_needs_no_lookup(monkeypatch):
    calls = []
    _settings(monkeypatch, (True, {}, []), calls=calls)

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
