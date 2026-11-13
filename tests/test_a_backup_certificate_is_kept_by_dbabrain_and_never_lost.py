"""A SQL Server backup certificate is kept by dbabrain, encrypted, proven - and only then taken off its server (0.37.0).

Why it matters: a backup written WITH ENCRYPTION restores on another instance only with its
certificate pair (``.cer`` + ``.pvk``). The operator, 2026-11-10 (translated): *certificates and keys
are not kept with the backups, nor on the server_id being backed up: take them once into dbabrain,
then delete them on the server - but keep them carefully, never lose them*; kept in the runtime
store only. Lost, every encrypted backup is restorable only on the instance that wrote it.

So the promises held here:

* the vault keeps the pair encrypted (the plaintext bytes are nowhere in the row), with each file's
  sha256, bytes and thumbprint; a row is never overwritten - the same pair again is the row already
  there, the same thumbprint with other bytes is refused; there is no way to delete a row;
* a row is ``verified`` only after it is read back, decrypted and held to every fact - a wrong
  passphrase, a damaged envelope or a changed fact never proves;
* ``take-certificate`` reads each file twice (hashed on the server, read and proven on arrival) and
  stores nothing when the two disagree; it keeps nothing on a dry run;
* the server's copy is deleted only with ``--apply --delete-at-source``, only for a proven row, never
  during a lockdown, one file per command with the facts the vault proved - the ``.cer``, then the
  ``.pvk``, then the empty ``_cert`` folder - and a refusal stops it before the next file;
* the backup script is told which thumbprints the vault holds and does not export them again; a
  vault that cannot be read tells it nothing (it exports as it always did);
* a restore reads the pair from the vault before the share or the server.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import certificate_take as T
from dbabrain.db import certificate_vault as V

PASS = "a test passphrase, not the estate's"
CER, PVK = b"0\x82\x03 a certificate's DER bytes", b"\x1e\xf1\xb5\xb0 a private key, encrypted by its password"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def vault(tmp_path):
    return V.CertificateVault(tmp_path / "store.sqlite")


def _keep(vault, cer=CER, pvk=PVK, **extra):
    values = dict(server_id="SRV-01", certificate_name="dbabrain_backup_cert", cer=cer, pvk=pvk,
                  cer_source_path="D:\\SQLBK\\_cert\\dbabrain_backup_cert.cer",
                  pvk_source_path="D:\\SQLBK\\_cert\\dbabrain_backup_cert.pvk",
                  password_ref="BACKUP_SRV01_PW", passphrase=PASS, taken_by="test")
    values.update(extra)
    return vault.keep(**values)


# --------------------------------------------------------------------------- #
# 1. The vault
# --------------------------------------------------------------------------- #
def test_the_pair_is_kept_encrypted_with_every_fact_that_proves_it(vault):
    row = _keep(vault)

    assert row["already_kept"] is False and row["verified_at"] is None
    assert row["thumbprint"] == hashlib.sha1(CER).hexdigest()  # noqa: S324 - SQL Server's thumbprint
    assert (row["cer_size_bytes"], row["cer_sha256"]) == (len(CER), _sha(CER))
    assert (row["pvk_size_bytes"], row["pvk_sha256"]) == (len(PVK), _sha(PVK))
    assert row["cer_file_name"] == "dbabrain_backup_cert.cer" and row["password_ref"] == "BACKUP_SRV01_PW"
    stored = row["envelope"]
    for plain in (CER, PVK):
        assert base64.b64encode(plain).decode() not in stored and plain.decode("latin-1") not in stored


def test_a_row_is_verified_only_after_it_is_read_back_decrypted_and_held_to_every_fact(vault):
    row = _keep(vault)
    proven = vault.prove(row["certificate_id"], passphrase=PASS)

    assert proven["verified_at"]
    cer, pvk, _row = vault.pair(row["certificate_id"], passphrase=PASS)
    assert (cer, pvk) == (CER, PVK)


def test_a_wrong_passphrase_never_proves(vault):
    row = _keep(vault)
    with pytest.raises(V.CertificateVaultError, match="cannot be decrypted"):
        vault.prove(row["certificate_id"], passphrase="another passphrase")
    assert vault.get(row["certificate_id"])["verified_at"] is None


def test_a_damaged_envelope_or_a_changed_fact_never_proves(vault):
    row = _keep(vault)
    envelope = json.loads(row["envelope"])
    envelope["ciphertext"] = envelope["ciphertext"][:-8] + "AAAAAAAA"
    with vault.connect() as conn:
        conn.execute("UPDATE backup_certificates SET envelope = ? WHERE certificate_id = ?",
                     (json.dumps(envelope), row["certificate_id"]))
    with pytest.raises(V.CertificateVaultError):
        vault.prove(row["certificate_id"], passphrase=PASS)

    other = _keep(vault, server_id="SRV-02")
    with vault.connect() as conn:
        conn.execute("UPDATE backup_certificates SET pvk_sha256 = ? WHERE certificate_id = ?",
                     ("0" * 64, other["certificate_id"]))
    with pytest.raises(V.CertificateVaultError, match="pvk is not the bytes"):
        vault.prove(other["certificate_id"], passphrase=PASS)


def test_the_same_pair_again_is_the_row_already_there_and_other_bytes_are_never_written_over_it(vault):
    first = _keep(vault)
    again = _keep(vault)
    assert again["already_kept"] is True and again["certificate_id"] == first["certificate_id"]

    with pytest.raises(V.CertificateVaultError, match="already kept with OTHER bytes"):
        _keep(vault, pvk=b"another private key for the same certificate")
    cer, pvk, _ = vault.pair(first["certificate_id"], passphrase=PASS)
    assert pvk == PVK


def test_a_new_certificate_is_a_new_row_and_the_old_one_stays(vault):
    old = _keep(vault)
    new = _keep(vault, cer=b"a renewed certificate", pvk=b"its key")
    assert new["certificate_id"] != old["certificate_id"]
    assert len(vault.listing()) == 2


def test_the_source_copy_may_be_marked_deleted_only_for_a_verified_row(vault):
    row = _keep(vault)
    with pytest.raises(V.CertificateVaultError, match="not verified"):
        vault.mark_source_deleted(row["certificate_id"])
    vault.prove(row["certificate_id"], passphrase=PASS)
    vault.mark_source_deleted(row["certificate_id"])
    assert vault.get(row["certificate_id"])["source_deleted_at"]


def test_nothing_is_kept_unencrypted_or_half_stated(vault):
    with pytest.raises(V.CertificateVaultError, match="never stored unencrypted"):
        _keep(vault, passphrase="")
    with pytest.raises(V.CertificateVaultError, match="neither may be empty"):
        _keep(vault, pvk=b"")
    with pytest.raises(V.CertificateVaultError, match="password_ref is required"):
        _keep(vault, password_ref="")


def test_the_vault_has_no_way_to_delete_or_rewrite_a_pair():
    source = Path(V.__file__).read_text(encoding="utf-8").upper()
    assert "DELETE FROM" not in source and "DROP " not in source
    assert "SET ENVELOPE" not in source and "SET CER_SHA256" not in source


def test_the_listing_never_shows_the_envelope(vault):
    _keep(vault)
    assert all("envelope" not in row for row in vault.listing())


# --------------------------------------------------------------------------- #
# 2. take-certificate
# --------------------------------------------------------------------------- #
class _Server:
    """The SQL Server host: its _cert folder, and the four common.cli commands the take sends."""

    def __init__(self) -> None:
        self.files = {"D:\\SQLBK\\_cert\\dbabrain_backup_cert.cer": CER,
                      "D:\\SQLBK\\_cert\\dbabrain_backup_cert.pvk": PVK}
        self.calls: list[tuple[str, dict]] = []
        self.read_override: dict[str, bytes] = {}
        self.refuse_delete: set[str] = set()
        self.folder_has_other = False

    def run(self, command, request, **_k):
        self.calls.append((command, request))
        if command == "file-checksum":
            return {"files": [{"path": p, "sha256": _sha(self.files[p]), "size_bytes": len(self.files[p])}
                              for p in request["paths"] if p in self.files]}
        if command == "read-file":
            data = self.read_override.get(request["path"], self.files[request["path"]])
            return {"path": request["path"], "size_bytes": len(data), "sha256": _sha(data),
                    "base64": base64.b64encode(data).decode()}
        raise AssertionError(command)

    def run_allowing_failure(self, command, request, **_k):
        self.calls.append((command, request))
        if command == "delete-verified-file":
            assert "paths" not in request
            path = request["path"]
            data = self.files.get(path)
            if path in self.refuse_delete or (data is not None and (len(data), _sha(data)) !=
                                              (request["size_bytes"], request["sha256"])):
                return False, {"file": {"status": "refused", "reason": "not the file stated"}}, "refused"
            self.files.pop(path, None)
            return True, {"file": {"status": "deleted" if data is not None else "not_found", "reason": ""}}, ""
        if command == "delete-empty-folder":
            if self.folder_has_other:
                return False, {"folder": {"status": "refused", "reason": "the folder holds 1 entry"}}, "refused"
            return True, {"folder": {"status": "deleted", "reason": ""}}, ""
        raise AssertionError(command)


def _job(**extra):
    values = dict(backup_id="SRV01_MSSQL_FULL", db_type="sqlserver", server_id="SRV-01", backup_dir="D:\\SQLBK",
                  env={}, env_secrets={"BACKUP_ENCRYPTION_PASSWORD": "BACKUP_SRV01_PW"}, label="SRV01_MSSQL_FULL")
    values.update(extra)
    return SimpleNamespace(**values)


@pytest.fixture
def server(monkeypatch):
    fake = _Server()
    monkeypatch.setattr(T.common_cli, "run", fake.run)
    monkeypatch.setattr(T.common_cli, "run_allowing_failure", fake.run_allowing_failure)
    monkeypatch.setattr(T, "resolve_backup_target", lambda job, data_dir=None: SimpleNamespace(platform="windows"))
    monkeypatch.setattr(T, "_load_secrets", lambda **_k: {})
    import dbabrain.backup_restore.spec_builder as spec_builder
    monkeypatch.setattr(spec_builder, "host_block", lambda target, **_k: {"runtime": "windows", "host": "srv"})
    return fake


@pytest.fixture
def jobs(monkeypatch):
    box = {"jobs": [_job()]}
    monkeypatch.setattr(T, "load_backup_jobs", lambda _path: box["jobs"])
    return box


class _Catalog:
    def __init__(self):
        self.pruned = []

    def initialize(self):
        pass

    def connect(self):
        catalog = self

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                return SimpleNamespace(fetchall=lambda: [{"file_id": 7}, {"file_id": 8}])
        return _Conn()

    def set_backup_file(self, file_id, **values):
        self.pruned.append((file_id, values["state"]))


@pytest.fixture
def unlocked(monkeypatch):
    import dbabrain.db.lockdown_store as L
    monkeypatch.setattr(L, "refuse_if_locked", lambda app_config, action: None)
    # The row's sealed file stands (its own gate is held in
    # test_a_vault_certificate_also_stands_as_a_file_a_recovery_key_opens.py).
    from dbabrain.backup_restore import certificate_files
    monkeypatch.setattr(certificate_files, "require_proven_file", lambda *_a, **_k: "the row's file")


def _take(vault, **extra):
    values = dict(app_config=None, config_path="x", backup_id="SRV01_MSSQL_FULL", passphrase=PASS, vault=vault)
    values.update(extra)
    return T.take_certificate(**values)


def test_a_dry_run_reads_both_files_twice_and_keeps_nothing(vault, server, jobs):
    answer = _take(vault)

    assert answer["thumbprint"] == hashlib.sha1(CER).hexdigest()  # noqa: S324
    assert [c for c, _ in server.calls] == ["file-checksum", "read-file", "read-file"]
    assert vault.listing() == [] and len(server.files) == 2


def test_apply_keeps_and_proves_and_deletes_nothing(vault, server, jobs):
    answer = _take(vault, apply=True)

    row = vault.get(answer["certificate_id"])
    assert row["verified_at"] and row["source_deleted_at"] is None
    assert len(server.files) == 2 and not any(c.startswith("delete") for c, _ in server.calls)


def test_two_reads_that_disagree_keep_nothing(vault, server, jobs):
    server.read_override["D:\\SQLBK\\_cert\\dbabrain_backup_cert.pvk"] = b"changed between the two reads"
    with pytest.raises(T.TakeRefused, match="the two reads disagree"):
        _take(vault, apply=True)
    assert vault.listing() == []


def test_delete_at_source_removes_one_file_per_command_with_the_vaults_facts_then_the_empty_folder(
        vault, server, jobs, unlocked):
    catalog = _Catalog()
    answer = _take(vault, apply=True, delete_at_source=True, catalog=catalog)

    deletes = [(c, r) for c, r in server.calls if c.startswith("delete")]
    assert [c for c, _ in deletes] == ["delete-verified-file", "delete-verified-file", "delete-empty-folder"]
    cer_req, pvk_req = deletes[0][1], deletes[1][1]
    assert cer_req["path"].endswith(".cer") and pvk_req["path"].endswith(".pvk")
    assert (cer_req["size_bytes"], cer_req["sha256"]) == (len(CER), _sha(CER))
    assert (pvk_req["size_bytes"], pvk_req["sha256"]) == (len(PVK), _sha(PVK))
    assert cer_req["must_be_under"] == "D:\\SQLBK" and deletes[2][1]["path"] == "D:\\SQLBK\\_cert"
    assert server.files == {}
    assert vault.get(answer["certificate_id"])["source_deleted_at"]
    assert catalog.pruned == [(7, "pruned"), (8, "pruned")]


def test_a_refused_delete_stops_before_the_next_file_and_the_vault_row_stays(vault, server, jobs, unlocked):
    server.refuse_delete.add("D:\\SQLBK\\_cert\\dbabrain_backup_cert.cer")
    with pytest.raises(T.TakeRefused, match="not deleted"):
        _take(vault, apply=True, delete_at_source=True, catalog=_Catalog())

    assert len(server.files) == 2, "the .pvk was not touched after the .cer was refused"
    [row] = vault.listing()
    assert row["verified_at"] and row["source_deleted_at"] is None


def test_a_folder_with_anything_else_in_it_stays_and_says_so(vault, server, jobs, unlocked):
    server.folder_has_other = True
    answer = _take(vault, apply=True, delete_at_source=True, catalog=_Catalog())
    assert any("refused" in step for step in answer["steps"]) and server.files == {}


def test_nothing_is_deleted_during_a_lockdown(vault, server, jobs, monkeypatch):
    import dbabrain.db.lockdown_store as L

    def locked(app_config, action):
        raise L.Locked(f"{action} is refused: a lockdown is on")
    monkeypatch.setattr(L, "refuse_if_locked", locked)

    with pytest.raises(L.Locked):
        _take(vault, apply=True, delete_at_source=True, catalog=_Catalog())
    assert len(server.files) == 2 and vault.listing()[0]["verified_at"], "kept and proven, nothing deleted"


@pytest.mark.parametrize("job, words", [
    (_job(db_type="postgresql"), "only a SQL Server backup"),
    (_job(env_secrets={}), "does not encrypt its backups"),
])
def test_an_entry_with_no_certificate_is_refused_by_name(vault, server, jobs, job, words):
    jobs["jobs"] = [job]
    with pytest.raises(T.TakeRefused, match=words):
        _take(vault)


def test_delete_at_source_without_apply_is_refused(vault, server, jobs):
    with pytest.raises(T.TakeRefused, match="needs --apply"):
        _take(vault, delete_at_source=True)


def test_a_pair_already_gone_from_the_server_is_said_and_nothing_happens(vault, server, jobs):
    server.files.clear()
    with pytest.raises(T.TakeRefused, match="nothing to take"):
        _take(vault, apply=True)


@pytest.mark.parametrize("windows, expected", [
    (True, ("D:\\SQLBK\\_cert", "D:\\SQLBK\\_cert\\dbabrain_backup_cert.cer", "D:\\SQLBK\\_cert\\dbabrain_backup_cert.pvk")),
    (False, ("/b/_cert", "/b/_cert/MY_CERT.cer", "/b/_cert/MY_CERT.pvk")),
])
def test_the_pair_is_where_the_backup_script_writes_it(windows, expected):
    job = _job(backup_dir="D:\\SQLBK\\" if windows else "/b/", env={} if windows else {"BACKUP_CERT_NAME": "MY_CERT"})
    assert T.certificate_paths(job, windows=windows) == expected


# --------------------------------------------------------------------------- #
# 3. The backup script is told what the vault holds; a restore reads the vault first
# --------------------------------------------------------------------------- #
def test_the_backup_run_is_told_the_verified_thumbprints_and_nothing_when_the_vault_cannot_be_read(vault, monkeypatch):
    row = _keep(vault)
    monkeypatch.setattr(V.CertificateVault, "from_config", classmethod(lambda cls, _c: vault))
    assert T.held_thumbprints(None, _job()) == "", "kept but not yet proven: not held"
    vault.prove(row["certificate_id"], passphrase=PASS)
    assert T.held_thumbprints(None, _job()) == row["thumbprint"]
    assert T.held_thumbprints(None, _job(db_type="oracle")) == ""

    def broken(cls, _c):
        raise OSError("store down")
    monkeypatch.setattr(V.CertificateVault, "from_config", classmethod(broken))
    assert T.held_thumbprints(None, _job()) == "", "an unreadable vault tells the script nothing: it exports"


@pytest.mark.parametrize("script", ["mssql_backup_database.ps1", "mssql_backup_database.sh"])
def test_both_backup_scripts_skip_the_export_of_a_held_thumbprint_and_warn_on_another(script):
    text = (Path(T.__file__).resolve().parents[1] / "common" / "backup_scripts" / "sqlserver" / script).read_text(
        encoding="utf-8")
    assert T.HELD_ENV in text and "is kept by dbabrain - not exported" in text
    assert "which dbabrain" in text and "take it with take-certificate" in text


def test_a_restore_reads_the_pair_from_the_vault(monkeypatch):
    from dbabrain.backup_restore import certificate as C

    monkeypatch.setattr(C, "resolve_password_ref", lambda ref: "the pvk password")
    monkeypatch.setattr(C, "_pair_from_vault", lambda config, name: (CER, PVK, {"certificate_id": 1, "thumbprint": "t"}))
    config = SimpleNamespace(restore_id="R", source_id="SRV-01",
                             backup_certificate=SimpleNamespace(name="dbabrain_backup_cert", password_ref="P", source_dir=""))

    pair = C.read_backup_certificate_pair(config)

    assert base64.b64decode(pair.certificate_base64) == CER and base64.b64decode(pair.private_key_base64) == PVK
    assert pair.thumbprint == hashlib.sha1(CER).hexdigest()  # noqa: S324


def test_a_restore_never_falls_back_to_the_share_when_the_vault_holds_nothing(monkeypatch):
    """0.37.0, the operator: kept in dbabrain only - nothing beside the backups is read."""
    from dbabrain.backup_restore import certificate as C

    monkeypatch.setattr(C, "resolve_password_ref", lambda ref: "pw")
    monkeypatch.setattr(C, "_pair_from_vault", lambda config, name: None)
    config = SimpleNamespace(restore_id="R", source_id="SRV-01",
                             backup_certificate=SimpleNamespace(name="c", password_ref="P", source_dir=""))
    with pytest.raises(Exception, match="take-certificate"):
        C.read_backup_certificate_pair(config)
