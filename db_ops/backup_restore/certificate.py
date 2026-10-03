from __future__ import annotations

from db_ops.lib import errors
from db_ops.backup_restore.shell_quoting import _build_sqlcmd_auth_args, _escape_identifier, _escape_sql_string, _ps_array, _ps_quote  # noqa: F401 - one definition

import base64
import hashlib
import io
import json
import shlex
import ssl
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib import request

from db_ops.backup_restore.config import BackupRestoreConfig, validate_restore_target_is_not_source
from db_ops.backup_restore.copy_backup import resolve_password_ref
from db_ops.backup_restore.sanitize import sanitize_text
from db_ops.lib import sqlserver_certificate
from db_ops.logging_ops import log_event


@dataclass(frozen=True)
class BackupCertificate:
    certificate_name: str
    thumbprint: str
    certificate_base64: str
    private_key_base64: str
    private_key_password: str


def has_certificate_source(config: BackupRestoreConfig) -> bool:
    """Does this entry say where its backups' certificate comes from - a Vault URL, or the pair
    dbabrain's own backup job exports beside the backups (``backup_certificate``)?"""
    return bool(config.certificate_api_url) or config.backup_certificate is not None


def ensure_source_certificate(
    *,
    config: BackupRestoreConfig,
    dry_run: bool = False,
    logger: object | None = None,
) -> dict[str, object]:
    if not has_certificate_source(config):
        return {"status": "SKIPPED_NO_CERTIFICATE_API", "source_id": config.source_id}

    if dry_run:
        if config.certificate_api_url:
            return {"status": "DRY_RUN", "source_id": config.source_id,
                    "certificate_api_url": config.certificate_api_url}
        return {"status": "DRY_RUN", "source_id": config.source_id,
                "certificate_source": _share_cert_path(config, "cer"),
                "certificate_source_dir": config.backup_certificate.source_dir or None}

    validate_restore_target_is_not_source(config)
    certificate = (fetch_backup_certificate(config) if config.certificate_api_url
                   else read_backup_certificate_pair(config, logger=logger))
    if config.is_linux:
        _log_remote_command(config, logger=logger, remote_exec_type="ssh", command_phase="certificate-import")
        result = _run_add_certificate_linux_via_ssh(certificate, config)
    else:
        _log_remote_command(
            config,
            logger=logger,
            remote_exec_type="powershell" if config.vm_credential_target else "local",
            command_phase="certificate-import",
        )
        if config.vm_credential_target:
            result = _run_add_certificate_over_winrm(certificate, config)
        else:
            result = _run_add_certificate_command(
                build_add_certificate_command(certificate=certificate, config=config), config=config)
    # The name that holds the thumbprint NOW - the requested one, one already there, or the
    # requested one with the thumbprint's first digits when that name belonged to another key.
    held = sqlserver_certificate.parse_marker(result.stdout) or {}
    return {
        "status": "SUCCESS",
        "source_id": config.source_id,
        "certificate_name": held.get("certificate_name") or certificate.certificate_name,
        "thumbprint": held.get("thumbprint") or certificate.thumbprint,
        "imported": held.get("imported"),
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _share_cert_path(config: BackupRestoreConfig, suffix: str) -> str:
    """``<share>\\_cert\\<name>.<suffix>`` - where the backup job exports the pair."""
    source = config.backup_certificate
    name = source.name if source is not None else ""
    return str(config.prod_backup_share).replace("/", "\\").rstrip("\\") + f"\\_cert\\{name}.{suffix}"


def read_backup_certificate_pair(config: BackupRestoreConfig, *, logger: object | None = None) -> BackupCertificate:
    """The pair dbabrain's backup job exported beside the backups, read into memory.

    From the share first, with the same login the backups are copied with. A pair exported before
    0.24.1 is readable by the SQL Server service account only, and the share refuses it - then
    ``source_dir`` is read over the SOURCE host's own login (``run-cmd``), where an administrator's
    session can. The thumbprint is the SHA-1 of the ``.cer``, so it is computed here, not asked for.
    """
    source = config.backup_certificate
    if source is None:
        raise errors.NotConfigured(f"restore_id={config.restore_id}: no backup_certificate on this entry.")
    password = resolve_password_ref(source.password_ref)
    if not password:
        raise errors.NotConfigured(f"Password ref not found in environment or secret_text.json: {source.password_ref}")
    try:
        cer, pvk = _pair_from_share(config)
        where = _share_cert_path(config, "cer")
    except Exception as share_error:  # noqa: BLE001 - refused, missing, unreachable: all the same next step.
        if not source.source_dir:
            raise errors.OperationFailed(
                f"cannot read the backup certificate from {_share_cert_path(config, 'cer')}: {share_error}. "
                "A pair exported before 0.24.1 is readable by the SQL Server service account only - run "
                "the backup once on this version, or name source.backup_certificate.source_dir to read "
                "it over the source host's login.") from share_error
        if logger:
            log_event(logger, level="logging", message=sanitize_text(
                f"restore_id={config.restore_id} certificate share read refused ({share_error}); "
                f"reading {source.source_dir} over the source host's login"))
        cer, pvk = _pair_from_source_host(config)
        where = source.source_dir
    if logger:
        log_event(logger, level="logging", message=sanitize_text(
            f"restore_id={config.restore_id} certificate {source.name} read from {where}"))
    return BackupCertificate(
        certificate_name=source.name,
        thumbprint=hashlib.sha1(cer).hexdigest(),  # noqa: S324 - SQL Server's thumbprint IS the SHA-1.
        certificate_base64=base64.b64encode(cer).decode("ascii"),
        private_key_base64=base64.b64encode(pvk).decode("ascii"),
        private_key_password=password,
    )


def _pair_from_share(config: BackupRestoreConfig) -> tuple[bytes, bytes]:
    from db_ops.backup_restore import share
    from db_ops.backup_restore.copy_backup import copy_share_login_requests, store_share_logins

    if sys.platform.startswith("win"):
        # A UNC path on Windows opens with the login cmdkey holds - stored first, as the copy does.
        store_share_logins(copy_share_login_requests(config))
    password = resolve_password_ref(config.prod_smb_password_env) if config.prod_smb_password_env else ""
    pair: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="db_ops_cert_") as stage:
        for suffix in ("cer", "pvk"):
            local = Path(stage) / f"pair.{suffix}"
            answer = share.get_file(
                config.prod_backup_share, share.share_relative(config.prod_backup_share, _share_cert_path(config, suffix)),
                local, username=config.prod_smb_username, password=password, timeout_seconds=120)
            if int(answer.get("exit_code") or 0) != 0 or not local.is_file() or local.stat().st_size == 0:
                raise errors.OperationFailed(str(answer.get("detail") or f"smb-get exit {answer.get('exit_code')}"))
            pair.append(local.read_bytes())
    return pair[0], pair[1]


def _pair_from_source_host(config: BackupRestoreConfig) -> tuple[bytes, bytes]:
    from db_ops.lib.data_sources.request_fill import host_access
    from db_ops.transport import common_cli

    source = config.backup_certificate
    access = host_access(config.source_id)
    windows = str(access.get("platform") or "").lower() == "windows"
    pair: list[bytes] = []
    for suffix in ("cer", "pvk"):
        if windows:
            path = source.source_dir.rstrip("\\/") + f"\\{source.name}.{suffix}"
            command = "[Convert]::ToBase64String([IO.File]::ReadAllBytes(" + _ps_quote(path) + "))"
        else:
            path = source.source_dir.rstrip("/") + f"/{source.name}.{suffix}"
            command = "base64 -w0 " + shlex.quote(path)
        ok, data, error = common_cli.run_allowing_failure("run-cmd", {
            "access": access, "target": config.source_id, "command": command,
            "timeout_seconds": 120, "confirm": True, "assume_yes": True})
        if not ok or int(data.get("exit_code") or 0) != 0:
            detail = (str(data.get("stderr") or "") or error).strip()[:300]
            raise errors.OperationFailed(f"cannot read {path} on {config.source_id}: {detail}")
        pair.append(base64.b64decode("".join(str(data.get("stdout") or "").split())))
    return pair[0], pair[1]


def fetch_backup_certificate(config: BackupRestoreConfig) -> BackupCertificate:
    token = resolve_password_ref(config.certificate_api_token_ref)
    if not token:
        raise errors.NotConfigured(f"Password ref not found in environment or secret_text.json: {config.certificate_api_token_ref}")

    http_request = request.Request(
        config.certificate_api_url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    if config.certificate_api_verify_tls:
        ca_file = str(getattr(config, "certificate_api_ca_file", "") or "")
        context = ssl.create_default_context(cafile=ca_file) if ca_file else None
    else:
        # Stated in the entry, never the default (review 0.25.0, B5.4) - and said every time.
        print(f"WARNING: {config.certificate_api_url}: TLS verification is off "
              "(certificate_api_verify_tls: false) - the Vault token and the certificate's key "
              "travel to whoever answers.", file=sys.stderr)
        context = ssl._create_unverified_context()
    with request.urlopen(http_request, timeout=30, context=context) as response:  # noqa: S310 - internal Vault endpoint.
        raw = json.loads(response.read().decode("utf-8-sig"))
    data = raw.get("data", {}).get("data", {}) if isinstance(raw, dict) else {}
    return parse_backup_certificate(data)


def parse_backup_certificate(data: dict[str, object]) -> BackupCertificate:
    certificate = BackupCertificate(
        certificate_name=str(data.get("certificate_name") or "").strip(),
        thumbprint=str(data.get("thumbprint") or "").strip(),
        certificate_base64=str(data.get("backup_cert_base64") or "").strip(),
        private_key_base64=str(data.get("backup_private_key_base64") or "").strip(),
        private_key_password=str(data.get("private_key_password") or ""),
    )
    missing = [
        name
        for name, value in (
            ("certificate_name", certificate.certificate_name),
            ("thumbprint", certificate.thumbprint),
            ("backup_cert_base64", certificate.certificate_base64),
            ("backup_private_key_base64", certificate.private_key_base64),
            ("private_key_password", certificate.private_key_password),
        )
        if not value
    ]
    if missing:
        raise errors.OperationFailed(f"Certificate API result is missing required fields: {', '.join(missing)}")
    base64.b64decode(certificate.certificate_base64, validate=True)
    base64.b64decode(certificate.private_key_base64, validate=True)
    return certificate


#: What the Windows target runs: write the certificate and its key into the import folder, import
#: them with the one batch (its paths already in it), then remove the pair. The values are assigned
#: at the top rather than passed as arguments, because the script travels in a run-cmd request on
#: stdin - there is no argv.
_WINDOWS_IMPORT_BODY = (
    "$ErrorActionPreference = 'Stop'",
    "Write-Output ('CERT_IMPORT: creating cert dir: ' + (Split-Path $CerPath))",
    "New-Item -ItemType Directory -Force -Path (Split-Path $CerPath) | Out-Null",
    "Write-Output ('CERT_IMPORT: writing cer file: ' + $CerPath)",
    "[IO.File]::WriteAllBytes($CerPath, [Convert]::FromBase64String($CerBase64))",
    "Write-Output ('CERT_IMPORT: writing pvk file: ' + $PvkPath)",
    "[IO.File]::WriteAllBytes($PvkPath, [Convert]::FromBase64String($PvkBase64))",
    "Write-Output ('CERT_IMPORT: running sqlcmd against: ' + $SqlInstance)",
    "try {",
    "    & $SqlcmdPath -S $SqlInstance -C @SqlAuthArgs -b -Q $Sql",
    "    $code = $LASTEXITCODE",
    "} finally {",
    "    Remove-Item -LiteralPath $CerPath, $PvkPath -Force -ErrorAction SilentlyContinue",
    "}",
    "if ($code -ne 0) { exit $code }",
    "Write-Output ('CERT_IMPORT: completed for certificate: ' + $CerName)",
)


def _safe_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in ("_", "-", ".") else "_" for char in name)


def certificate_file_paths(certificate: BackupCertificate, config: BackupRestoreConfig) -> tuple[str, str]:
    """Where the pair is written for the import - the target's staging folder, as the INSTANCE
    reads it (``vm_import_local``; on Linux the import path, which a container binds at the same
    path)."""
    safe = _safe_name(certificate.certificate_name)
    if config.is_linux:
        cert_dir = str(config.vm_import_local).replace("\\", "/").rstrip("/") + "/__db_ops_cert"
        return f"{cert_dir}/{safe}.cer", f"{cert_dir}/{safe}.pvk"
    cert_dir = PureWindowsPath(str(config.vm_import_local)) / "__db_ops_cert"
    return str(cert_dir / f"{safe}.cer"), str(cert_dir / f"{safe}.pvk")


def build_add_certificate_script(*, certificate: BackupCertificate, config: BackupRestoreConfig) -> str:
    """The PowerShell a Windows restore target runs to import the certificate (``run-cmd``, WinRM)."""
    cer_path, pvk_path = certificate_file_paths(certificate, config)
    values = {
        "SqlcmdPath": config.sqlcmd_path,
        "SqlInstance": config.restore_sql_instance_on_vm,
        "Sql": build_add_certificate_sql(certificate, cer_path=cer_path, pvk_path=pvk_path),
        "CerBase64": certificate.certificate_base64,
        "PvkBase64": certificate.private_key_base64,
        "CerName": certificate.certificate_name,
        "CerPath": cer_path,
        "PvkPath": pvk_path,
    }
    lines = [f"${name} = {_ps_quote(str(value))}" for name, value in values.items()]
    auth = _build_sqlcmd_auth_args(config)
    if "-P" in auth:
        # sqlcmd reads SQLCMDPASSWORD when there is no -P: the password stays in this script body
        # (sent on stdin) and never becomes sqlcmd's command line on the target (review 0.25.0, F11.2).
        at = auth.index("-P")
        lines.append(f"$env:SQLCMDPASSWORD = {_ps_quote(auth[at + 1])}")
        auth = auth[:at] + auth[at + 2:]
    lines.append(f"$sqlAuthArgs = @({_ps_array(auth)})")
    return "\n".join([*lines, *_WINDOWS_IMPORT_BODY])


def build_add_certificate_command(*, certificate: BackupCertificate, config: BackupRestoreConfig) -> list[str]:
    """The local ``sqlcmd`` argv for a restore on this machine - the files written here first.

    A remote Windows target runs :func:`build_add_certificate_script` through ``run-cmd`` instead.
    """
    cer_path, pvk_path = (Path(path) for path in certificate_file_paths(certificate, config))
    cer_path.parent.mkdir(parents=True, exist_ok=True)
    cer_path.write_bytes(base64.b64decode(certificate.certificate_base64))
    pvk_path.write_bytes(base64.b64decode(certificate.private_key_base64))
    return [
        config.sqlcmd_path,
        "-S",
        config.restore_sql_instance_on_vm,
        "-C",
        *_build_sqlcmd_auth_args(config),
        "-b",
        "-Q",
        build_add_certificate_sql(certificate, cer_path=str(cer_path), pvk_path=str(pvk_path)),
    ]


def build_add_certificate_sql(certificate: BackupCertificate, *, cer_path: str, pvk_path: str) -> str:
    """The one batch (:mod:`db_ops.lib.sqlserver_certificate`): by thumbprint, never dropping.

    This used to skip the import when a certificate of the same NAME existed - and a target with
    its own ``db_ops_backup_cert`` then never received the source's, so its restore failed on the
    thumbprint (2026-09-27, a lab VM).
    """
    return sqlserver_certificate.import_batch(
        name=certificate.certificate_name, cer_path=cer_path, pvk_path=pvk_path,
        password=certificate.private_key_password)


def _run_add_certificate_linux_via_ssh(certificate: BackupCertificate, config: BackupRestoreConfig) -> subprocess.CompletedProcess[str]:
    """Import on a Linux target: the pair written into its staging folder, the batch run where the
    SQL Server is (``run-sqlcmd`` - inside its container when the entry names one), the pair
    removed again."""
    from db_ops.backup_restore.copy_backup import open_ssh_connection
    from db_ops.backup_restore.restore_database import _run_sqlcmd_via_ssh, build_sqlcmd_query_command

    cer_path, pvk_path = certificate_file_paths(certificate, config)
    with open_ssh_connection(config) as ssh:
        # Written on the target from memory - the private key never lands on this machine's disk.
        ssh.put_bytes(base64.b64decode(certificate.certificate_base64), cer_path)
        ssh.put_bytes(base64.b64decode(certificate.private_key_base64), pvk_path)
        try:
            result = _run_sqlcmd_via_ssh(
                build_sqlcmd_query_command(
                    sql=build_add_certificate_sql(certificate, cer_path=cer_path, pvk_path=pvk_path),
                    config=config),
                config)
        finally:
            ssh.run(f"rm -f {shlex.quote(cer_path)} {shlex.quote(pvk_path)}")

    if result.returncode != 0:
        details = [f"Certificate import command failed with exit code {result.returncode}."]
        stdout_text = sanitize_text(str(result.stdout or "").strip())
        stderr_text = sanitize_text(str(result.stderr or "").strip())
        if stdout_text:
            details.append(f"stdout:\n{stdout_text}")
        if stderr_text:
            details.append(f"stderr:\n{stderr_text}")
        if not stdout_text and not stderr_text:
            details.append("No stdout/stderr was returned by SSH sqlcmd.")
        raise errors.OperationFailed("\n".join(details))
    return subprocess.CompletedProcess(
        args=["__ssh_cert_import__"], returncode=0, stdout=result.stdout, stderr=result.stderr)


def _run_add_certificate_command(
    cmd: list[str],
    *,
    timeout_seconds: int = 600,
    config: BackupRestoreConfig | None = None,
) -> subprocess.CompletedProcess[str]:
    """Import on this machine: the local ``sqlcmd`` argv, run through ``common.cli run-sqlcmd`` (R10)."""
    if config is not None and config.is_linux:
        raise errors.Refused(
            f"Target context mismatch: restore_id={config.restore_id} target_host={config.vm_credential_target} "
            "target_os_type=linux cannot execute a local certificate import."
        )
    from db_ops.backup_restore.restore_database import _local_request_from_argv
    from db_ops.transport import common_cli

    ok, data, error = common_cli.run_allowing_failure(
        "run-sqlcmd", _local_request_from_argv(cmd, timeout_seconds=timeout_seconds))
    return _certificate_outcome(cmd, ok=ok, data=data, error=error, timeout_seconds=timeout_seconds)


def _run_add_certificate_over_winrm(
    certificate: BackupCertificate,
    config: BackupRestoreConfig,
    *,
    timeout_seconds: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Import on a Windows target: the script, run there by ``common.cli run-cmd`` over WinRM (R10).

    It used to be a local PowerShell ``Invoke-Command`` whose script carried the password on this
    machine's command line; the request now travels on stdin.
    """
    password = ""
    if config.vm_username and config.vm_password_env:
        password = resolve_password_ref(config.vm_password_env)
        if not password:
            raise errors.NotConfigured(f"Password ref not found in environment or secret_text.json: {config.vm_password_env}")
    from db_ops.transport import common_cli

    request = {
        "access": {"method": "winrm", "host": config.vm_credential_target, "platform": "windows",
                   "shell": "powershell", "username": config.vm_username if password else "",
                   "password": password, "timeout_seconds": config.remote_command_timeout_seconds},
        "platform": "windows",
        "script": build_add_certificate_script(certificate=certificate, config=config),
        "timeout_seconds": timeout_seconds,
        "confirm": True,
        "assume_yes": True,
    }
    ok, data, error = common_cli.run_allowing_failure("run-cmd", request)
    return _certificate_outcome(["__winrm_cert_import__"], ok=ok, data=data, error=error,
                                timeout_seconds=timeout_seconds)


def _certificate_outcome(cmd: list[str], *, ok: bool, data: dict, error: str,
                         timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    """The import's answer as the process it used to be - or the error the caller has always seen."""
    stdout = sanitize_text(str(data.get("stdout") or "").strip())
    stderr = sanitize_text(str(data.get("stderr") or "").strip())
    if data.get("timed_out"):
        details = [f"Certificate import command timed out after {timeout_seconds} seconds."]
        details += [f"stdout:\n{stdout}"] if stdout else []
        details += [f"stderr:\n{stderr}"] if stderr else []
        raise errors.OperationFailed("\n".join(details))
    if "exit_code" not in data:
        raise errors.OperationFailed(f"Certificate import command could not be run: {error or 'no answer'}")
    exit_code = int(data.get("exit_code") or 0)
    if ok and exit_code == 0:
        return subprocess.CompletedProcess(cmd, 0, str(data.get("stdout") or ""), str(data.get("stderr") or ""))
    details = [f"Certificate import command failed with exit code {exit_code}."]
    if stdout:
        details.append(f"stdout:\n{stdout}")
    if stderr:
        details.append(f"stderr:\n{stderr}")
    if not stdout and not stderr:
        details.append("No stdout/stderr was returned by PowerShell/sqlcmd.")
    raise errors.OperationFailed("\n".join(details))


def _log_remote_command(
    config: BackupRestoreConfig,
    *,
    logger: object | None,
    remote_exec_type: str,
    command_phase: str,
) -> None:
    if logger:
        log_event(
            logger,
            level="logging",
            message=(
                f"certificate remote-command restore_id={config.restore_id} target_id={config.target_id} "
                f"target_host={config.vm_credential_target or 'local'} target_os_type={config.vm_platform} "
                f"remote_exec_type={remote_exec_type} sql_instance={config.restore_sql_instance_on_vm} "
                f"command_phase={command_phase}"
            ),
        )










