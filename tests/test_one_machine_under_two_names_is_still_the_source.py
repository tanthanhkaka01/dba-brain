r"""A restore target that is the source under another name is refused like the source itself.

The guard compared strings (review 0.25.0, B4.6). The same production server written as a short name
in one field and as an IP or an FQDN in another - ``\\PRODSQL\...`` against ``192.0.2.50,1433`` -
passed every check, and the restore then ran ``RESTORE ... REPLACE`` on the server it was meant to
protect. Both sides are resolved now, and a shared address is one machine.

Loopback never counts: ``localhost`` in ``restore_sql_instance_on_vm`` is the target VM, not the
machine running the check. A name that does not resolve adds nothing, and its string still counts.
"""

from __future__ import annotations

import dataclasses
import socket
from pathlib import Path

import pytest

from db_ops.backup_restore import config as restore_config
from db_ops.backup_restore.config import BackupRestoreConfig, validate_restore_target_is_not_source

ADDRESSES = {
    "prodsql": "192.0.2.50",
    "prodsql.example.test": "192.0.2.50",
    "labsql": "198.51.100.20",
}


def _forget_resolved_names() -> None:
    cached = getattr(restore_config, "_addresses", None)
    if cached is not None:
        cached.cache_clear()


def _resolver(table: dict[str, str]):
    def resolve(host, *_args, **_kwargs):
        if host not in table:
            raise socket.gaierror(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]
    return resolve


@pytest.fixture(autouse=True)
def names(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolver(ADDRESSES))
    _forget_resolved_names()
    yield
    _forget_resolved_names()


def _config(**overrides) -> BackupRestoreConfig:
    base = BackupRestoreConfig(
        prod_backup_share=Path(r"\\prodsql\SQLBK"), vm_import_unc=Path(r"\\labsql\SQLBK_IMPORT"),
        vm_import_local=Path(r"C:\SQLBK_IMPORT"), vm_log_unc=Path(r"C:\logs"), vm_log_local=Path(r"C:\logs"),
        prod_smb_credential_target="prodsql", prod_smb_username="u", prod_smb_password_env="P",
        vm_credential_target="labsql", vm_username="u", vm_password_env="P",
        restore_sql_instance_on_vm="labsql,1433")
    return dataclasses.replace(base, **overrides)


def test_the_source_written_as_its_ip_is_the_source():
    with pytest.raises(ValueError, match="Unsafe restore config"):
        validate_restore_target_is_not_source(_config(vm_credential_target="192.0.2.50"))


def test_the_share_by_its_fqdn_and_the_instance_by_its_ip_are_one_machine():
    with pytest.raises(ValueError, match="Unsafe restore config"):
        validate_restore_target_is_not_source(_config(
            prod_backup_share=Path(r"\\prodsql.example.test\SQLBK"),
            restore_sql_instance_on_vm="192.0.2.50,1433"))


def test_an_import_share_on_the_source_under_its_ip_is_the_source():
    with pytest.raises(ValueError, match="vm_import_unc points at source"):
        validate_restore_target_is_not_source(_config(vm_import_unc=Path(r"\\192.0.2.50\IMPORT")))


def test_localhost_on_the_target_is_not_the_source(monkeypatch):
    # The worst case for loopback: the source name itself resolving to it on the checking machine.
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({**ADDRESSES, "prodsql": "127.0.0.1",
                                                          "localhost": "127.0.0.1"}))
    _forget_resolved_names()

    validate_restore_target_is_not_source(_config(restore_sql_instance_on_vm="localhost,1433"))


def test_another_machine_and_an_unresolvable_name_are_allowed():
    validate_restore_target_is_not_source(_config())
    validate_restore_target_is_not_source(_config(vm_credential_target="unknown-lab-host"))
