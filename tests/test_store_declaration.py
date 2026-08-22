"""The store, stated as data — the thing that let ``common`` stop looking anything up.

``common`` performs work and reads nothing. That already held for SQL against a target database (a
``run-sql`` request carries host, login and password) but not for the *runtime store*: a caller
could only say "config.json" and let the other side read it. So the one thing every app writes
through was the one thing that could not be named in a request.

Two properties matter more than the serialisation itself:

* **A caller can name a store that is not the node's own.** An in-process call could hand over a
  live store object; a subprocess cannot. Without a declaration, moving message queueing onto the
  CLI broke eleven tests that write to a temp store, because there was no way to say which store
  they meant. A declaration is something a test can build — which is why the move became possible
  at all instead of a rewrite.
* **The password is a value, not a reference.** ``password_ref`` is a lookup, and resolving it is
  the app's job. A declaration carrying a ref would put the secret store back on the far side.
"""

from __future__ import annotations

import json
import subprocess
import sys
import sqlite3

import pytest

from db_ops.config import PostgresStoreConfig, SqliteStoreConfig, StoreConfig
from db_ops.db import DbOpsStore, declaration


def _pg_config(**over):
    fields = dict(host="10.0.0.1", port=5433, database="db_ops", schema="db_ops",
                  username="postgres", sslmode="prefer")
    fields.update(over)
    return StoreConfig(backend="postgresql", postgresql=PostgresStoreConfig(**fields))


# --------------------------------------------------------------------------- #
# Describing
# --------------------------------------------------------------------------- #
def test_a_sqlite_store_describes_itself_as_a_path():
    block = declaration.describe(StoreConfig(sqlite=SqliteStoreConfig(path="/tmp/x.sqlite")))

    assert block["backend"] == "sqlite"
    assert block["sqlite"]["path"].endswith("x.sqlite")


def test_a_postgres_store_carries_a_resolved_password_not_a_ref():
    """A ref would mean the far side has to open the secret store, which is the lookup the whole
    split exists to remove."""
    block = declaration.describe(_pg_config(password_ref="SOME_REF"), password="s3cret")

    assert block["postgresql"]["password"] == "s3cret"
    assert "password_ref" not in block["postgresql"]
    assert "SOME_REF" not in json.dumps(block)


def test_describing_refuses_anything_that_is_not_a_store():
    with pytest.raises(declaration.StoreDeclarationError, match="describe expects"):
        declaration.describe({"backend": "postgresql"})


def test_a_live_store_can_describe_itself_without_a_config(tmp_path):
    """The daemon's case: its notify helper deliberately takes no config — holding Telegram
    settings of its own is exactly how it drifted from every other app — so the store object is
    the only thing that can say where a row goes."""
    store = DbOpsStore(tmp_path / "runtime.sqlite")

    block = declaration.describe_store(store)

    assert block == declaration.for_path(tmp_path / "runtime.sqlite")


def test_describing_something_that_is_not_a_store_is_refused():
    with pytest.raises(declaration.StoreDeclarationError, match="describe_store expects"):
        declaration.describe_store(object())


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_a_declaration_round_trips(tmp_path):
    target = declaration.parse(declaration.for_path(tmp_path / "r.sqlite"))

    assert target.store.is_sqlite
    assert str(target.store.sqlite.path).endswith("r.sqlite")


def test_the_password_rides_beside_the_declaration():
    """StoreTarget already takes it as an override, which is what keeps a ref out of the block."""
    target = declaration.parse(declaration.describe(_pg_config(), password="pw"))

    assert target.store.backend == "postgresql"
    assert target.password == "pw"


@pytest.mark.parametrize("block,expected", [
    ({}, "store.backend"),
    ({"backend": "mysql"}, "store.backend"),
    ({"backend": "sqlite", "sqlite": {}}, "store.sqlite.path"),
    ({"backend": "postgresql", "postgresql": {"database": "d", "username": "u"}}, "host"),
    ({"backend": "postgresql", "postgresql": {"host": "h", "username": "u"}}, "database"),
], ids=["empty", "unknown_backend", "no_path", "no_host", "no_database"])
def test_an_incomplete_declaration_is_refused_by_name(block, expected):
    """Named rather than defaulted: a store block that silently fell back to localhost would write
    the row somewhere real and wrong."""
    with pytest.raises(declaration.StoreDeclarationError, match=expected):
        declaration.parse(block)


def test_parsing_reads_no_config_and_no_secret_store():
    """Asserted on the imports, because the split only holds while it is true."""
    import ast

    tree = ast.parse(open(declaration.__file__, encoding="utf-8").read())
    top_level = {
        alias.name.split(".")[0]
        for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "subprocess" not in top_level


# --------------------------------------------------------------------------- #
# Redacting
# --------------------------------------------------------------------------- #
def test_the_password_is_hidden_for_logging_and_the_original_is_untouched():
    block = declaration.describe(_pg_config(), password="s3cret")

    safe = declaration.redact(block)

    assert safe["postgresql"]["password"] == "***"
    assert block["postgresql"]["password"] == "s3cret", "redact must not mutate its input"


# --------------------------------------------------------------------------- #
# The property the whole move depended on
# --------------------------------------------------------------------------- #
def test_the_cli_writes_to_the_store_the_caller_named(tmp_path):
    """Not to config.json's. This is what unblocked moving message queueing onto the CLI: eleven
    tests write to a temp store, and before declarations there was no way to tell it which."""
    sqlite_path = tmp_path / "named.sqlite"
    DbOpsStore(sqlite_path).initialize()
    request = {
        "store": declaration.for_path(sqlite_path),
        "chat_id": "-1", "text": "declared", "level": "logging", "phase": "START",
        "source_type": "declaration_test", "source_id": "1",
    }

    completed = subprocess.run(
        [sys.executable, "-m", "db_ops.db.cli", "queue-telegram-message", "-"],
        input=json.dumps(request), capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(sqlite_path) as conn:
        rows = conn.execute(
            "SELECT tlgchat_id, message_text FROM telegram_send_messages").fetchall()
    assert rows == [("-1", "declared")]
