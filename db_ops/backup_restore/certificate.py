from __future__ import annotations
from db_ops.backup_restore.shell_quoting import _build_sqlcmd_auth_args, _escape_identifier, _escape_sql_string, _ps_array, _ps_quote  # noqa: F401 - one definition

import base64
import io
import json
import shlex
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib import request

from db_ops.backup_restore.config import BackupRestoreConfig, validate_restore_target_is_not_source
from db_ops.backup_restore.copy_backup import resolve_password_ref
from db_ops.backup_restore.sanitize import sanitize_text
from db_ops.logging_ops import log_event


@dataclass(frozen=True)
class BackupCertificate:
    certificate_name: str
    thumbprint: str
    certificate_base64: str
    private_key_base64: str
    private_key_password: str


def ensure_source_certificate(
    *,
    config: BackupRestoreConfig,
    dry_run: bool = False,
    logger: object | None = None,
) -> dict[str, object]:
    if not config.certificate_api_url:
        return {"status": "SKIPPED_NO_CERTIFICATE_API", "source_id": config.source_id}

    if dry_run:
        return {
            "status": "DRY_RUN",
            "source_id": config.source_id,
            "certificate_api_url": config.certificate_api_url,
        }

    validate_restore_target_is_not_source(config)
    certificate = fetch_backup_certificate(config)
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
    return {
        "status": "SUCCESS",
        "source_id": config.source_id,
        "certificate_name": certificate.certificate_name,
        "thumbprint": certificate.thumbprint,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def fetch_backup_certificate(config: BackupRestoreConfig) -> BackupCertificate:
    token = resolve_password_ref(config.certificate_api_token_ref)
    if not token:
        raise RuntimeError(f"Password ref not found in environment or secret_text.json: {config.certificate_api_token_ref}")

    http_request = request.Request(
        config.certificate_api_url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    context = None if config.certificate_api_verify_tls else ssl._create_unverified_context()
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
        raise RuntimeError(f"Certificate API result is missing required fields: {', '.join(missing)}")
    base64.b64decode(certificate.certificate_base64, validate=True)
    base64.b64decode(certificate.private_key_base64, validate=True)
    return certificate


#: What the Windows target runs: write the certificate and its key into the import folder, then
#: CREATE CERTIFICATE from them. The values are assigned at the top rather than passed as
#: arguments, because the script travels in a run-cmd request on stdin - there is no argv.
_WINDOWS_IMPORT_BODY = (
    "$ErrorActionPreference = 'Stop'",
    "$certDir = Join-Path $CertRoot '__db_ops_cert'",
    "Write-Output ('CERT_IMPORT: creating cert dir: ' + $certDir)",
    "New-Item -ItemType Directory -Force -Path $certDir | Out-Null",
    "$safeName = ($CerName -replace '[^A-Za-z0-9_.-]', '_')",
    "$cerPath = Join-Path $certDir ($safeName + '.cer')",
    "$pvkPath = Join-Path $certDir ($safeName + '.pvk')",
    "Write-Output ('CERT_IMPORT: writing cer file: ' + $cerPath)",
    "[IO.File]::WriteAllBytes($cerPath, [Convert]::FromBase64String($CerBase64))",
    "Write-Output ('CERT_IMPORT: writing pvk file: ' + $pvkPath)",
    "[IO.File]::WriteAllBytes($pvkPath, [Convert]::FromBase64String($PvkBase64))",
    "$cerSqlPath = $cerPath.Replace(\"'\", \"''\")",
    "$pvkSqlPath = $pvkPath.Replace(\"'\", \"''\")",
    "$Sql = $Sql.Replace('__DB_OPS_CERT_FILE__', $cerSqlPath).Replace('__DB_OPS_PVK_FILE__', $pvkSqlPath)",
    "Write-Output ('CERT_IMPORT: running sqlcmd against: ' + $SqlInstance)",
    "& $SqlcmdPath -S $SqlInstance -C @SqlAuthArgs -b -Q $Sql",
    "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
    "Write-Output ('CERT_IMPORT: completed for certificate: ' + $CerName)",
)


def build_add_certificate_script(*, certificate: BackupCertificate, config: BackupRestoreConfig) -> str:
    """The PowerShell a Windows restore target runs to import the certificate (``run-cmd``, WinRM)."""
    values = {
        "SqlcmdPath": config.sqlcmd_path,
        "SqlInstance": config.restore_sql_instance_on_vm,
        "Sql": build_add_certificate_sql(certificate),
        "CerBase64": certificate.certificate_base64,
        "PvkBase64": certificate.private_key_base64,
        "CerName": certificate.certificate_name,
        "CertRoot": str(config.vm_import_local),
    }
    lines = [f"${name} = {_ps_quote(str(value))}" for name, value in values.items()]
    lines.append(f"$sqlAuthArgs = @({_ps_array(_build_sqlcmd_auth_args(config))})")
    return "\n".join([*lines, *_WINDOWS_IMPORT_BODY])


def build_add_certificate_command(*, certificate: BackupCertificate, config: BackupRestoreConfig) -> list[str]:
    """The local ``sqlcmd`` argv for a restore on this machine - the files written here first.

    A remote Windows target runs :func:`build_add_certificate_script` through ``run-cmd`` instead.
    """
    sql = build_add_certificate_sql(certificate)
    sql_auth_args = _build_sqlcmd_auth_args(config)
    cert_dir = config.vm_import_local / "__db_ops_cert"
    cert_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(char if char.isalnum() or char in ("_", "-", ".") else "_" for char in certificate.certificate_name)
    cer_path = cert_dir / f"{safe_name}.cer"
    pvk_path = cert_dir / f"{safe_name}.pvk"
    cer_path.write_bytes(base64.b64decode(certificate.certificate_base64))
    pvk_path.write_bytes(base64.b64decode(certificate.private_key_base64))
    return [
        config.sqlcmd_path,
        "-S",
        config.restore_sql_instance_on_vm,
        "-C",
        *sql_auth_args,
        "-b",
        "-Q",
        sql.replace("__DB_OPS_CERT_FILE__", _escape_sql_string(str(cer_path))).replace(
            "__DB_OPS_PVK_FILE__",
            _escape_sql_string(str(pvk_path)),
        ),
    ]


def build_add_certificate_sql(certificate: BackupCertificate) -> str:
    thumbprint = certificate.thumbprint
    if not thumbprint.lower().startswith("0x"):
        thumbprint = f"0x{thumbprint}"
    return f"""
USE master;

-- A fresh target has no Database Master Key; importing a certificate with a
-- private key requires one. Create it (also protected by the Service Master Key,
-- so it opens automatically) when missing.
IF NOT EXISTS (SELECT 1 FROM master.sys.symmetric_keys WHERE name = N'##MS_DatabaseMasterKey##')
BEGIN
    CREATE MASTER KEY ENCRYPTION BY PASSWORD = N'{_escape_sql_string(certificate.private_key_password)}';
END;

IF NOT EXISTS
(
    SELECT 1
    FROM sys.certificates
    WHERE name = N'{_escape_sql_string(certificate.certificate_name)}'
       OR CONVERT(varchar(66), thumbprint, 1) = '{_escape_sql_string(thumbprint)}'
)
BEGIN
    CREATE CERTIFICATE [{_escape_identifier(certificate.certificate_name)}]
    FROM FILE = N'__DB_OPS_CERT_FILE__'
    WITH PRIVATE KEY
    (
        FILE = N'__DB_OPS_PVK_FILE__',
        DECRYPTION BY PASSWORD = N'{_escape_sql_string(certificate.private_key_password)}'
    );
END;
""".strip()


def _run_add_certificate_linux_via_ssh(certificate: BackupCertificate, config: BackupRestoreConfig) -> subprocess.CompletedProcess[str]:
    """Import the backup encryption certificate on a Linux SQL Server host via SSH+SFTP."""
    from db_ops.backup_restore.copy_backup import open_ssh_connection

    sql = build_add_certificate_sql(certificate)
    safe_name = "".join(char if char.isalnum() or char in ("_", "-", ".") else "_" for char in certificate.certificate_name)
    linux_import = str(config.vm_import_local).replace("\\", "/")
    cert_dir = f"{linux_import}/__db_ops_cert"
    cer_path = f"{cert_dir}/{safe_name}.cer"
    pvk_path = f"{cert_dir}/{safe_name}.pvk"
    cer_bytes = base64.b64decode(certificate.certificate_base64)
    pvk_bytes = base64.b64decode(certificate.private_key_base64)
    sql_auth_args = _build_sqlcmd_auth_args(config)

    with open_ssh_connection(config) as ssh:
        # Written on the target from memory - the private key never lands on this machine's disk.
        ssh.put_bytes(cer_bytes, cer_path)
        ssh.put_bytes(pvk_bytes, pvk_path)
        final_sql = (
            sql.replace("__DB_OPS_CERT_FILE__", _escape_sql_string(cer_path))
               .replace("__DB_OPS_PVK_FILE__", _escape_sql_string(pvk_path))
        )
        auth_str = " ".join(shlex.quote(a) for a in sql_auth_args)
        remote_cmd = (
            "export PATH=$PATH:/opt/mssql-tools/bin:/opt/mssql-tools18/bin; "
            f"{shlex.quote(config.sqlcmd_path)} "
            f"-S {shlex.quote(config.restore_sql_instance_on_vm)} "
            f"-C {auth_str} -b "
            f"-Q {shlex.quote(final_sql)}"
        )
        answer = ssh.run(remote_cmd)
        stdout_data, stderr_data, rc = answer.stdout, answer.stderr, answer.exit_code

    if rc != 0:
        details = [f"Certificate import command failed with exit code {rc}."]
        stdout_text = sanitize_text(stdout_data.strip())
        stderr_text = sanitize_text(stderr_data.strip())
        if stdout_text:
            details.append(f"stdout:\n{stdout_text}")
        if stderr_text:
            details.append(f"stderr:\n{stderr_text}")
        if not stdout_text and not stderr_text:
            details.append("No stdout/stderr was returned by SSH sqlcmd.")
        raise RuntimeError("\n".join(details))
    return subprocess.CompletedProcess(
        args=["__ssh_cert_import__"],
        returncode=rc,
        stdout=stdout_data,
        stderr=stderr_data,
    )


def _run_add_certificate_command(
    cmd: list[str],
    *,
    timeout_seconds: int = 600,
    config: BackupRestoreConfig | None = None,
) -> subprocess.CompletedProcess[str]:
    """Import on this machine: the local ``sqlcmd`` argv, run through ``common.cli run-sqlcmd`` (R10)."""
    if config is not None and config.is_linux:
        raise RuntimeError(
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
            raise RuntimeError(f"Password ref not found in environment or secret_text.json: {config.vm_password_env}")
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
        raise RuntimeError("\n".join(details))
    if "exit_code" not in data:
        raise RuntimeError(f"Certificate import command could not be run: {error or 'no answer'}")
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
    raise RuntimeError("\n".join(details))


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










