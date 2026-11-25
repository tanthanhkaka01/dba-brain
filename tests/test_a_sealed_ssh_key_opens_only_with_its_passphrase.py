"""An SSH private key is kept sealed: encrypted in OpenSSH's format, its passphrase a secret (0.42.0, 1.410).

Read on the worker on 2026-11-20: four private keys lay unencrypted in `data/ssh_keys/` at 644/664,
open to any login on the host and to any copy of the folder (plan
`audits/20261120_plan_worker_secret_exposure.md`, F4). The operator chose an encrypted file: each
key stays a file standard tools read, its random passphrase is in the encrypted secret store, and
`passphrases.json` beside the keys names the ref. These tests hold:

- `seal-ssh-key` leaves a key that opens with its passphrase only, to the same public key, after the
  passphrase is stored and read back - and refuses, changing nothing, a key already encrypted or a
  ref that holds another value; the passphrase is in no answer;
- a login that names a sealed key carries its passphrase to paramiko - through the app-side
  resolution and every common builder - and a sealed key without one says what is missing;
- the master logs into the worker with the key a worker node names in `config.json`.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from dbabrain.common import cli as common_cli
from dbabrain.common import ssh_key_seal
from dbabrain.common.ssh import open_key_file
from dbabrain.lib import secret_text
from dbabrain.lib.data_sources import ssh_auth
from dbabrain.lib.ssh_errors import SshError

STORE_KEY = "node passphrase #1"

#: Sealing a key and opening it are the [ssh] extra's (paramiko brings bcrypt); the core install `ci` tests
#: has neither.
needs_ssh = pytest.mark.skipif(importlib.util.find_spec("paramiko") is None, reason="the [ssh] extra is not installed")


@pytest.fixture
def node(tmp_path, monkeypatch):
    data = tmp_path / "data"
    keys = data / "ssh_keys"
    keys.mkdir(parents=True)
    (data / "encrypted_secret_text.json").write_text(
        json.dumps(secret_text.encrypt_secret_text({"OTHER": "x"}, STORE_KEY, iterations=1000)), encoding="utf-8")
    monkeypatch.setenv("DBABRAIN_SECRET_KEY", STORE_KEY)
    monkeypatch.setattr("dbabrain.lib.paths.DEFAULT_DATA_DIR", data)
    return data, keys


def _rsa_pem(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                       serialization.NoEncryption()))
    return ssh_key_seal.public_line(key)


def _seal(request, capsys):
    code = common_cli.main(["seal-ssh-key", json.dumps(request)])
    answer = json.loads(capsys.readouterr().out)
    return code, answer


@needs_ssh
def test_a_key_is_sealed_after_its_passphrase_is_stored_and_opens_only_with_it(node, capsys):
    import paramiko

    data, keys = node
    path = keys / "repo.key"
    public = _rsa_pem(path)

    code, answer = _seal({"key_file": str(path), "passphrase_ref": "SSH_KEY_REPO"}, capsys)

    assert code == 0 and answer["success"] is True
    passphrase = secret_text.load_secret_text(data, key=STORE_KEY)["SSH_KEY_REPO"]
    assert passphrase and passphrase not in json.dumps(answer)
    assert b"BEGIN OPENSSH PRIVATE KEY" in path.read_bytes()
    with pytest.raises(paramiko.PasswordRequiredException):
        open_key_file(str(path), None)
    assert open_key_file(str(path), passphrase).get_base64() == public.split()[1]
    registry = json.loads((keys / "passphrases.json").read_text(encoding="utf-8"))
    assert registry["repo.key"]["passphrase_ref"] == "SSH_KEY_REPO"
    assert answer["data"]["fingerprint"].startswith("SHA256:")


@needs_ssh
def test_a_key_already_encrypted_is_refused_and_left_as_it_was(node, capsys):
    _data, keys = node
    path = keys / "repo.key"
    _rsa_pem(path)
    _seal({"key_file": str(path), "passphrase_ref": "SSH_KEY_REPO"}, capsys)
    before = path.read_bytes()

    code, answer = _seal({"key_file": str(path), "passphrase_ref": "SSH_KEY_REPO_2"}, capsys)

    assert code != 0 and "encrypted already" in answer["error"]
    assert path.read_bytes() == before


def test_a_ref_that_holds_another_value_is_refused_and_the_key_is_not_touched(node, capsys):
    _data, keys = node
    path = keys / "repo.key"
    _rsa_pem(path)
    before = path.read_bytes()

    code, _answer = _seal({"key_file": str(path), "passphrase_ref": "OTHER"}, capsys)

    assert code != 0 and path.read_bytes() == before
    assert not (keys / "passphrases.json").exists()


@needs_ssh
def test_a_new_pair_is_generated_sealed_and_never_over_a_file(node, capsys):
    import paramiko

    data, keys = node
    path = keys / "worker.key"
    code, answer = _seal({"key_file": str(path), "passphrase_ref": "SSH_KEY_WORKER", "generate": True,
                          "comment": "dbabrain@master"}, capsys)

    assert code == 0 and answer["data"]["generated"] is True
    public = (keys / "worker.key.pub").read_text(encoding="utf-8").strip()
    assert public.startswith("ssh-ed25519 ") and public.endswith(" dbabrain@master")
    passphrase = secret_text.load_secret_text(data, key=STORE_KEY)["SSH_KEY_WORKER"]
    assert open_key_file(str(path), passphrase).get_base64() == public.split()[1]

    code, answer = _seal({"key_file": str(path), "passphrase_ref": "SSH_KEY_WORKER_2", "generate": True}, capsys)
    assert code != 0 and "never overwrites" in answer["error"]


def test_without_bcrypt_the_answer_names_the_extra_to_install(node, capsys, monkeypatch):
    """1.1.0, 1.422: it said cryptography's own *Need bcrypt module* (found by CI on the core install)."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives.serialization import ssh

    def no_bcrypt(*_a, **_k):
        raise UnsupportedAlgorithm("Need bcrypt module")

    monkeypatch.setattr(ssh, "_bcrypt_kdf", no_bcrypt)
    _data, keys = node
    path = keys / "repo.key"
    _rsa_pem(path)
    before = path.read_bytes()

    code, answer = _seal({"key_file": str(path), "passphrase_ref": "SSH_KEY_REPO"}, capsys)

    assert code != 0 and "[ssh]" in answer["error"] and "pip install" in answer["error"]
    assert path.read_bytes() == before


def test_a_name_instead_of_a_full_path_is_refused(node, capsys):
    code, answer = _seal({"key_file": "repo.key", "passphrase_ref": "SSH_KEY_REPO"}, capsys)
    assert code != 0 and "full path" in answer["error"]


# --------------------------------------------------------------------------- #
# A login that names a sealed key carries its passphrase
# --------------------------------------------------------------------------- #
@needs_ssh
def test_the_passphrase_is_found_by_the_key_and_an_unsealed_key_needs_none(node, capsys):
    data, keys = node
    sealed, plain = keys / "repo.key", keys / "plain.key"
    _rsa_pem(sealed)
    _rsa_pem(plain)
    _seal({"key_file": str(sealed), "passphrase_ref": "SSH_KEY_REPO"}, capsys)
    stored = secret_text.load_secret_text(data, key=STORE_KEY)["SSH_KEY_REPO"]

    assert ssh_auth.key_passphrase(sealed, data_dir=data) == stored
    assert ssh_auth.key_passphrase(plain, data_dir=data) == ""
    login = ssh_auth.ssh_login({"host": "192.0.2.5", "auth_type": "key", "key_file": "repo.key"},
                               {"username": "u"}, data_dir=data)
    assert login["key_passphrase"] == stored and login["key_file"] == str(sealed)


def test_a_sealed_key_whose_ref_is_gone_says_so(node):
    _data, keys = node
    (keys / "passphrases.json").write_text(json.dumps({"repo.key": {"passphrase_ref": "GONE"}}), encoding="utf-8")
    with pytest.raises(SshError, match="GONE"):
        ssh_auth.key_passphrase(keys / "repo.key", secrets={})


@needs_ssh
def test_paramiko_is_handed_the_passphrase_and_never_shown_it(monkeypatch):
    from dbabrain.common import remote_exec, ssh

    access = remote_exec.RemoteAccess.from_json({
        "method": "ssh", "host": "192.0.2.5", "username": "u", "auth_type": "key",
        "key_file": "/keys/repo.key", "key_passphrase": "s3aled", "platform": "linux"})
    assert access.key_passphrase == "s3aled" and "s3aled" not in repr(access)
    assert access.to_dict()["key_passphrase"] == "***"

    seen = {}

    class Client:
        def load_host_keys(self, *_a):
            pass

        def set_missing_host_key_policy(self, *_a):
            pass

        def connect(self, **kwargs):
            seen.update(kwargs)

    import paramiko

    monkeypatch.setattr(paramiko, "SSHClient", Client)
    ssh.open_ssh_client("192.0.2.5", "u", key_filename="/keys/repo.key", passphrase="s3aled", announce=False)
    assert seen["passphrase"] == "s3aled" and seen["key_filename"] == "/keys/repo.key"


@pytest.mark.parametrize("build", ["sqlcmd", "hostcmd", "remote_host"])
def test_every_builder_carries_the_passphrase_beside_the_key(build):
    login = {"host": "192.0.2.5", "port": 22, "username": "u", "key_file": "/keys/repo.key",
             "key_passphrase": "s3aled"}
    if build == "sqlcmd":
        from dbabrain.common.sqlcmd_run import ssh_access

        access = ssh_access(login)
    elif build == "hostcmd":
        from dbabrain.common import hostcmd

        access = hostcmd.access_for(hostcmd.parse_host({**login, "runtime": "linux"}))
    else:
        from dbabrain.lib.remote_host import RemoteHost

        access = RemoteHost(host="192.0.2.5", username="u", key_file="/keys/repo.key",
                            key_passphrase="s3aled", password="sudo-pw", call=None).access()
        assert access["auth_type"] == "key" and access["password"] == "sudo-pw"
    assert access["key_passphrase"] == "s3aled" and access["key_file"] == "/keys/repo.key"


@needs_ssh
def test_the_master_logs_into_the_worker_with_the_key_its_node_names(node, capsys, tmp_path, monkeypatch):
    from dbabrain.control import _support

    data, keys = node
    _seal({"key_file": str(keys / "worker.key"), "passphrase_ref": "SSH_KEY_WORKER", "generate": True}, capsys)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"worker": [{"node_id": "w", "host": "192.0.2.9", "user": "dev",
                                              "password_ref": "P", "key_file": "worker.key"}]}), encoding="utf-8")
    monkeypatch.setattr("dbabrain.lib.data_sources.DEFAULT_DATA_DIR", data)

    path, passphrase = _support.worker_key_login("192.0.2.9", config)
    assert path == str(keys / "worker.key")
    assert passphrase == secret_text.load_secret_text(data, key=STORE_KEY)["SSH_KEY_WORKER"]
    assert _support.worker_key_login("192.0.2.10", config) == ("", "")
