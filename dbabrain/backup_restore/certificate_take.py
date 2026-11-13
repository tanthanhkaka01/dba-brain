"""Take a SQL Server backup certificate into dbabrain's vault, once - then, if asked, off its server (0.37.0).

The operator, 2026-11-10 (translated): *certificates and keys are not kept where the backups are, nor
on the server_id being backed up: run once, copy them to dbabrain, then delete them on the server_id -
but keep them carefully, never lose them.* Kept in the runtime store only
(:mod:`dbabrain.db.certificate_vault`).

``backup_restore take-certificate --backup-id ID`` - run by hand, per backup entry, never scheduled:

1. the entry must be SQL Server and encrypt its backups (``env_secrets.BACKUP_ENCRYPTION_PASSWORD`` -
   the ``.pvk`` password, whose ref the vault keeps); the pair is ``<backup_dir>/_cert/<name>.cer``
   and ``.pvk``, ``<name>`` the job's ``BACKUP_CERT_NAME`` or the script's own default;
2. each file is hashed on its server (``file-checksum``) and its bytes read (``read-file``, which
   proves them against the host's own size and sha256); the two reads must agree;
3. the pair is kept, encrypted, and **proven**: read back on a new connection, decrypted, both files
   held to the sha256, the bytes and the thumbprint kept;
4. the row is also written as a sealed file on this node (:mod:`.certificate_files` - for dbabrain
   and an offline recovery key), when ``store_config.json`` configures it;
5. only with ``--delete-at-source``, only for a proven row WITH its proven file, never during a
   lockdown: the ``.cer``,
   then the ``.pvk``, each by ``delete-verified-file`` with the facts the vault holds, then the
   ``_cert`` folder by ``delete-empty-folder`` (refused, and said, while anything else is in it);
   the catalog's rows for the two files turn ``pruned``.

Without ``--apply`` it reads and reports, and stores nothing. The backup script then stops exporting
a certificate the vault holds (``BACKUP_CERT_HELD_THUMBPRINTS``), and a restore reads the pair from
the vault first.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from dbabrain.backup_restore.backup import _load_secrets, load_backup_jobs, resolve_backup_target
from dbabrain.db.certificate_vault import CertificateVault, CertificateVaultError, facts_of, thumbprint_of
from dbabrain.lib import errors
from dbabrain.lib.sqlserver_certificate import DEFAULT_NAME
from dbabrain.transport import common_cli

PASSWORD_ENV = "BACKUP_ENCRYPTION_PASSWORD"
#: The env the backup script reads: thumbprints the vault holds - not exported again.
HELD_ENV = "BACKUP_CERT_HELD_THUMBPRINTS"


class TakeRefused(errors.Refused):
    """The entry has no certificate to take, or a step did not prove."""


def certificate_name(job: Any) -> str:
    return str((job.env or {}).get("BACKUP_CERT_NAME") or DEFAULT_NAME)


def certificate_paths(job: Any, *, windows: bool) -> tuple[str, str, str]:
    """``(folder, cer, pvk)`` as the backup script writes them."""
    sep = "\\" if windows else "/"
    folder = str(job.backup_dir).rstrip("\\/") + sep + "_cert"
    name = certificate_name(job)
    return folder, f"{folder}{sep}{name}.cer", f"{folder}{sep}{name}.pvk"


def _job(config_path: str | Path, backup_id: str) -> Any:
    jobs = [job for job in load_backup_jobs(config_path) if job.backup_id == backup_id]
    if not jobs:
        raise TakeRefused(f"no backup entry {backup_id!r}.")
    job = jobs[0]
    if str(job.db_type).lower() != "sqlserver":
        raise TakeRefused(f"{backup_id} is {job.db_type}: only a SQL Server backup has a certificate pair to take.")
    if not (job.env_secrets or {}).get(PASSWORD_ENV):
        raise TakeRefused(f"{backup_id} does not encrypt its backups (no env_secrets.{PASSWORD_ENV}) - "
                          "there is no certificate to take.")
    return job


def take_certificate(*, app_config: Any, config_path: str | Path, backup_id: str, passphrase: str,
                     data_dir: str | Path | None = None, key: str | None = None, key_base64: str | None = None,
                     apply: bool = False, delete_at_source: bool = False, by: str = "operator",
                     vault: CertificateVault | None = None, catalog: Any = None) -> dict[str, Any]:
    """Read, keep, prove - and with ``delete_at_source``, delete the server's copy. Answers each step."""
    if delete_at_source and not apply:
        raise TakeRefused("--delete-at-source needs --apply: nothing is deleted from a dry run.")
    job = _job(config_path, backup_id)
    target = resolve_backup_target(job, data_dir=data_dir)
    windows = str(getattr(target, "platform", "") or "").lower() == "windows"
    folder, cer_path, pvk_path = certificate_paths(job, windows=windows)
    secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64)
    from dbabrain.backup_restore.spec_builder import host_block

    host = host_block(target, secrets=secrets, data_dir=data_dir)
    answer: dict[str, Any] = {"backup_id": backup_id, "server_id": job.server_id,
                              "certificate_name": certificate_name(job), "folder": folder,
                              "password_ref": job.env_secrets[PASSWORD_ENV], "steps": []}

    # 2. Hashed where it is, and read; the two must agree.
    hashed = {f["path"]: (f["sha256"], f.get("size_bytes"))
              for f in common_cli.run("file-checksum", {"host": host, "paths": [cer_path, pvk_path]}).get("files") or []}
    missing = [p for p in (cer_path, pvk_path) if p not in hashed]
    if missing:
        raise TakeRefused(f"{backup_id}: not on {job.server_id}: {', '.join(missing)} - nothing to take "
                          "(already taken and deleted? see `backup_restore certificates`).")
    pair: dict[str, bytes] = {}
    for path in (cer_path, pvk_path):
        read = common_cli.run("read-file", {"host": host, "path": path})
        data = base64.b64decode(read["base64"])
        size, sha = facts_of(data)
        if (sha, size) != (hashed[path][0], int(hashed[path][1] or -1)):
            raise TakeRefused(f"{path}: read as {size} bytes / {sha}, hashed on the server as "
                              f"{hashed[path][1]} bytes / {hashed[path][0]} - the two reads disagree; nothing kept.")
        pair[path] = data
    answer["thumbprint"] = thumbprint_of(pair[cer_path])
    answer["files"] = [{"path": p, "size_bytes": len(pair[p]), "sha256": facts_of(pair[p])[1]} for p in (cer_path, pvk_path)]
    answer["steps"].append("read: both files hashed on the server and read, the two reads agree")
    if not apply:
        answer["steps"].append("dry run: nothing kept, nothing deleted (--apply to keep)")
        return answer

    # 3. Kept, encrypted - and proven on a new connection.
    vault = vault or CertificateVault.from_config(app_config)
    row = vault.keep(server_id=job.server_id, certificate_name=certificate_name(job), cer=pair[cer_path],
                     pvk=pair[pvk_path], cer_source_path=cer_path, pvk_source_path=pvk_path,
                     password_ref=job.env_secrets[PASSWORD_ENV], passphrase=passphrase, taken_by=by,
                     backup_id=backup_id)
    answer["certificate_id"] = row["certificate_id"]
    answer["steps"].append("kept: already in the vault, same bytes" if row["already_kept"]
                           else f"kept: vault row {row['certificate_id']}, encrypted")
    row = vault.prove(int(row["certificate_id"]), passphrase=passphrase)
    answer["verified_at"] = row["verified_at"]
    answer["steps"].append("proven: read back, decrypted, sha256 + bytes + thumbprint equal")

    # 4. The physical copy: a sealed file on this node (the operator: in the vault AND as a file).
    from dbabrain.backup_restore import certificate_files

    try:
        sealed = certificate_files.seal_row(app_config, row, passphrase=passphrase, secrets=secrets,
                                            apply=True, vault=vault)
        answer["file"] = sealed["path"]
        answer["steps"].append(f"file {sealed['status']}: {sealed['path']}")
    except Exception as exc:  # noqa: BLE001 - the row stands; the file is said, and the delete gate holds
        answer["file_error"] = str(exc)
        answer["steps"].append(f"file: not written - {exc}")
    if not delete_at_source:
        return answer

    # 5. Off the server: one file per command, with the facts the vault proved - and only when the
    #    row's own file stands too, so two independent copies are left when the server's is gone.
    from dbabrain.db.lockdown_store import refuse_if_locked

    refuse_if_locked(app_config, "take-certificate --delete-at-source")
    try:
        certificate_files.require_proven_file(app_config, row, secrets=secrets)
    except Exception as exc:  # noqa: BLE001 - every reason is the same stop, said
        raise TakeRefused(f"nothing deleted: {exc}") from exc
    deleted = []
    for path, size, sha in ((cer_path, row["cer_size_bytes"], row["cer_sha256"]),
                            (pvk_path, row["pvk_size_bytes"], row["pvk_sha256"])):
        ok, data, error = common_cli.run_allowing_failure("delete-verified-file", {
            "host": host, "path": path, "file_name": path.replace("\\", "/").rsplit("/", 1)[-1],
            "size_bytes": int(size), "sha256": str(sha), "must_be_under": str(job.backup_dir)})
        line = (data or {}).get("file") or {}
        if not ok or line.get("status") not in ("deleted", "not_found"):
            raise TakeRefused(f"{path}: not deleted ({line.get('status') or 'refused'}: {line.get('reason') or error}) "
                              f"- the vault row {row['certificate_id']} is kept and proven; nothing else was touched"
                              + (f" after {', '.join(deleted)}" if deleted else "") + ".")
        deleted.append(path)
        answer["steps"].append(f"deleted on the server: {path} ({line.get('status')})")
    vault.mark_source_deleted(int(row["certificate_id"]))
    ok, data, error = common_cli.run_allowing_failure("delete-empty-folder", {
        "host": host, "path": folder, "must_be_under": str(job.backup_dir)})
    folder_line = (data or {}).get("folder") or {}
    answer["steps"].append(f"folder {folder}: {folder_line.get('status') or 'refused'}"
                           + (f" - {folder_line.get('reason')}" if folder_line.get("reason") else (f" - {error}" if not ok else "")))
    answer["catalog_rows_pruned"] = _prune_catalog_rows(app_config, catalog, job.server_id, deleted)
    return answer


def _prune_catalog_rows(app_config: Any, catalog: Any, server_id: str, paths: list[str]) -> int:
    """The catalog's rows for the deleted files say ``pruned`` - the scrub must not call them missing."""
    from dbabrain.db.backup_catalog import BackupCatalogStore

    catalog = catalog or BackupCatalogStore.from_config(app_config)
    catalog.initialize()
    marks = ", ".join("?" for _ in paths)
    with catalog.connect() as conn:
        rows = conn.execute(
            f"SELECT file_id FROM backup_files WHERE server_id = ? AND state = 'present' AND "
            f"(file_path IN ({marks}) OR host_path IN ({marks}))", (server_id, *paths, *paths)).fetchall()
    for row in rows:
        catalog.set_backup_file(int(row["file_id"]), state="pruned", on_disk=0,
                                pruned_by="take-certificate: kept in the vault, deleted on the server")
    return len(rows)


def held_thumbprints(app_config: Any, job: Any) -> str:
    """The ``BACKUP_CERT_HELD_THUMBPRINTS`` value for a job's run - '' when none, or when the vault
    cannot be read (the script then exports as it always did: never less safe than before)."""
    if str(getattr(job, "db_type", "")).lower() != "sqlserver" or not (job.env_secrets or {}).get(PASSWORD_ENV):
        return ""
    try:
        return ",".join(CertificateVault.from_config(app_config).held_thumbprints(
            server_id=job.server_id, certificate_name=certificate_name(job)))
    except Exception:  # noqa: BLE001 - see docstring
        return ""


def vault_pair(app_config: Any, *, server_id: str, certificate_name: str, passphrase: str
               ) -> tuple[bytes, bytes, dict[str, Any]] | None:
    """The newest proven pair the vault holds for that certificate, or ``None``."""
    try:
        vault = CertificateVault.from_config(app_config)
        row = vault.find(server_id=server_id, certificate_name=certificate_name)
    except Exception:  # noqa: BLE001 - a vault that cannot be read is "not in the vault"; the caller says so
        return None
    if row is None:
        return None
    try:
        cer, pvk, row = vault.pair(int(row["certificate_id"]), passphrase=passphrase)
    except CertificateVaultError:
        return None
    return cer, pvk, row
