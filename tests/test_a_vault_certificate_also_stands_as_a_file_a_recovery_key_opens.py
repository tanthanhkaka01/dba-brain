"""Each proven vault row also stands as ONE sealed file that an offline recovery key opens (0.37.0).

Why it matters: the operator asked for the certificate as a physical file as well as in the vault,
and what happens when the secret store's passphrase is lost. The vault row and the ``.pvk``
password are both behind that passphrase - lose it and every natively encrypted backup is lost with
it. So the file carries everything a restore needs (both files and the ``.pvk`` password), is sealed
with ``age`` for dbabrain AND at least one recovery key kept off every dbabrain machine, is written
once and never overwritten, and a server's copy is deleted only when the row's file stands and
proves. Real age encryption here (``pyrage``), no fake.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pyrage = pytest.importorskip("pyrage")

from dbabrain.backup_restore import certificate_files as F  # noqa: E402
from dbabrain.lib import vault_file as V  # noqa: E402

CER, PVK = b"\x30\x82 DER certificate bytes", b"\x1e\xf1\xb5\xb0 PVK private key bytes"


def _row(**over):
    row = {"certificate_id": 7, "server_id": "SRV-01", "certificate_name": "dbabrain_backup_cert",
           "thumbprint": hashlib.sha1(CER).hexdigest(),  # noqa: S324
           "cer_file_name": "dbabrain_backup_cert.cer", "cer_size_bytes": len(CER),
           "cer_sha256": hashlib.sha256(CER).hexdigest(),
           "pvk_file_name": "dbabrain_backup_cert.pvk", "pvk_size_bytes": len(PVK),
           "pvk_sha256": hashlib.sha256(PVK).hexdigest(), "password_ref": "ENC_REF",
           "taken_at": "2026-11-10T05:00:00Z", "backup_id": "B1", "verified_at": "2026-11-10T05:00:01Z"}
    row.update(over)
    return row


class _Vault:
    def __init__(self, rows):
        self.rows = rows

    def listing(self):
        return [dict(r) for r in self.rows]

    def pair(self, certificate_id, *, passphrase):
        assert passphrase == "store passphrase"
        return CER, PVK, next(dict(r) for r in self.rows if r["certificate_id"] == certificate_id)


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A node whose store_config.json configures the files, a dbabrain identity, a recovery key."""
    identity, _ = V.new_identity()
    recovery, recovery_public = V.new_identity()
    store_config = tmp_path / "store_config.json"
    store_config.write_text(json.dumps({"backend": "sqlite", V.BLOCK: {
        "file_folder": str(tmp_path / "vault"), "identity_ref": "VAULT_ID",
        "recovery_recipients": [recovery_public]}}), encoding="utf-8")
    app_config = SimpleNamespace(store=SimpleNamespace(config_file=store_config))
    secrets = {"VAULT_ID": identity, "ENC_REF": "the pvk password"}
    return SimpleNamespace(app_config=app_config, secrets=secrets, identity=identity, recovery=recovery,
                           folder=tmp_path / "vault", store_config=store_config)


def _seal(node, row=None, *, apply=True):
    row = row or _row()
    return F.seal_row(node.app_config, row, passphrase="store passphrase", secrets=node.secrets, apply=apply,
                      vault=_Vault([row]))


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #
def test_the_file_is_named_by_server_certificate_and_thumbprint(node):
    answer = _seal(node)
    path = Path(answer["path"])
    assert answer["status"] == "written" and answer["recipients"] == 2
    assert path == node.folder / "SRV-01" / f"dbabrain_backup_cert_{_row()['thumbprint']}.vault.age"


def test_the_recovery_key_alone_opens_it_and_finds_everything_a_restore_needs(node):
    """The passphrase and the secret store gone: the offline key and the file are enough."""
    path = Path(_seal(node)["path"])

    content = V.unseal(path.read_bytes(), node.recovery)

    assert base64.b64decode(content["cer"]["base64"]) == CER
    assert base64.b64decode(content["pvk"]["base64"]) == PVK
    assert content["pvk_password"] == "the pvk password"
    assert content["thumbprint"] == hashlib.sha1(CER).hexdigest()  # noqa: S324
    assert V.problems(content, _row()) == []


def test_the_file_is_standard_age_that_dbabrains_identity_opens_too(node):
    path = Path(_seal(node)["path"])
    assert path.read_bytes().startswith(b"age-encryption.org/v1\n")
    assert V.unseal(path.read_bytes(), node.identity)["certificate_id"] == 7


def test_a_key_that_is_neither_cannot_open_it(node):
    path = Path(_seal(node)["path"])
    stranger, _ = V.new_identity()
    with pytest.raises(V.VaultFileError, match="cannot be opened"):
        V.unseal(path.read_bytes(), stranger)


# --------------------------------------------------------------------------- #
# Opening it: the right key, an old key, a wrong key, no key, no age, a damaged file
# --------------------------------------------------------------------------- #
def test_an_old_key_opens_only_the_files_sealed_for_it(node):
    """A key replaced later still opens what was sealed for it - and nothing sealed after it."""
    old_path = Path(_seal(node)["path"])
    new_identity, _ = V.new_identity()
    node.secrets["VAULT_ID"] = new_identity
    newer = _row(certificate_id=9, thumbprint=_row()["thumbprint"], certificate_name="next_cert")
    new_path = Path(_seal(node, newer)["path"])

    assert V.unseal(old_path.read_bytes(), node.identity)["certificate_id"] == 7
    with pytest.raises(V.VaultFileError, match="cannot be opened"):
        V.unseal(new_path.read_bytes(), node.identity)
    assert V.unseal(new_path.read_bytes(), new_identity)["certificate_id"] == 9
    # The recovery key was a recipient of both.
    assert V.unseal(old_path.read_bytes(), node.recovery)["certificate_id"] == 7
    assert V.unseal(new_path.read_bytes(), node.recovery)["certificate_id"] == 9


def test_the_proof_with_a_key_the_file_was_not_sealed_for_fails_and_rewrites_nothing(node):
    path = Path(_seal(node)["path"])
    before = path.read_bytes()
    node.secrets["VAULT_ID"] = V.new_identity()[0]

    with pytest.raises(V.VaultFileError, match="cannot be opened"):
        _seal(node)
    with pytest.raises(V.VaultFileError, match="cannot be opened"):
        F.require_proven_file(node.app_config, _row(), secrets=node.secrets)
    assert path.read_bytes() == before


@pytest.mark.parametrize("identity, words", [
    ("", "no identity given"),
    (None, "no identity given"),
    ("AGE-SECRET-KEY-1TOOSHORT", "not an age identity"),
    ("age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq", "not an age identity"),
])
def test_no_key_or_a_key_that_is_not_one_is_refused_before_anything_is_read(node, identity, words):
    path = Path(_seal(node)["path"])
    with pytest.raises(V.VaultFileError, match=words):
        V.unseal(path.read_bytes(), identity)


def test_no_identity_in_the_secret_store_refuses_by_name(node):
    _seal(node)
    node.secrets.pop("VAULT_ID")
    with pytest.raises(F.VaultFileRefused, match="no VAULT_ID"):
        F.require_proven_file(node.app_config, _row(), secrets=node.secrets)
    with pytest.raises(F.VaultFileRefused, match="no VAULT_ID"):
        _seal(node)


@pytest.mark.parametrize("damage", ["truncated", "flipped", "empty", "not_age", "plain_json"])
def test_a_damaged_or_foreign_file_is_never_taken_for_the_record(node, damage):
    path = Path(_seal(node)["path"])
    data = path.read_bytes()
    broken = {"truncated": data[: len(data) // 2],
              "flipped": data[:-20] + bytes([data[-20] ^ 0x01]) + data[-19:],
              "empty": b"",
              "not_age": b"PK\x03\x04 a zip, not an age file",
              "plain_json": json.dumps(V.payload(_row(), cer=CER, pvk=PVK, pvk_password="pw")).encode()}[damage]
    with pytest.raises(V.VaultFileError):
        V.unseal(broken, node.identity)
    path.write_bytes(broken)
    with pytest.raises((V.VaultFileError, F.VaultFileRefused)):
        F.require_proven_file(node.app_config, _row(), secrets=node.secrets)


def test_without_the_age_library_nothing_is_sealed_or_opened_and_it_says_what_to_install(node, monkeypatch):
    import sys

    path = Path(_seal(node)["path"])
    monkeypatch.setitem(sys.modules, "pyrage", None)
    with pytest.raises(V.VaultFileError, match="pip install pyrage"):
        V.unseal(path.read_bytes(), node.identity)
    with pytest.raises(V.VaultFileError, match="pip install pyrage"):
        V.new_identity()
    with pytest.raises(V.VaultFileError, match="pip install pyrage"):
        _seal(node, _row(certificate_id=9, certificate_name="other"))
    assert not (node.folder / "SRV-01" / f"other_{_row()['thumbprint']}.vault.age").exists()


def test_nothing_secret_is_readable_in_the_file(node):
    data = Path(_seal(node)["path"]).read_bytes()
    for secret in (CER, PVK, b"the pvk password", base64.b64encode(PVK)):
        assert secret not in data


def test_a_file_already_there_is_proven_and_never_rewritten(node):
    path = Path(_seal(node)["path"])
    before = path.read_bytes()

    again = _seal(node)

    assert again["status"] == "proven" and path.read_bytes() == before


def test_a_file_that_does_not_prove_is_a_stop_not_a_rewrite(node):
    path = Path(_seal(node)["path"])
    other = V.seal(V.payload(_row(), cer=CER + b"x", pvk=PVK, pvk_password="pw"),
                   [V.recipient_of(node.identity), V.recipient_of(node.recovery)])
    path.write_bytes(other)

    with pytest.raises(F.VaultFileRefused, match="never overwritten"):
        _seal(node)
    assert path.read_bytes() == other


def test_a_dry_run_writes_nothing(node):
    answer = _seal(node, apply=False)
    assert answer["status"] == "missing" and not Path(answer["path"]).exists()


def test_an_unverified_row_is_not_written(node):
    with pytest.raises(F.VaultFileRefused, match="not verified"):
        _seal(node, _row(verified_at=None))


def test_a_file_without_the_pvk_password_is_never_written(node):
    node.secrets.pop("ENC_REF")
    with pytest.raises(F.VaultFileRefused, match="opens nothing"):
        _seal(node)
    assert not any(node.folder.rglob("*.age"))


def test_seal_all_writes_every_verified_row_and_skips_the_others(node):
    rows = [_row(), _row(certificate_id=8, verified_at=None)]
    answer = F.seal_all(node.app_config, passphrase="store passphrase", secrets=node.secrets, apply=True,
                        vault=_Vault(rows))
    assert answer["counts"] == {"written": 1, "skipped": 1} and answer["ok"]


# --------------------------------------------------------------------------- #
# The gate before a server's copy is deleted
# --------------------------------------------------------------------------- #
def test_the_gate_refuses_until_the_file_stands_and_proves(node):
    with pytest.raises(F.VaultFileRefused, match="not written"):
        F.require_proven_file(node.app_config, _row(), secrets=node.secrets)
    path = _seal(node)["path"]
    assert F.require_proven_file(node.app_config, _row(), secrets=node.secrets) == path


def test_the_gate_refuses_when_the_files_are_not_configured(tmp_path):
    store_config = tmp_path / "store_config.json"
    store_config.write_text(json.dumps({"backend": "sqlite"}), encoding="utf-8")
    app_config = SimpleNamespace(store=SimpleNamespace(config_file=store_config))
    assert F.settings(app_config) is None
    with pytest.raises(F.VaultFileRefused, match="not configured"):
        F.require_proven_file(app_config, _row(), secrets={})


def test_take_certificate_deletes_nothing_without_the_file(node, monkeypatch):
    from dbabrain.backup_restore import certificate_take as T

    sent = []
    monkeypatch.setattr(T.common_cli, "run_allowing_failure", lambda op, req, **_k: sent.append(op) or (True, {}, ""))
    monkeypatch.setattr("dbabrain.db.lockdown_store.refuse_if_locked", lambda *_a, **_k: None)
    monkeypatch.setattr(F, "seal_row", lambda *a, **k: {"path": "x", "status": "written"})
    _wire_take(T, monkeypatch)

    with pytest.raises(T.TakeRefused, match="nothing deleted"):
        T.take_certificate(app_config=node.app_config, config_path="c", backup_id="B1", passphrase="p",
                           apply=True, delete_at_source=True, vault=_ProvingVault())
    assert "delete-verified-file" not in sent


class _ProvingVault:
    def keep(self, **_k):
        return {**_row(), "already_kept": True}

    def prove(self, certificate_id, *, passphrase):
        return _row()


def _wire_take(T, monkeypatch):
    job = SimpleNamespace(backup_id="B1", server_id="SRV-01", db_type="sqlserver", backup_dir="D:\\SQLBK",
                          env={}, env_secrets={"BACKUP_ENCRYPTION_PASSWORD": "ENC_REF"})
    monkeypatch.setattr(T, "load_backup_jobs", lambda _p: [job])
    monkeypatch.setattr(T, "resolve_backup_target", lambda j, **_k: SimpleNamespace(platform="windows"))
    monkeypatch.setattr(T, "_load_secrets", lambda **_k: {"ENC_REF": "pw"})
    monkeypatch.setattr("dbabrain.backup_restore.spec_builder.host_block", lambda *_a, **_k: {"runtime": "windows"})
    folder = "D:\\SQLBK\\_cert\\"

    def run(op, request, **_k):
        if op == "file-checksum":
            return {"files": [{"path": folder + "dbabrain_backup_cert.cer", "sha256": hashlib.sha256(CER).hexdigest(),
                               "size_bytes": len(CER)},
                              {"path": folder + "dbabrain_backup_cert.pvk", "sha256": hashlib.sha256(PVK).hexdigest(),
                               "size_bytes": len(PVK)}]}
        data = CER if request["path"].endswith(".cer") else PVK
        return {"base64": base64.b64encode(data).decode()}
    monkeypatch.setattr(T.common_cli, "run", run)


# --------------------------------------------------------------------------- #
# Settings and keys
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, words", [
    ("x", "must be an object"),
    ({"file_folder": "v", "identity_ref": "I"}, "recovery_recipients must be stated"),
    ({"file_folder": "v", "identity_ref": "I", "recovery_recipients": []}, "at least one"),
    ({"file_folder": "v", "identity_ref": "I", "recovery_recipients": ["age1short"]}, "not an age public key"),
    ({"file_folder": "", "identity_ref": "I", "recovery_recipients": ["x"]}, "file_folder"),
    ({"file_folder": "v", "identity_ref": "I", "recovery_recipients": [], "folder": "v"}, "unknown field"),
])
def test_settings_that_are_not_complete_are_refused(raw, words):
    with pytest.raises(V.VaultFileError, match=words):
        V.parse_settings(raw)


def test_one_recipient_alone_is_never_enough_to_seal():
    _, only = V.new_identity()
    with pytest.raises(V.VaultFileError, match="AND at least one recovery key"):
        V.seal({"x": 1}, [only, only])


def test_a_recovery_key_is_written_once_and_its_public_half_answered(tmp_path):
    out = tmp_path / "recovery.key"
    answer = F.new_recovery_key(out, created_at="2026-11-10T06:00:00Z")
    identity = V.identity_from_file_text(out.read_text(encoding="ascii"))
    assert V.recipient_of(identity) == answer["recipient"] and V.check_recipient(answer["recipient"])
    with pytest.raises(F.VaultFileRefused, match="never overwritten"):
        F.new_recovery_key(out, created_at="2026-11-10T06:00:01Z")


def test_dbabrains_identity_is_made_once_and_never_replaced(node, monkeypatch):
    from dbabrain.transport import common_cli

    with pytest.raises(F.VaultFileRefused, match="never replaced"):
        F.new_dbabrain_identity(node.app_config, secrets=node.secrets)

    stored = {}
    monkeypatch.setattr(common_cli, "run", lambda op, req, **_k: stored.update(op=op, **req) or {"plaintext_source": "x"})
    answer = F.new_dbabrain_identity(node.app_config, secrets={})
    assert stored["op"] == "secret-set" and stored["ref"] == "VAULT_ID" and stored["also_plaintext"] is True
    assert V.recipient_of(stored["value"]) == answer["recipient"]


@pytest.mark.parametrize("server_id, name", [("../etc", "c"), ("S", "a/b"), ("S", ""), ("S", "..x")])
def test_a_name_that_would_leave_the_folder_is_refused(server_id, name):
    with pytest.raises(V.VaultFileError):
        V.relative_path(server_id, name, "a" * 40)
