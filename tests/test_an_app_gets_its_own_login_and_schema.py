"""Another tool sharing the store's instance gets its own login and its own schema.

The operator, 2026-11-04: daily-assists keeps its journal in the dbabrain PostgreSQL instance,
database `db_ops`, a schema of its own, and dbabrain may create the user for it. The login must get
exactly that - CONNECT, its schema, its search_path - and nothing on dbabrain's own schema; an
existing role's password must not be changed behind whatever already uses it; a superuser is never
handed over; the password never appears in what is printed.
"""

from __future__ import annotations

import pytest

from dbabrain.db import postgres_store as P
from dbabrain.lib.config import PostgresStoreConfig


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def execute(self, sql, params=()):
        self.conn.seen.append(sql)
        if sql.startswith("SELECT rolsuper"):
            self._row = self.conn.role
        elif sql.startswith("SELECT 1 FROM information_schema.schemata"):
            self._row = (1,) if self.conn.schema_there else None
        elif sql.startswith("SELECT pg_is_in_recovery"):
            self._row = (self.conn.standby,)
        elif sql.startswith("SELECT has_schema_privilege"):
            self._row = (False,)
        else:
            self.conn.ran.append(sql)

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _Conn:
    def __init__(self, *, role=None, schema_there=False, standby=False):
        self.role, self.schema_there, self.standby = role, schema_there, standby
        self.seen: list[str] = []
        self.ran: list[str] = []

    def cursor(self):
        return _Cursor(self)

    def close(self):
        pass


def _store():
    return PostgresStoreConfig(host="h", port=5433, database="db_ops", schema="db_ops",
                               username="postgres")


def _run(monkeypatch, conn, **kwargs):
    monkeypatch.setattr(P, "connect", lambda *a, **k: conn)
    options = {"role": "daily_assists", "schema": "daily_assists", "role_password": "s3cr'et", **kwargs}
    return P.create_app_login(_store(), password="x", **options)


def test_a_new_login_gets_its_schema_connect_and_search_path_and_nothing_more(monkeypatch):
    conn = _Conn()
    result = _run(monkeypatch, conn)
    assert conn.ran[0].startswith('CREATE ROLE "daily_assists" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
    assert conn.ran[0].endswith("PASSWORD 's3cr''et'"), "the quote in the password is doubled"
    assert conn.ran[1:] == [
        'CREATE SCHEMA "daily_assists" AUTHORIZATION "daily_assists"',
        f'GRANT CONNECT ON DATABASE "{_store().database}" TO "daily_assists"',
        f'ALTER ROLE "daily_assists" IN DATABASE "{_store().database}" SET search_path = "daily_assists"',
    ]
    assert not any(f'"{_store().schema}"' in sql and "SCHEMA" in sql for sql in conn.ran), "nothing on the store's schema"
    assert result.role_created and result.schema_created and not result.reaches_store_schema


def test_the_password_is_never_in_what_is_reported(monkeypatch):
    result = _run(monkeypatch, _Conn())
    assert all("s3cr" not in statement for statement in result.statements)
    assert "'***'" in result.statements[0]
    assert "s3cr" not in result.summary()


def test_a_dry_run_runs_nothing(monkeypatch):
    conn = _Conn()
    result = _run(monkeypatch, conn, dry_run=True)
    assert conn.ran == [] and result.dry_run and len(result.statements) == 4


def test_an_existing_role_keeps_its_password_unless_asked(monkeypatch):
    conn = _Conn(role=(False, True), schema_there=True)
    result = _run(monkeypatch, conn)
    assert not any("PASSWORD" in sql for sql in conn.ran)
    assert conn.ran[0] == 'GRANT USAGE, CREATE ON SCHEMA "daily_assists" TO "daily_assists"'
    assert not result.role_created and not result.password_set

    conn = _Conn(role=(False, True), schema_there=True)
    assert _run(monkeypatch, conn, reset_password=True).password_set
    assert conn.ran[0].startswith('ALTER ROLE "daily_assists" LOGIN PASSWORD')


def test_a_superuser_role_is_never_handed_to_an_app(monkeypatch):
    with pytest.raises(P.PostgresStoreError, match="superuser"):
        _run(monkeypatch, _Conn(role=(True, True)))


@pytest.mark.parametrize("schema", [_store().schema, "public"])
def test_the_store_schema_or_public_is_not_an_apps_schema(monkeypatch, schema):
    with pytest.raises(P.PostgresStoreError, match="schema of its own"):
        _run(monkeypatch, _Conn(), schema=schema)


def test_names_go_through_the_identifier_check(monkeypatch):
    with pytest.raises(P.PostgresStoreError, match="Unsafe role"):
        _run(monkeypatch, _Conn(), role='x"; DROP TABLE y; --')


def test_a_standby_is_refused(monkeypatch):
    with pytest.raises(P.PostgresStoreError, match="standby"):
        _run(monkeypatch, _Conn(standby=True))
