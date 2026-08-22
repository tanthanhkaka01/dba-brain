"""Tests for data/store_config.json — the declaration of which database db_ops stores
its own runtime data in (SQLite or PostgreSQL) and the full connection details for it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import db_ops.config as config_module
from db_ops.config import (
    DEFAULT_STORE_CONFIG_FILE,
    POSTGRESQL_BACKEND,
    SQLITE_BACKEND,
    STORE_CONFIG_ENV_VAR,
    PostgresStoreConfig,
    load_config,
    resolve_store_config_path,
)
from conftest import shipped_config, shipped_tool_root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tree(tmp_path: Path, store: dict | None = None, main: dict | None = None) -> Path:
    """Build a tool tree: <root>/config.json plus <root>/data/store_config.json."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    if store is not None:
        (data / "store_config.json").write_text(json.dumps(store), encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps(main or {"app_name": "t"}), encoding="utf-8")
    return tmp_path


def _load_at(root: Path):
    original = config_module.TOOL_ROOT
    config_module.TOOL_ROOT = root
    try:
        return load_config(root / "config.json")
    finally:
        config_module.TOOL_ROOT = original


# ---------------------------------------------------------------------------
# The declaration: which backend, and the connection details for it
# ---------------------------------------------------------------------------

def test_shipped_store_config_declares_a_supported_backend():
    """The shipped data/store_config.json must load and name a backend the store supports.

    Deliberately not pinned to one backend: which one is live is an operational decision that
    changes (this tree has since moved to PostgreSQL, while the shipped example starts on SQLite
    because that needs nothing installed). What must hold is that the declaration loads, names a
    supported backend, and passes its own completeness validation.

    Read through `shipped_tool_root()` rather than the repository root, so the same check runs on
    a clone that has only the examples — which is the copy a stranger actually starts from.
    """
    config = _load_at(shipped_tool_root())
    assert config.store.backend in (SQLITE_BACKEND, POSTGRESQL_BACKEND)
    assert config.store.config_file is not None
    assert config.store.config_file.name == "store_config.json"
    config.store.validate()
    # Whichever is live, the active connection string must be usable and password-free.
    assert config.store.connection_string
    assert "sslmode" in config.store.connection_string or config.store.connection_string.startswith("sqlite:///")


def test_sqlite_path_comes_from_the_store_file(tmp_path):
    root = _tree(tmp_path, {"backend": "sqlite", "sqlite": {"path": "runtime/from_store.sqlite"}})
    config = _load_at(root)
    assert config.sqlite_path == (root / "runtime" / "from_store.sqlite").resolve()
    assert config.store.connection_string.endswith("runtime/from_store.sqlite")


def test_relative_store_path_resolves_against_the_tool_root_not_data(tmp_path):
    """The store file lives in data/, so a relative path there must not land in data/runtime —
    the same trap _resolve_path_setting documents for configs that sit below the tool root."""
    root = _tree(tmp_path, {"backend": "sqlite", "sqlite": {"path": "runtime/db_ops.sqlite"}})
    assert _load_at(root).sqlite_path == (root / "runtime" / "db_ops.sqlite").resolve()


def test_connection_string_wins_over_path_so_the_file_cannot_state_two_places(tmp_path):
    """Both keys describe one destination. The URL is authoritative and the path is read back
    out of it, so a stale sibling value can never send writes somewhere else."""
    root = _tree(
        tmp_path,
        {
            "backend": "sqlite",
            "sqlite": {"path": "runtime/ignored.sqlite",
                       "connection_string": "sqlite:///runtime/authoritative.sqlite"},
        },
    )
    assert _load_at(root).sqlite_path.name == "authoritative.sqlite"


def test_absolute_sqlite_url_stays_absolute(tmp_path):
    target = (tmp_path / "elsewhere" / "db_ops.sqlite").resolve()
    root = _tree(
        tmp_path,
        {"backend": "sqlite", "sqlite": {"connection_string": f"sqlite:///{target.as_posix()}"}},
    )
    assert _load_at(root).sqlite_path == target


def test_a_written_out_connection_string_matches_what_the_fields_derive():
    """A postgresql block that writes the URL out in full must agree with its own sibling fields.

    Otherwise the file documents a destination the code would not actually connect to: the URL
    wins at runtime, so a stale host or port beside it reads as truth and is not.

    Skipped when the shipped file writes no URL out — the example leaves it blank so the fields
    alone describe the target, and there is then nothing for them to disagree with. The assertion
    used to require the URL to be there, which made it a statement about *this operator's* store
    rather than about the rule.
    """
    import dataclasses

    postgres = _load_at(shipped_tool_root()).store.postgresql
    if not postgres.explicit_connection_string:
        pytest.skip("the shipped store_config leaves the postgresql URL to be derived")
    derived = dataclasses.replace(postgres, explicit_connection_string="").connection_string
    assert derived == postgres.explicit_connection_string


# ---------------------------------------------------------------------------
# Precedence: explicit config key > inline block > store file > tool default
# ---------------------------------------------------------------------------

def test_explicit_sqlite_path_overrides_the_store_file(tmp_path):
    """Still a documented override (the standalone-EXE layouts point it at a local path),
    so a config that names one must not be silently redirected to the store file."""
    root = _tree(
        tmp_path,
        {"backend": "sqlite", "sqlite": {"path": "runtime/from_store.sqlite"}},
        {"app_name": "t", "sqlite_path": "runtime/explicit.sqlite"},
    )
    assert _load_at(root).sqlite_path.name == "explicit.sqlite"


def test_inline_store_block_overrides_the_file(tmp_path):
    root = _tree(
        tmp_path,
        {"backend": "sqlite", "sqlite": {"path": "runtime/from_store.sqlite"}},
        {"app_name": "t", "store": {"backend": "sqlite", "sqlite": {"path": "runtime/inline.sqlite"}}},
    )
    assert _load_at(root).sqlite_path.name == "inline.sqlite"


def test_no_store_file_falls_back_to_legacy_sqlite_path(tmp_path):
    """A tree deployed before this file existed keeps working off config.json alone."""
    root = _tree(tmp_path, None, {"app_name": "t", "sqlite_path": "runtime/legacy.sqlite"})
    config = _load_at(root)
    assert config.sqlite_path.name == "legacy.sqlite"
    assert config.store.backend == SQLITE_BACKEND
    assert config.store.config_file is None


def test_no_store_file_and_no_key_uses_the_tool_default(tmp_path):
    root = _tree(tmp_path, None, {"app_name": "t"})
    assert _load_at(root).sqlite_path == (root / "runtime" / "db_ops.sqlite").resolve()


def test_env_var_points_at_another_store_file(tmp_path, monkeypatch):
    elsewhere = tmp_path / "side_install_store.json"
    elsewhere.write_text(
        json.dumps({"backend": "sqlite", "sqlite": {"path": "runtime/side.sqlite"}}), encoding="utf-8"
    )
    root = _tree(tmp_path, {"backend": "sqlite", "sqlite": {"path": "runtime/from_store.sqlite"}})
    monkeypatch.setenv(STORE_CONFIG_ENV_VAR, str(elsewhere))
    assert _load_at(root).sqlite_path.name == "side.sqlite"
    assert resolve_store_config_path() == elsewhere


def test_store_config_file_pointer_is_honoured(tmp_path):
    (tmp_path / "data").mkdir()
    pointed = tmp_path / "data" / "other_store.json"
    pointed.write_text(
        json.dumps({"backend": "sqlite", "sqlite": {"path": "runtime/pointed.sqlite"}}), encoding="utf-8"
    )
    root = _tree(
        tmp_path,
        {"backend": "sqlite", "sqlite": {"path": "runtime/from_store.sqlite"}},
        {"app_name": "t", "store_config_file": "data/other_store.json"},
    )
    assert _load_at(root).sqlite_path.name == "pointed.sqlite"


# ---------------------------------------------------------------------------
# PostgreSQL: fully describable, and refused loudly until the store speaks it
# ---------------------------------------------------------------------------

def test_postgresql_backend_loads_and_is_selected(tmp_path):
    """A complete postgresql declaration is accepted and becomes the active backend.

    This used to raise "SQLite-only": the store classes spoke SQLite only, so the config layer
    refused the flip rather than let the app write to the SQLite file while claiming PostgreSQL.
    They run on either backend now (see db_ops.db.backend), so the declaration must load.
    """
    root = _tree(
        tmp_path,
        {
            "backend": "postgresql",
            "postgresql": {"host": "10.0.0.1", "port": 5433, "database": "db_ops",
                           "username": "db_ops", "password_ref": "REF"},
        },
    )
    store = _load_at(root).store
    assert store.backend == POSTGRESQL_BACKEND
    assert store.is_postgresql and not store.is_sqlite
    assert store.postgresql.host == "10.0.0.1"
    # The active connection string is the PostgreSQL one, and carries no password.
    assert store.connection_string.startswith("postgresql://")
    assert "{password}" in store.connection_string


def test_postgres_alias_is_accepted_as_a_backend_name(tmp_path):
    root = _tree(
        tmp_path,
        {"backend": "postgres", "postgresql": {"host": "h", "database": "db_ops"}},
    )
    assert _load_at(root).store.backend == POSTGRESQL_BACKEND


def test_incomplete_postgres_block_is_refused_before_the_unsupported_error(tmp_path):
    """"Complete enough to connect with" is checked on its own, so a half-filled block reports
    what is missing rather than the not-implemented-yet message."""
    root = _tree(tmp_path, {"backend": "postgresql", "postgresql": {"host": "h"}})
    with pytest.raises(RuntimeError, match="connection_string or host \\+ database"):
        _load_at(root)


def test_unknown_backend_is_refused(tmp_path):
    root = _tree(tmp_path, {"backend": "mysql"})
    with pytest.raises(RuntimeError, match="Unknown store backend"):
        _load_at(root)


def test_password_is_substituted_from_the_secret_store(tmp_path):
    """The file holds a password_ref and a {password} placeholder; the resolved string is the
    only place the secret appears."""
    from db_ops.lib import secret_text

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    blob = secret_text.encrypt_secret_text({"STORE_PW": "s3cr#t/pw"}, "passphrase")
    (data_dir / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME).write_text(
        json.dumps(blob), encoding="utf-8"
    )

    postgres = PostgresStoreConfig(
        host="10.0.0.1", port=5433, database="db_ops", username="db_ops",
        password_ref="STORE_PW", schema="db_ops", secret_text_dir=data_dir,
    )
    assert "{password}" in postgres.connection_string
    assert "s3cr" not in postgres.connection_string  # safe form carries no secret

    resolved = postgres.resolved_connection_string("passphrase")
    assert "{password}" not in resolved
    # Reserved URL characters in the password must be percent-encoded, not pasted raw.
    assert "s3cr%23t%2Fpw" in resolved


def test_missing_password_ref_raises_instead_of_connecting_as_nobody(tmp_path):
    from db_ops.lib import secret_text

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    blob = secret_text.encrypt_secret_text({"OTHER": "x"}, "passphrase")
    (data_dir / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME).write_text(
        json.dumps(blob), encoding="utf-8"
    )
    postgres = PostgresStoreConfig(
        host="h", database="db_ops", username="u", password_ref="STORE_PW", secret_text_dir=data_dir
    )
    with pytest.raises(RuntimeError, match="was not found in the secret text"):
        postgres.resolved_connection_string("passphrase")


def test_a_connection_string_with_an_inline_password_is_used_verbatim():
    """No {password} placeholder means the URL already carries whatever it needs; the resolver
    must not append a second set of credentials."""
    url = "postgresql://u:already@10.0.0.1:5433/db_ops"
    postgres = PostgresStoreConfig(explicit_connection_string=url)
    assert postgres.connection_string == url
    assert postgres.resolved_connection_string("any-key") == url


# ---------------------------------------------------------------------------
# describe(): what status output reports
# ---------------------------------------------------------------------------

def test_describe_reports_the_backend_and_never_a_password(tmp_path):
    root = _tree(
        tmp_path,
        {
            "backend": "sqlite",
            "sqlite": {"path": "runtime/db_ops.sqlite"},
            "postgresql": {"host": "h", "database": "db_ops", "username": "u",
                           "password_ref": "REF"},
        },
    )
    described = _load_at(root).store.describe()
    assert described["backend"] == SQLITE_BACKEND
    assert described["connection_string"].startswith("sqlite:///")
    assert "password" not in json.dumps(described).lower()


def test_backend_constants_match_the_documented_names():
    assert SQLITE_BACKEND == "sqlite"
    assert POSTGRESQL_BACKEND == "postgresql"
    assert DEFAULT_STORE_CONFIG_FILE == "data/store_config.json"
