"""An MCP node reads the secret store again when it changes - it is a service, not a command.

``dbabrain mcp connect`` runs for days on the worker (APP-MCP-CONNECT). On 2026-11-17 the lab targets
and their logins were pushed to it, one config file at a time: the inventory, read per request,
named the servers at once, but every request on them was refused - *password ref not found* -
because the node had decrypted its store once, when it started, and kept that copy. Only a restart
of the process made the secrets that were already on disk usable.

So the node holds its copy against the file it came from (its modification time and size): a
changed file is decrypted again, and the redactor is rebuilt from it, so a secret stored after the
start is both usable and hidden from every answer. An unchanged file costs one ``stat``.
"""

from __future__ import annotations

import os
from pathlib import Path

from dbabrain.lib import mcp_tools as M
from dbabrain.lib.secret_text import set_secret_text
from dbabrain.mcp import server as S

KEY = "mcp-node-reads-again"


def _node(data: Path) -> S.Node:
    return S.Node(data_dir=data, config_path=None, settings=M.parse_mcp_tools(dict(M.DEFAULTS)),
                  reference=[], operations=set())


def _touch_later(path: Path) -> None:
    # Some filesystems keep a coarse mtime: move it on, so the test does not depend on the clock.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def test_a_secret_stored_after_the_first_read_is_found(tmp_path, monkeypatch):
    monkeypatch.setenv("DBABRAIN_SECRET_KEY", KEY)
    set_secret_text(tmp_path, "FIRST", "v-first", key=KEY)
    node = _node(tmp_path)
    assert node.secrets() == {"FIRST": "v-first"}

    set_secret_text(tmp_path, "PUSHED_LATER", "v-later", key=KEY)
    _touch_later(tmp_path / "encrypted_secret_text.json")
    assert node.secrets() == {"FIRST": "v-first", "PUSHED_LATER": "v-later"}


def test_a_secret_stored_later_is_redacted_from_answers_too(tmp_path, monkeypatch):
    monkeypatch.setenv("DBABRAIN_SECRET_KEY", KEY)
    set_secret_text(tmp_path, "FIRST", "v-first-value", key=KEY)
    node = _node(tmp_path)
    assert "v-first-value" not in node.redact("answer: v-first-value")

    set_secret_text(tmp_path, "PUSHED_LATER", "v-later-value", key=KEY)
    _touch_later(tmp_path / "encrypted_secret_text.json")
    assert "v-later-value" not in node.redact("answer: v-later-value")


def test_an_unchanged_store_is_not_decrypted_again(tmp_path, monkeypatch):
    monkeypatch.setenv("DBABRAIN_SECRET_KEY", KEY)
    set_secret_text(tmp_path, "FIRST", "v", key=KEY)
    node = _node(tmp_path)
    node.secrets()
    reads = []
    import dbabrain.lib.secret_text as T

    monkeypatch.setattr(T, "load_secret_text", lambda *a, **k: reads.append(1) or {})
    node.secrets()
    node.redact("text")
    assert reads == []


def test_a_store_that_becomes_readable_clears_the_error(tmp_path, monkeypatch):
    set_secret_text(tmp_path, "FIRST", "v", key=KEY)
    monkeypatch.delenv("DBABRAIN_SECRET_KEY", raising=False)
    node = _node(tmp_path)
    assert node.secrets() == {} and node.secrets_error

    monkeypatch.setenv("DBABRAIN_SECRET_KEY", KEY)
    set_secret_text(tmp_path, "SECOND", "w", key=KEY)
    _touch_later(tmp_path / "encrypted_secret_text.json")
    assert node.secrets() == {"FIRST": "v", "SECOND": "w"} and node.secrets_error == ""
