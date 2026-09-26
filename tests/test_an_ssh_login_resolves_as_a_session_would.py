"""An SSH login read in `lib` is the login a `common` session opens - and only `lib` reads it.

`sre` turns a `cmd_access` block into the login a `common.cli move-db-docker` request carries. It
did that with `common.remote_exec.RemoteAccess` until 0.24.0 - an app importing `common` (rules
R03) - and now asks `lib.data_sources.ssh_login`. Two readers of one block is how two
interpretations start, so this held them to each other on every way a login can be written.

Since 0.24.0 there is one reader: `remote_exec` finds no key under `data/ssh_keys/` and opens no
secret store (rules R09). What is held now is the hand-over - the login `ssh_login` resolves, given
to a session, is the login the session opens, on every way a block can be written - and that the
session refuses, in words, to do the reading itself.
"""

from __future__ import annotations

import pytest

from db_ops.common.remote_exec import RemoteAccess, RemoteExecError
from db_ops.lib.data_sources import ssh_login
from db_ops.lib.secret_value import SecretValueError
from db_ops.lib.ssh_errors import SshError


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    keys = tmp_path / "ssh_keys"
    keys.mkdir()
    (keys / "lab.key").write_text("not a real key", encoding="utf-8")
    (tmp_path / "absolute.key").write_text("not a real key either", encoding="utf-8")
    monkeypatch.setenv("LAB_OS_PASSWORD", "from-the-environment")
    monkeypatch.delenv("DB_OPS_SECRET_KEY", raising=False)
    return tmp_path


def _cases(data_dir):
    return {
        "a password in the credential": (
            {"method": "ssh", "host": "192.0.2.10", "auth_type": "password"},
            {"username": "ops", "password": "typed"}),
        "a password ref the environment answers": (
            {"method": "ssh", "host": "192.0.2.10", "auth_type": "password"},
            {"username": "ops", "password_ref": "LAB_OS_PASSWORD"}),
        "the block's own user wins over the credential's": (
            {"method": "ssh", "host": "192.0.2.10", "auth_type": "password", "username": "root"},
            {"username": "ops", "password": "typed"}),
        "a key by its bare name": (
            {"method": "ssh", "host": "192.0.2.10", "key_file": "lab.key", "port": "2222"},
            {"username": "ops"}),
        "a key by its absolute path": (
            {"method": "ssh", "host": "192.0.2.10", "auth_type": "key",
             "key_file": str(data_dir / "absolute.key")},
            {"username": "ops"}),
        "a key whose passphrase ref resolves to nothing": (
            {"method": "ssh", "host": "192.0.2.10", "key_file": "lab.key"},
            {"username": "ops", "password_ref": "NOT_ANYWHERE"}),
        "the agent's keys - no key file at all": (
            {"method": "ssh", "host": "192.0.2.10"}, {"username": "ops"}),
    }


@pytest.mark.parametrize("case", list(_cases(__import__("pathlib").Path("."))))
def test_the_login_lib_resolves_is_the_login_a_session_opens(data_dir, case):
    block, credential = _cases(data_dir)[case]
    login = ssh_login(block, credential, data_dir=data_dir)

    session = RemoteAccess.from_json(
        {"method": "ssh", "auth_type": block.get("auth_type") or "key", **login})

    assert login == {"host": session.host, "port": session.port, "username": session.username,
                     "password": session.password, "key_file": session.key_file or ""}


def test_a_key_name_is_found_by_the_reader_and_refused_by_the_session(data_dir):
    block = {"method": "ssh", "host": "192.0.2.10", "key_file": "gone.key"}
    with pytest.raises(SshError, match="SSH key not found"):
        ssh_login(block, {"username": "ops"}, data_dir=data_dir)
    with pytest.raises(RemoteExecError, match=r"data/ssh_keys/.*rules R09"):
        RemoteAccess.from_json({**block, "key_file": "lab.key"}, credential={"username": "ops"})


def test_a_ref_nothing_holds_is_looked_for_in_the_store_by_the_reader_only(data_dir):
    block = {"method": "ssh", "host": "192.0.2.10", "auth_type": "password"}
    credential = {"username": "ops", "password_ref": "NOT_ANYWHERE"}
    with pytest.raises(SecretValueError, match="secret store: NOT_ANYWHERE"):
        ssh_login(block, credential, data_dir=data_dir)
    with pytest.raises(RemoteExecError, match="not among the secrets stated with it"):
        RemoteAccess.from_json(block, credential=credential)
