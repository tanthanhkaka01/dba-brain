"""Reusing a database password ref that already exists, instead of writing over it.

`/spbot_create_db_docker` takes the DB password in two forms: a **ref** already in the encrypted
store, or a **value**. Sending `-` for the value means "I gave you a ref, reuse it". That path was
unreachable, and the way it failed named nothing about passwords:

* `--password-text-env DB_OPS_NEW_DB_PASSWORD` sat in the command's unconditional ``command_argv``
  while the env var was fed from ``password_text``. So a skipped password arrived as a variable
  holding the literal ``-``, and the flag looked entirely legitimate.
* With a new ref, the store then held a password of ``-``, which SQL Server rejected for failing
  its password policy — after the ref had already been created and deployed.
* With an existing ref, ``set_secret_text`` refused: *already exists with a different value*. The
  operator asking to reuse a ref was told the ref was in the way.

Both are the same gap, and the SSH password beside it never had it: ``remote_password_text`` was
already conditional. These tests hold the two halves of the fix together — the command must not
pass the flag when the value is skipped, and the CLI must never treat the sentinel as a password
however it arrives.
"""

import argparse
import json

import pytest

from db_ops.sre.cli import SKIP_SENTINEL, ProvisionError, _supplied_password_text
from db_ops.telegram.command_processor import DEFAULT_COMMANDS_PATH, build_cli_argv


def _config():
    data = json.loads(DEFAULT_COMMANDS_PATH.read_text(encoding="utf-8-sig"))
    command = next(c for c in data["telegram_support_commands"]
                   if c["command_text"] == "spbot_create_db_docker")
    return command["action_config"]


def _values(**overrides):
    """The exact message that failed on 2026-08-10, as parameter values."""
    values = {
        "python": "python", "config_path": "config.json",
        "name": "MSSQL25", "engine": "mssql", "version": "2025-latest", "mode": "single",
        "host_port": "1433",
        "password_env": "MSSQL_192_0_2_11_1433_SA", "password_text": SKIP_SENTINEL,
        "deploy_target": "192.0.2.11", "remote_user": "dev",
        "remote_password_ref": "REMOTE_192_0_2_11_DEV", "remote_password_text": "-",
        "remote_key_name": "-", "recreate": "no", "install_docker": "yes",
    }
    values.update(overrides)
    return values


def _args(**overrides):
    values = {"password_text": None, "password_text_env": None}
    values.update(overrides)
    return argparse.Namespace(**values)


# --------------------------------------------------------------------------------------
# The command definition
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# The CLI, for any caller that still sends the sentinel
# --------------------------------------------------------------------------------------

def test_the_sentinel_inside_the_environment_variable_is_not_a_password(monkeypatch):
    """The exact shape that stored a password of '-'. A worker running the old command definition
    still sends this, so the CLI has to recognise it on its own."""
    monkeypatch.setenv("DB_OPS_NEW_DB_PASSWORD", SKIP_SENTINEL)

    assert _supplied_password_text(_args(password_text_env="DB_OPS_NEW_DB_PASSWORD")) is None


def test_the_sentinel_on_the_flag_is_not_a_password():
    assert _supplied_password_text(_args(password_text=SKIP_SENTINEL)) is None


def test_no_password_arguments_at_all_means_reuse_the_ref():
    assert _supplied_password_text(_args()) is None


def test_a_real_value_is_taken_from_the_environment(monkeypatch):
    monkeypatch.setenv("DB_OPS_NEW_DB_PASSWORD", "Str0ng#Pass!")

    assert _supplied_password_text(
        _args(password_text_env="DB_OPS_NEW_DB_PASSWORD")) == "Str0ng#Pass!"


def test_a_sentinel_flag_does_not_veto_a_real_value_in_the_environment(monkeypatch):
    """The two spellings are checked in order, not exclusively: `-` on the flag means only that
    the flag carried nothing."""
    monkeypatch.setenv("DB_OPS_NEW_DB_PASSWORD", "Str0ng#Pass!")

    assert _supplied_password_text(
        _args(password_text=SKIP_SENTINEL,
              password_text_env="DB_OPS_NEW_DB_PASSWORD")) == "Str0ng#Pass!"


def test_a_named_but_genuinely_empty_variable_is_still_an_error(monkeypatch):
    """A caller who named a variable meant to set a password and lost it somewhere. Downgrading
    that to "reuse whatever is stored" would provision a database with a password nobody chose."""
    monkeypatch.delenv("DB_OPS_NEW_DB_PASSWORD", raising=False)

    with pytest.raises(ProvisionError, match="empty"):
        _supplied_password_text(_args(password_text_env="DB_OPS_NEW_DB_PASSWORD"))


def test_surrounding_whitespace_does_not_turn_the_sentinel_into_a_password(monkeypatch):
    monkeypatch.setenv("DB_OPS_NEW_DB_PASSWORD", f"  {SKIP_SENTINEL}  ")

    assert _supplied_password_text(_args(password_text_env="DB_OPS_NEW_DB_PASSWORD")) is None
