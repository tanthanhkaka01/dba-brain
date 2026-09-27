"""A backup certificate is found by its thumbprint, and importing one never drops another.

SQL Server reads an encrypted backup with whichever certificate has the right THUMBPRINT; the name is
a label. Every path that imported one decided by name, and ``db_ops_backup_cert`` is the default name
everywhere, so on 2026-09-27 - restoring a production server's backups into a lab VM, which dbabrain also backs up
with its own ``db_ops_backup_cert`` - each path did the wrong thing its own way:

* ``restore-key`` and the shell restore dropped the target's certificate of that name and created
  the source's - leaving the target's own backups unreadable on it;
* the SMB restore saw the name and skipped the import - so the source's certificate never arrived,
  and the restore failed on the thumbprint.

Now there is one batch (``db_ops.lib.sqlserver_certificate``) and all three send it: the thumbprint
is the SHA-1 of the ``.cer`` file, read by the instance itself; one already there is left alone; a
taken name becomes ``<name>_<first 8 hex digits>``. The SMB restore can also read the pair that
dbabrain's own backup job exports beside the backups, which it could not before.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from db_ops.backup_restore import certificate as cert_module
from db_ops.backup_restore.config import BackupCertificateSource, BackupRestoreConfig, load_restore_configs
from db_ops.common import restorekey, sqlcmd_run
from db_ops.lib import sqlserver_certificate

REPO = Path(__file__).resolve().parents[1]
SHELL_RESTORE = REPO / "db_ops" / "common" / "restore_scripts" / "sqlserver" / "mssql_restore.sh"
WINDOWS_BACKUP = REPO / "db_ops" / "common" / "backup_scripts" / "sqlserver" / "mssql_backup_database.ps1"


def _batch(**overrides) -> str:
    values = {"name": "db_ops_backup_cert", "cer_path": "/stage/_cert/db_ops_backup_cert.cer",
              "pvk_path": "/stage/_cert/db_ops_backup_cert.pvk", "password": "pw"}
    values.update(overrides)
    return sqlserver_certificate.import_batch(**values)


# --------------------------------------------------------------------------- #
# The one batch
# --------------------------------------------------------------------------- #
def test_the_thumbprint_is_read_from_the_cer_file_by_the_instance_itself():
    batch = _batch()
    assert ("HASHBYTES('SHA1', f.BulkColumn) FROM OPENROWSET(BULK N'/stage/_cert/db_ops_backup_cert.cer', "
            "SINGLE_BLOB)") in batch
    assert "FROM master.sys.certificates WHERE thumbprint = @thumbprint" in batch


def test_nothing_is_ever_dropped():
    assert "DROP CERTIFICATE" not in _batch().upper()


def test_a_name_taken_by_another_certificate_gets_the_thumbprints_first_digits():
    batch = _batch()
    assert "IF EXISTS (SELECT 1 FROM master.sys.certificates WHERE name = @name)" in batch
    assert "SET @name = @name + N'_' + LOWER(CONVERT(varchar(8), SUBSTRING(@thumbprint, 1, 4), 2))" in batch


def test_a_copy_without_its_private_key_gets_the_key_added_rather_than_a_second_copy():
    batch = _batch()
    assert "pvt_key_encryption_type = 'NA'" in batch
    assert "ALTER CERTIFICATE ' + QUOTENAME(@present)" in batch


def test_values_inside_the_dynamic_statement_are_escaped_for_both_literals():
    batch = _batch(cer_path="/o'k/a.cer", pvk_path="/o'k/a.pvk", password="p'w")
    # Read by OPENROWSET in this batch: one literal.
    assert "OPENROWSET(BULK N'/o''k/a.cer'" in batch
    # Inside sp_executesql's statement, which sits in this batch's literal: two.
    assert "FROM FILE = N''/o''''k/a.cer''" in batch
    assert "DECRYPTION BY PASSWORD = N''p''''w''" in batch


def test_the_answer_line_names_the_certificate_that_holds_the_thumbprint_now():
    assert sqlserver_certificate.parse_marker(
        "noise\nDB_OPS_CERTIFICATE|db_ops_backup_cert_a1b2c3d4|A1B2C3D4E5F60718293A4B5C6D7E8F9012345678|1\n") == {
        "certificate_name": "db_ops_backup_cert_a1b2c3d4",
        "thumbprint": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", "imported": True}
    assert sqlserver_certificate.parse_marker("no marker here") is None


def test_the_shell_restore_sends_the_same_batch_and_drops_nothing():
    """It cannot import the Python; it carries the same statements, which this holds it to."""
    shell = SHELL_RESTORE.read_text(encoding="utf-8")
    assert "DROP CERTIFICATE" not in shell
    for line in ("HASHBYTES('SHA1', f.BulkColumn)", "WHERE thumbprint = @thumbprint",
                 "SUBSTRING(@thumbprint, 1, 4)", "pvt_key_encryption_type = 'NA'",
                 "DB_OPS_CERTIFICATE|"):
        assert line in shell


# --------------------------------------------------------------------------- #
# restore-key
# --------------------------------------------------------------------------- #
def test_restore_key_plans_the_one_batch_and_drops_nothing():
    answer = restorekey.import_key({"cer_path": "/c.cer", "pvk_path": "/c.pvk", "password": "pw",
                                    "dry_run": True})
    assert answer["statements"] == [_batch(cer_path="/c.cer", pvk_path="/c.pvk")]
    assert "DROP CERTIFICATE" not in answer["statements"][0]


def test_restore_key_through_sqlcmd_reports_one_already_there_as_not_imported(monkeypatch):
    sent = {}

    def fake_run(request):
        sent.update(request)
        return {"exit_code": 0, "timed_out": False,
                "stdout": "DB_OPS_CERTIFICATE|db_ops_backup_cert_source|A1B2C3D4|0\n"}

    monkeypatch.setattr(sqlcmd_run, "run_sqlcmd", fake_run)
    answer = restorekey.import_key({
        "certificate_name": "db_ops_backup_cert", "cer_path": "/c.cer", "pvk_path": "/c.pvk",
        "password": "pw", "sqlcmd": {"instance": "localhost,1433", "via": "ssh", "container": "MSSQL"}})
    assert answer == {"certificate_name": "db_ops_backup_cert_source", "thumbprint": "a1b2c3d4",
                      "imported": False, "ok": True}
    assert sent["container"] == "MSSQL" and "OPENROWSET(BULK N'/c.cer'" in sent["sql"]


# --------------------------------------------------------------------------- #
# The SMB restore reads the pair dbabrain's backup job exports
# --------------------------------------------------------------------------- #
def _config(tmp_path: Path, **overrides) -> BackupRestoreConfig:
    config = BackupRestoreConfig(
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK_DBOPS"), vm_import_unc=tmp_path / "imp",
        vm_import_local=Path("/opt/db_ops/backup/SQLBK_IMPORT/SRC"), vm_log_unc=tmp_path / "log",
        vm_log_local=Path("/log"), prod_smb_credential_target="192.0.2.250", prod_smb_username="u",
        prod_smb_password_env="", vm_credential_target="192.0.2.251", vm_username="tuser",
        vm_password_env="", restore_sql_instance_on_vm="localhost,1433", vm_platform="linux",
        source_id="ACME-192-0-2-250", restore_id="R1",
        backup_certificate=BackupCertificateSource(name="db_ops_backup_cert", password_ref="PASSPHRASE_REF"))
    return dataclasses.replace(config, **overrides)


def test_the_pair_is_read_from_the_share_and_its_thumbprint_computed_here(tmp_path, monkeypatch):
    monkeypatch.setenv("PASSPHRASE_REF", "passphrase")
    monkeypatch.setattr(cert_module, "_pair_from_share", lambda config: (b"DER-CERT", b"PVK"))
    certificate = cert_module.read_backup_certificate_pair(_config(tmp_path))
    assert certificate.thumbprint == hashlib.sha1(b"DER-CERT").hexdigest()
    assert base64.b64decode(certificate.private_key_base64) == b"PVK"
    assert certificate.private_key_password == "passphrase"


def test_a_share_that_refuses_the_pair_says_how_to_read_it_instead(tmp_path, monkeypatch):
    """A pair exported before 0.24.1 is readable by the SQL Server service account only."""
    monkeypatch.setenv("PASSPHRASE_REF", "passphrase")

    def refused(config):
        raise RuntimeError("NT_STATUS_ACCESS_DENIED")

    monkeypatch.setattr(cert_module, "_pair_from_share", refused)
    with pytest.raises(RuntimeError) as error:
        cert_module.read_backup_certificate_pair(_config(tmp_path))
    assert "source.backup_certificate.source_dir" in str(error.value)
    assert "NT_STATUS_ACCESS_DENIED" in str(error.value)


def test_with_source_dir_the_pair_is_read_over_the_source_hosts_login(tmp_path, monkeypatch):
    monkeypatch.setenv("PASSPHRASE_REF", "passphrase")

    def refused(config):
        raise RuntimeError("NT_STATUS_ACCESS_DENIED")

    monkeypatch.setattr(cert_module, "_pair_from_share", refused)
    monkeypatch.setattr(cert_module, "_pair_from_source_host", lambda config: (b"DER", b"KEY"))
    config = _config(tmp_path, backup_certificate=BackupCertificateSource(
        name="db_ops_backup_cert", password_ref="PASSPHRASE_REF", source_dir=r"D:\SQLBK_DBOPS\_cert"))
    assert cert_module.read_backup_certificate_pair(config).thumbprint == hashlib.sha1(b"DER").hexdigest()


def test_a_dry_run_names_where_the_pair_would_come_from(tmp_path):
    answer = cert_module.ensure_source_certificate(config=_config(tmp_path), dry_run=True)
    assert answer["status"] == "DRY_RUN"
    assert answer["certificate_source"] == r"\\192.0.2.250\SQLBK_DBOPS\_cert\db_ops_backup_cert.cer"


def test_an_entry_states_the_pair_and_the_target_container(tmp_path):
    path = tmp_path / "restore_config.json"
    entry = {
        "restore_id": "R1", "server_id": "ACME-192-0-2-250", "target_server_id": "ACME-192-0-2-251",
        "cleanup_retention": 86400,
        "time_window": {"from_hour": 2, "to_hour": 5, "repeat_interval": 72000, "retry_interval": 600,
                        "timeout": 7200},
        "source": {"backup_share": "\\\\192.0.2.250\\SQLBK_DBOPS", "credential_target": "192.0.2.250",
                   "username": "u", "password_ref": "SHARE_REF",
                   "backup_certificate": {"password_ref": "PASSPHRASE_REF", "source_dir": "D:\\SQLBK_DBOPS\\_cert"}},
        "target": {"vm_platform": "linux", "credential_target": "192.0.2.251", "username": "tuser",
                   "password_ref": "VM_REF", "sql_instance": "localhost,1433", "sql_username": "sa",
                   "sql_password_ref": "SA_REF", "restore_data_dir": "/var/opt/mssql/data",
                   "vm_import_linux_path": "/opt/db_ops/backup/SQLBK_IMPORT/SRC",
                   "sql_container": "MSSQL_1433"},
        "database_mappings": [{"source_database": "APPDB", "target_database": "AppDb"}],
    }
    path.write_text(json.dumps({"backup_restore": {"restores": [entry]}}), encoding="utf-8")
    config = load_restore_configs(path)[0]
    assert config.backup_certificate == BackupCertificateSource(
        name="db_ops_backup_cert", password_ref="PASSPHRASE_REF", source_dir="D:\\SQLBK_DBOPS\\_cert")
    assert config.sql_container == "MSSQL_1433"


def test_a_pair_without_its_passphrase_ref_is_refused_when_the_entry_is_read(tmp_path):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": [{
        "restore_id": "R1", "server_id": "S", "target_server_id": "T", "cleanup_retention": 86400,
        "source": {"backup_share": "\\\\h\\s", "backup_certificate": {"name": "c"}},
        "target": {"vm_platform": "linux", "vm_import_linux_path": "/i", "restore_data_dir": "/d"},
    }]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="backup_certificate.password_ref is required"):
        load_restore_configs(path)


# --------------------------------------------------------------------------- #
# The Windows backup job leaves the pair as readable as the backups
# --------------------------------------------------------------------------- #
def test_the_windows_backup_job_gives_the_pair_its_folders_permissions_on_every_run():
    script = WINDOWS_BACKUP.read_text(encoding="utf-8")
    reset = script.index("icacls $part /reset")
    export = script.index("BACKUP CERTIFICATE [$certName]")
    guard = script.index("if (@($certExists)[0] -ne '1')")
    # After the export, and outside the "export once" branch - so an older pair is opened too.
    assert reset > export > guard
    branch_end = script.index("\n    }\n", export) if "\n    }\n" in script[export:] else script.index("\r\n    }\r\n", export)
    assert reset > branch_end
