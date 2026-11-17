"""A node kept running by the daemon waits for its link; it never asks for one.

``dbabrain mcp connect`` links a machine with a device sign-in: it prints a code, and the person opens
the link and approves it within ten minutes. Run by the daemon as a service (the worker, 2026-11-16),
nobody is looking at that output: a node with no token would ask for a code, let it expire, end, be
started again and ask again, all day. And the first code the worker asked for did expire - its
process ended in eleven lines of stack instead of a sentence.

So ``--wait-for-link`` never starts a link. With no token it says once what is missing and reads the
secret store again each minute, and a token arriving from a ``connect`` run by hand is the one it
connects with. A token the Worker refuses is set aside the same way, never retried against the
Worker in a loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbabrain.mcp import cli, relay


class _Node:
    """The node's secret store as a sequence of reads: each read answers the next state."""

    def __init__(self, reads: list[dict[str, str]], error: str = "") -> None:
        self.reads = list(reads)
        self.data_dir = Path(".")
        self._secrets = None
        self._redactor = None
        self.secrets_error = error
        self.read_count = 0

    def secrets(self) -> dict[str, str]:
        if self._secrets is None:
            self.read_count += 1
            self._secrets = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        return self._secrets


def _token(value: str) -> dict[str, str]:
    return {relay.NODE_TOKEN_REF: value}


def test_a_node_with_no_token_waits_and_reads_the_store_again(capsys):
    node = _Node([{}, {}, _token("t-1")])
    sleeps: list[int] = []
    assert relay.wait_for_token(node, sleep=sleeps.append) == "t-1"
    assert sleeps == [relay.LINK_WAIT_SECONDS] * 2
    said = capsys.readouterr().err
    assert said.count("not linked") == 1, "the missing link is said once, not every minute"
    assert "dbabrain mcp connect" in said


def test_the_service_never_starts_a_device_link(monkeypatch):
    monkeypatch.setattr(relay, "link", lambda *_a, **_k: pytest.fail("the service asked for a code"))
    node = _Node([{}, _token("t-1")])
    connected: list[str] = []
    relay.connect_node(node, wait_for_link=True, sleep=lambda _s: None, run=connected.append)
    assert connected == ["t-1"]


def test_a_refused_token_is_set_aside_until_a_new_one_is_stored(capsys):
    node = _Node([_token("old"), _token("old"), _token("old"), _token("new")])
    sleeps: list[int] = []
    tried: list[str] = []

    def run(token: str) -> None:
        tried.append(token)
        if token == "old":
            raise relay.RelayError("dbabrain.dev refused this node's token (401) - it was unlinked")

    relay.connect_node(node, wait_for_link=True, sleep=sleeps.append, run=run)
    assert tried == ["old", "new"], "the refused token was never tried against the Worker again"
    assert len(sleeps) == 2
    assert "--relink" in capsys.readouterr().err


def test_a_store_it_cannot_read_is_named_as_the_reason(capsys):
    node = _Node([{}, _token("t")], error="SecretKeyMissing: no key")
    relay.wait_for_token(node, sleep=lambda _s: None)
    assert "DBABRAIN_SECRET_KEY" in capsys.readouterr().err


def test_a_link_that_was_not_approved_ends_in_a_sentence(monkeypatch, capsys):
    def expired(*_a, **_k):
        raise relay.RelayError("the link was not approved (400): expired_token")

    monkeypatch.setattr(relay, "connect_node", expired)
    monkeypatch.setattr(cli, "_node", lambda _home: object())
    assert cli.main(["connect"]) == 1
    err = capsys.readouterr().err
    assert "expired_token" in err and "Traceback" not in err


def test_relink_and_wait_for_link_are_not_asked_together(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_node", lambda _home: object())
    assert cli.main(["connect", "--relink", "--wait-for-link"]) == 2
    assert "one of them" in capsys.readouterr().err
