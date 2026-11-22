"""A passphrase change re-seals what it opens as NEW rows and keeps the old ones, retired (0.42.0, 1.411).

The runtime store keeps two things only the secret store's passphrase opens: the repository's age keys
(every encrypted backup file names one by ``key_id``) and the SQL Server backup certificates. The
operator, 2026-11-21 (translated): *do not update that important data and lose its trace - set the old
one inactive, then make the new one active*. These tests hold:

- each active row is copied whole into a new row sealed with the new passphrase; the old row keeps its
  envelope byte for byte and is marked retired, pointing at its replacement;
- a file that names the OLD key_id still decrypts - followed to the active row of the same key - and a
  certificate found by its name is the active row;
- both tables move in one transaction, or neither does: a row that does not open with the old
  passphrase stops it before anything is written;
- a store made by schema 1 (the UNIQUE that allowed one row per key) is migrated, and rotates twice;
- the secret store file is sealed again whole, and only the new passphrase opens it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from dbabrain.db import certificate_vault as V
from dbabrain.db import passphrase_rotation as P
from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore
from dbabrain.lib import errors, secret_text
from dbabrain.lib import vault_file

OLD, NEW, NEWER = "old pass #1", "new pass #2", "newer pass #3"
CER, PVK = b"0\x82\x03 a certificate's DER bytes", b"\x1e\xf1 a private key"


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "store.sqlite"
    keys = RepositoryEncryptionStore(path)
    vault = V.CertificateVault(path)
    identity, recipient = vault_file.new_identity()
    key = keys.add_key(server_id="SRC", identity=identity, recipient=recipient, created_at="2026-01-01T00:00:00Z",
                       days=90, passphrase=OLD, created_by="test")
    keys.mark_key_sealed(int(key["key_id"]), sealed_file="/repo/_repository_keys/k1.age")
    cert = vault.keep(server_id="SRV", certificate_name="dbabrain_backup_cert", cer=CER, pvk=PVK,
                      cer_source_path="D:\\c.cer", pvk_source_path="D:\\c.pvk", password_ref="PW", passphrase=OLD,
                      taken_by="test")
    vault.prove(int(cert["certificate_id"]), passphrase=OLD)
    return path, keys, vault, key, cert, identity


def _rows(path, table):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
    finally:
        conn.close()


def test_each_active_row_becomes_a_new_row_and_the_old_one_is_kept_retired(store):
    path, keys, _vault, key, cert, _identity = store
    before = {t: _rows(path, t) for t in ("repository_keys", "backup_certificates")}

    done = P.reseal_all(keys.target, old_passphrase=OLD, new_passphrase=NEW, reason="test rotation")

    for table, envelope in (("repository_keys", "identity_envelope"), ("backup_certificates", "envelope")):
        rows = _rows(path, table)
        old, new = rows
        assert old[envelope] == before[table][0][envelope], "the old envelope is kept, byte for byte"
        assert old["retired_at"] and old["retired_reason"] == "test rotation" and old["replaced_by"] == list(new.values())[0]
        assert new["retired_at"] is None and new["replaced_by"] is None
        assert secret_text.decrypt_secret_text(json.loads(new[envelope]), NEW)
        with pytest.raises(errors.InvalidConfig):
            secret_text.decrypt_secret_text(json.loads(new[envelope]), OLD)
    assert done["repository_keys"][0]["old_id"] == int(key["key_id"])
    assert done["backup_certificates"][0]["old_id"] == int(cert["certificate_id"])
    new_key = _rows(path, "repository_keys")[1]
    assert (new_key["sealed_file"], new_key["proven_at"], new_key["encrypt_until"]) == (
        before["repository_keys"][0]["sealed_file"], before["repository_keys"][0]["proven_at"],
        before["repository_keys"][0]["encrypt_until"]), "the same key, the same facts"


def test_a_file_that_names_the_old_key_id_still_decrypts_with_the_new_passphrase(store):
    _path, keys, vault, key, cert, identity = store
    P.reseal_all(keys.target, old_passphrase=OLD, new_passphrase=NEW, reason="r")

    assert keys.identity(int(key["key_id"]), passphrase=NEW) == identity
    assert [k["key_id"] for k in keys.keys(server_id="SRC")] != [key["key_id"]], "the active row is the new one"
    assert len(keys.keys(server_id="SRC")) == 1 and len(keys.keys(include_retired=True)) == 2
    assert keys.key_by_recipient(key["recipient"])["retired_at"] is None

    cer, pvk, row = vault.pair(int(cert["certificate_id"]), passphrase=NEW)
    assert (cer, pvk) == (CER, PVK) and row["retired_at"] is None
    found = vault.find(server_id="SRV", certificate_name="dbabrain_backup_cert")
    assert found["certificate_id"] != cert["certificate_id"]
    assert vault.held_thumbprints(server_id="SRV", certificate_name="dbabrain_backup_cert") == [cert["thumbprint"]]
    assert len(vault.listing()) == 1 and len(vault.listing(include_retired=True)) == 2


def test_a_row_that_does_not_open_stops_both_tables_before_anything_is_written(store):
    path, keys, vault, _key, _cert, _identity = store
    # A second certificate sealed with another passphrase: the vault does not open wholly with OLD.
    vault.keep(server_id="SRV", certificate_name="other", cer=b"other cer", pvk=b"other pvk",
               cer_source_path="D:\\o.cer", pvk_source_path="D:\\o.pvk", password_ref="PW", passphrase="stranger",
               taken_by="test")
    before = {t: _rows(path, t) for t in ("repository_keys", "backup_certificates")}

    assert P.plan(keys.target, old_passphrase=OLD)["backup_certificates"]["not_opening"]
    with pytest.raises(ValueError, match="does not open with the old passphrase"):
        P.reseal_all(keys.target, old_passphrase=OLD, new_passphrase=NEW, reason="r")
    assert {t: _rows(path, t) for t in before} == before, "the keys' table did not move either"


def test_a_second_rotation_retires_the_first_generation_too(store):
    path, keys, _vault, key, _cert, identity = store
    P.reseal_all(keys.target, old_passphrase=OLD, new_passphrase=NEW, reason="first")
    P.reseal_all(keys.target, old_passphrase=NEW, new_passphrase=NEWER, reason="second")

    rows = _rows(path, "repository_keys")
    assert [r["retired_at"] is None for r in rows] == [False, False, True]
    assert keys.identity(int(key["key_id"]), passphrase=NEWER) == identity


def test_a_schema_one_store_is_migrated_and_keeps_its_rows(tmp_path):
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE repository_keys (key_id INTEGER PRIMARY KEY AUTOINCREMENT, server_id TEXT NOT NULL,
            algorithm TEXT NOT NULL, recipient TEXT NOT NULL, identity_envelope TEXT NOT NULL, created_at TEXT NOT NULL,
            encrypt_until TEXT NOT NULL, lifetime_days INTEGER NOT NULL, created_by TEXT NOT NULL, sealed_file TEXT NULL,
            proven_at TEXT NULL, CONSTRAINT uq_repository_keys_recipient UNIQUE (recipient));
    """)
    identity, recipient = vault_file.new_identity()
    envelope = json.dumps(secret_text.encrypt_secret_text({"identity": identity}, OLD))
    conn.execute("INSERT INTO repository_keys (server_id, algorithm, recipient, identity_envelope, created_at, "
                 "encrypt_until, lifetime_days, created_by) VALUES ('SRC', 'age-x25519', ?, ?, "
                 "'2026-01-01T00:00:00Z', '2026-04-01T00:00:00Z', 90, 'v1')", (recipient, envelope))
    conn.commit()
    conn.close()

    keys = RepositoryEncryptionStore(path)
    assert keys.identity(1, passphrase=OLD) == identity
    P.reseal_all(keys.target, old_passphrase=OLD, new_passphrase=NEW, reason="r")
    assert keys.identity(1, passphrase=NEW) == identity
    assert len(_rows(path, "repository_keys")) == 2


def test_the_secret_store_file_is_sealed_again_whole_and_opens_only_with_the_new_passphrase(tmp_path):
    path = tmp_path / "encrypted_secret_text.json"
    path.write_text(json.dumps(secret_text.encrypt_secret_text({"A": "1", "B": "2"}, OLD, iterations=1000)),
                    encoding="utf-8")

    assert secret_text.rekey_secret_text_file(path, old_key=OLD, new_key=NEW) == 2
    assert secret_text.load_secret_text(tmp_path, key=NEW) == {"A": "1", "B": "2"}
    with pytest.raises(errors.InvalidConfig):
        secret_text.load_secret_text(tmp_path, key=OLD)
    assert [p for p in tmp_path.iterdir() if p.suffix != ".lock"] == [path], "no old copy is kept beside it"
    with pytest.raises(errors.InvalidConfig):
        secret_text.rekey_secret_text_file(path, old_key=OLD, new_key=NEWER)   # the old one opens it no more
    assert secret_text.load_secret_text(tmp_path, key=NEW) == {"A": "1", "B": "2"}
