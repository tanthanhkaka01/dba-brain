"""SSH host keys are pinned at first use, and a changed one refuses (0.35.0, 1.223 - A3.3).

security.md 5a listed it as not yet defended: every SSH hop accepted whatever key answered, so a
machine put at a host's address received the password or ran the commands. With
``DBABRAIN_SSH_KNOWN_HOSTS`` naming a file, ``common.ssh.open_ssh_client`` loads it, writes a host's
key there the first time it is seen, and refuses one that differs (``SshHostKeyChanged``, a
refusal, before any credential is sent). Unset, nothing changes: like ``sqlserver_tls_verify`` it is
the deployment's switch, so a lab estate keeps connecting.
"""

from __future__ import annotations

import sys
import types

import pytest

from dbabrain.common import ssh
from dbabrain.lib import errors


class _Client:
    def __init__(self):
        self.loaded = []
        self.policy = None

    def load_host_keys(self, path):
        self.loaded.append(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        if _FAKE.raise_on_connect is not None:
            raise _FAKE.raise_on_connect


class _BadHostKey(Exception):
    pass


_FAKE = types.SimpleNamespace(raise_on_connect=None)


@pytest.fixture
def paramiko(monkeypatch):
    clients = []
    module = types.SimpleNamespace(
        SSHClient=lambda: clients.append(_Client()) or clients[-1], AutoAddPolicy=lambda: "auto-add",
        BadHostKeyException=_BadHostKey, AuthenticationException=type("Auth", (Exception,), {}))
    monkeypatch.setitem(sys.modules, "paramiko", module)
    _FAKE.raise_on_connect = None
    return clients


def test_unset_every_key_is_accepted_as_before(monkeypatch, paramiko):
    monkeypatch.delenv(ssh.KNOWN_HOSTS_ENV, raising=False)
    ssh.open_ssh_client("192.0.2.10", "dev", password="x", announce=False)
    assert paramiko[0].loaded == [] and paramiko[0].policy == "auto-add"


def test_set_the_file_is_made_and_loaded_so_a_first_key_is_saved_into_it(monkeypatch, paramiko, tmp_path):
    pinned = tmp_path / "ssh" / "known_hosts"
    monkeypatch.setenv(ssh.KNOWN_HOSTS_ENV, str(pinned))
    ssh.open_ssh_client("192.0.2.10", "dev", password="x", announce=False)
    assert pinned.exists() and paramiko[0].loaded == [str(pinned)]


def test_a_changed_key_is_a_refusal_naming_the_file(monkeypatch, paramiko, tmp_path):
    monkeypatch.setenv(ssh.KNOWN_HOSTS_ENV, str(tmp_path / "known_hosts"))
    _FAKE.raise_on_connect = _BadHostKey("key mismatch")
    with pytest.raises(ssh.SshHostKeyChanged) as raised:
        ssh.open_ssh_client("192.0.2.10", "dev", password="x", announce=False)
    assert isinstance(raised.value, errors.Refused) and isinstance(raised.value, ssh.SshError)
    assert ssh.KNOWN_HOSTS_ENV in str(raised.value) and "Nothing was sent" in str(raised.value)
