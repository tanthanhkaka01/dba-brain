"""Applying a SQL Server **Cumulative Update** to an instance, and proving it landed.

This is the Windows/SQL-Server-specific half of what ORD 129 needed on 2026-08-03; everything
about it that is *not* SQL Server — reaching the host, reading its state, restarting it, waiting
for services, the gate/evidence model — is in :mod:`db_ops.common.host_ops` and
:mod:`db_ops.common.evidence`, so a task that only needs a restart does not drag a patcher in.
The full CU is composed from the small pieces::

    precheck  ->  restart  ->  precheck  ->  apply-cu  ->  restart  ->  verify-build

Three findings from that night are encoded here rather than left to the next operator:

* **`SERVERPROPERTY` is authoritative; the registry is corroboration** — and the registry value
  to read is ``PatchLevel``, not ``Version``. ``Version`` is the build *originally installed*
  and never moves when a CU is applied, so comparing it against the target build reported FAIL
  on every successful CU on every instance. See :func:`verify_build`.
* **Exit code 3010 is a success**, meaning *patch applied, restart required*. It must never be
  retried; the correct response is the second restart.
* **The pending-reboot check is a real blocker.** SQL Server setup evaluates
  ``PendingFileRenameOperations`` in its ``RebootRequiredCheck`` rule and refuses to patch while
  it is populated — 307 entries from the print spooler cost the CU26 window its first restart.
  ``precheck`` catches it read-only, before anything is written.

**Input is a JSON object**, resolving both halves of the target from one ``server_id``: the host
(``cmd_access``) and the instance (the SQL login)::

    {
      "target": "ACME-192-0-2-250",
      "installer": "D:\\\\Softwares\\\\SQLServer2022-KB5093420-x64.exe",
      "expected_build": "16.0.4265.3",
      "installer_sha256": "A0FA...",       // optional; skipped with "skip_hash": true
      "kb": "KB5093420",                    // optional, recorded in the evidence
      "credential_name": "...",             // optional SQL login; default = the instance's
      "setup_account": "APPDB-DB\\\\appdbadmin",  // optional; must be a sysadmin
      "window": {"start": "...", "end": "..."},
      "confirm": true, "dry_run": false,
      "overrides": ["allow-stale-backup"]
    }
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from db_ops.common import host_ops, sql_run
from db_ops.common.evidence import FAIL, OK, WARN, GateReport
from db_ops.common.remote_exec import quote_powershell

__all__ = [
    "SqlServerPatchError",
    "apply_cu",
    "patch_arguments",
    "patch_exit_verdict",
    "precheck",
    "setup_log_root",
    "sqlserver_registry_key",
    "sqlserver_service_names",
    "verify_build",
    "version_tuple",
]

# Setup's own exit codes. 3010 is the one that matters: the patch succeeded and the host needs
# a restart to finalise it. Treating it as a failure (and re-running setup) is the classic way
# to turn a successful CU into an incident.
EXIT_SUCCESS = 0
EXIT_SUCCESS_RESTART_REQUIRED = 3010


class SqlServerPatchError(RuntimeError):
    """A user-facing failure: unknown target, no installer path, unreadable setup output."""


# --------------------------------------------------------------------------- #
# Naming rules (major version -> registry key, service names, log root)
# --------------------------------------------------------------------------- #
def _major_version(build: str) -> int:
    """The major version number out of a build string (``16.0.4265.3`` -> 16)."""
    match = re.match(r"\s*(\d+)", str(build or ""))
    return int(match.group(1)) if match else 0


def sqlserver_registry_key(build: str, instance_name: str = "") -> str:
    """The ``MSSQL<major>.<instance>`` key holding an instance's setup registry values.

    A default instance is registered as ``MSSQLSERVER``, not as an empty name — deriving this
    rather than asking for it is why the caller does not have to know that.
    """
    instance = str(instance_name or "").strip() or "MSSQLSERVER"
    return f"MSSQL{_major_version(build)}.{instance}"


def sqlserver_service_names(instance_name: str = "", *, include_browser: bool = True) -> list[str]:
    """The Windows services one instance owns.

    Named instances suffix the instance (``MSSQL$APPDB`` / ``SQLAgent$APPDB``); the default
    instance uses the two fixed names. SQL Browser is instance-independent but is what lets
    clients find a named instance at all, so it is included by default.
    """
    instance = str(instance_name or "").strip()
    if instance and instance.upper() != "MSSQLSERVER":
        names = [f"MSSQL${instance}", f"SQLAgent${instance}"]
    else:
        names = ["MSSQLSERVER", "SQLSERVERAGENT"]
    if include_browser:
        names.append("SQLBrowser")
    return names


def setup_log_root(build: str) -> str:
    """Where setup writes ``Summary.txt`` and its per-run log directories."""
    return rf"C:\Program Files\Microsoft SQL Server\{_major_version(build)}0\Setup Bootstrap\Log"


def version_tuple(value: str) -> tuple[int, ...]:
    """``16.0.4265.3`` -> ``(16, 0, 4265, 3)``, for ordering builds."""
    parts: list[int] = []
    for chunk in str(value or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


# --------------------------------------------------------------------------- #
# Remote probes
# --------------------------------------------------------------------------- #
_PROBE_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = [ordered]@{}
$installer = __INSTALLER__
if ($installer) {
    $out.installer_exists = Test-Path $installer
    if ($out.installer_exists) {
        $file = Get-Item $installer
        $out.installer_size = $file.Length
        $out.installer_modified = $file.LastWriteTime.ToString('s')
        $out.installer_product_version = $file.VersionInfo.ProductVersion
        $signature = Get-AuthenticodeSignature $installer
        $out.installer_signature_status = $signature.Status.ToString()
        $out.installer_signer = $signature.SignerCertificate.Subject
        if (__HASH__) { $out.installer_sha256 = (Get-FileHash $installer -Algorithm SHA256).Hash }
    }
}
$setup = Get-ItemProperty ('HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\' + __REGKEY__ + '\Setup')
$out.registry_version = $setup.Version
$out.registry_patch_level = $setup.PatchLevel
$out.registry_edition = $setup.Edition
$out.setup_running = @(Get-Process | Where-Object { $_.Name -match '^(setup|SQLServer\d{4}-KB\d+.*)$' } |
    ForEach-Object { $_.Name }) -join ','
$out | ConvertTo-Json -Depth 5 -Compress
"""

_SUMMARY_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$logRoot = __LOGROOT__
$summary = Join-Path $logRoot 'Summary.txt'
$latest = Get-ChildItem $logRoot -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1
[ordered]@{
    summary_modified = (Get-Item $summary).LastWriteTime.ToString('s')
    summary_text = [string](Get-Content $summary -Raw)
    latest_log_dir = $latest.Name
    latest_log_modified = $latest.LastWriteTime.ToString('s')
} | ConvertTo-Json -Depth 4 -Compress
"""

_PATCH_PS = r"""
$ErrorActionPreference = 'Stop'
$installer = __INSTALLER__
$arguments = @(__ARGS__)
$logDirectory = __RUNDIR__
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$started = Get-Date
$process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru `
    -RedirectStandardOutput (Join-Path $logDirectory 'patch_stdout.log') `
    -RedirectStandardError (Join-Path $logDirectory 'patch_stderr.log')
$exitCode = $process.ExitCode
Set-Content -Path (Join-Path $logDirectory 'exit.code') -Value $exitCode -Encoding ascii
[ordered]@{
    exit_code = $exitCode
    started_at = $started.ToString('s')
    finished_at = (Get-Date).ToString('s')
    duration_minutes = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
    run_directory = $logDirectory
} | ConvertTo-Json -Compress
"""


def patch_arguments(instance_name: str) -> list[str]:
    """The unattended patch contract. Every flag here is load-bearing.

    ``/quiet`` (no UI at all — there is no console on the far end of a remoting session),
    ``/IAcceptSQLServerLicenseTerms`` (setup refuses to start without it),
    ``/Action=Patch`` (update an existing instance rather than install a new one),
    ``/InstanceName`` (a host can hold several; patching the wrong one is silent),
    ``/SuppressPrivacyStatementNotice`` (another interactive prompt).
    """
    return [
        "/quiet",
        "/IAcceptSQLServerLicenseTerms",
        "/Action=Patch",
        f"/InstanceName={str(instance_name or 'MSSQLSERVER').strip()}",
        "/SuppressPrivacyStatementNotice",
    ]


def _probe(session, *, installer: str, registry_key: str, skip_hash: bool, timeout_seconds: int) -> dict[str, Any]:
    script = (
        _PROBE_PS.replace("__INSTALLER__", quote_powershell(installer) if installer else "$null")
        .replace("__REGKEY__", quote_powershell(registry_key))
        .replace("__HASH__", "$false" if skip_hash else "$true")
    )
    result = session.run_script(script, shell="powershell", timeout_seconds=timeout_seconds)
    return host_ops.parse_json_output(result.stdout, result.stderr, what="installer/registry probe")


def _setup_summary(session, *, build: str, timeout_seconds: int = 180) -> dict[str, Any]:
    """The tail of setup's own ``Summary.txt`` — the only place a failed CU explains itself."""
    script = _SUMMARY_PS.replace("__LOGROOT__", quote_powershell(setup_log_root(build)))
    try:
        result = session.run_script(script, shell="powershell", timeout_seconds=timeout_seconds)
        summary = host_ops.parse_json_output(result.stdout, result.stderr, what="setup summary")
    except host_ops.HostOpsError as exc:
        return {"error": str(exc)}
    return summary


# --------------------------------------------------------------------------- #
# SQL-side facts
# --------------------------------------------------------------------------- #
_SERVER_INFO_SQL = (
    # SERVERPROPERTY returns sql_variant, which the ODBC driver cannot bind; every value is
    # cast to a concrete type here or the whole query fails on the first column.
    "SELECT CAST(SERVERPROPERTY('ServerName') AS nvarchar(200)) AS server_name, "
    "CAST(SERVERPROPERTY('InstanceName') AS nvarchar(200)) AS instance_name, "
    "CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)) AS product_version, "
    "CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(50)) AS product_level, "
    "CAST(SERVERPROPERTY('ProductUpdateLevel') AS nvarchar(50)) AS update_level, "
    "CAST(SERVERPROPERTY('ProductUpdateReference') AS nvarchar(50)) AS update_reference, "
    "CAST(SERVERPROPERTY('Edition') AS nvarchar(100)) AS edition, "
    "CAST(SERVERPROPERTY('IsClustered') AS int) AS is_clustered, "
    "CAST(SERVERPROPERTY('IsHadrEnabled') AS int) AS is_hadr"
)


def _rows(cursor, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    columns = [column[0] for column in cursor.description]
    return [
        dict(zip(columns, [None if value is None else str(value) for value in row]))
        for row in cursor.fetchall()
    ]


def _connect(request: dict[str, Any], *, data_dir: str | Path | None, timeout_seconds: int):
    """Connect to the instance as the login the request (or the inventory) names."""
    try:
        resolved = sql_run.resolve_sqlserver_target(
            str(request.get("target") or ""),
            data_dir=data_dir,
            database="master",
            credential_name=str(request.get("credential_name") or ""),
        )
    except sql_run.SqlRunError as exc:
        raise SqlServerPatchError(str(exc)) from exc
    if str(resolved.get("db_type")) != "sqlserver":
        raise SqlServerPatchError(
            f"{request.get('target')} is db_type={resolved.get('db_type')}; a cumulative update "
            "applies to SQL Server instances only."
        )
    return sql_run.connect_target(resolved, timeout_seconds=timeout_seconds), resolved


def _wait_for_sql(request, *, data_dir, timeout_seconds: int, poll_seconds: int = 15,
                  sleep: Callable[[float], None] = time.sleep):
    """Retry the connection until the instance accepts one, or the budget expires."""
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    last_error: Exception | None = None
    while True:
        try:
            return _connect(request, data_dir=data_dir, timeout_seconds=15)
        except (SqlServerPatchError, sql_run.SqlRunError) as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise SqlServerPatchError(
                    f"The instance did not accept connections within {timeout_seconds}s: {last_error}"
                ) from exc
            sleep(poll_seconds)


def _gate_sql(
    report: GateReport,
    cursor,
    *,
    policy: dict[str, Any],
    setup_account: str = "",
) -> dict[str, Any]:
    """The "is this instance safe to patch" checklist, in the order an operator would ask it."""
    info = _rows(cursor, _SERVER_INFO_SQL)[0]
    report.note("sql", info)
    report.add(
        "sql.connect",
        OK,
        f"{info['server_name']} {info['product_version']} {info['product_level']} "
        f"{info['update_level'] or 'no CU'} ({info['edition']})",
        data=info,
    )
    report.add(
        "sql.standalone",
        OK if info["is_clustered"] == "0" and info["is_hadr"] == "0" else FAIL,
        f"clustered={info['is_clustered']}, hadr={info['is_hadr']} "
        "(a clustered or availability-group instance is patched node by node, which this "
        "operation does not do)",
        override="allow-ha",
    )
    sysadmin = _rows(cursor, "SELECT IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin")[0]["is_sysadmin"]
    report.add(
        "sql.sysadmin",
        OK if sysadmin == "1" else FAIL,
        f"the connected login is sysadmin: {sysadmin == '1'}",
    )
    if setup_account:
        # Setup runs its post-patch upgrade scripts as the Windows account that launched it. If
        # that account is not a sysadmin the patch installs the binaries and then fails to
        # upgrade the databases, which is the expensive half to recover from.
        clean = setup_account.replace("'", "''")
        member_count = _rows(
            cursor,
            "SELECT COUNT(*) AS member_count FROM sys.server_role_members m "
            "JOIN sys.server_principals r ON r.principal_id = m.role_principal_id "
            "JOIN sys.server_principals p ON p.principal_id = m.member_principal_id "
            f"WHERE r.name = 'sysadmin' AND p.name = '{clean}'",
        )[0]["member_count"]
        report.add(
            "sql.setup_account_sysadmin",
            OK if member_count != "0" else FAIL,
            f"{setup_account} is a sysadmin: {member_count != '0'} "
            "(setup runs the post-patch upgrade scripts as this account)",
        )

    databases = _rows(cursor, "SELECT name, state_desc FROM sys.databases ORDER BY database_id")
    offline = [db["name"] for db in databases if db["state_desc"] != "ONLINE"]
    report.add(
        "sql.databases_online",
        OK if not offline else FAIL,
        f"{len(databases)} databases, all ONLINE" if not offline else "not ONLINE: " + ", ".join(offline),
        data=databases,
    )

    max_age = float(policy["max_backup_age_hours"])
    backups = _rows(
        cursor,
        "SELECT d.name, CONVERT(varchar(19), MAX(b.backup_finish_date), 120) AS last_full, "
        "DATEDIFF(minute, MAX(b.backup_finish_date), GETDATE()) / 60.0 AS age_hours "
        "FROM sys.databases d "
        "LEFT JOIN msdb.dbo.backupset b ON b.database_name = d.name AND b.type = 'D' "
        "WHERE d.database_id > 4 GROUP BY d.name ORDER BY d.name",
    )
    stale = [
        row["name"]
        for row in backups
        if row["last_full"] is None or float(row["age_hours"] or 0) > max_age
    ]
    report.add(
        "sql.recent_full_backup",
        OK if not stale else FAIL,
        f"every user database has a full backup within {max_age}h"
        if not stale
        else "stale or missing full backup: " + ", ".join(stale),
        data=backups,
        override="allow-stale-backup",
    )

    running_jobs = _rows(
        cursor,
        "SELECT j.name FROM msdb.dbo.sysjobactivity a "
        "JOIN msdb.dbo.sysjobs j ON j.job_id = a.job_id "
        "JOIN msdb.dbo.syssessions s ON s.session_id = a.session_id "
        "WHERE a.start_execution_date IS NOT NULL AND a.stop_execution_date IS NULL",
    )
    report.add(
        "sql.no_running_jobs",
        OK if not running_jobs else WARN,
        "no SQL Agent job is running"
        if not running_jobs
        else "running jobs: " + ", ".join(job["name"] for job in running_jobs),
        blocking=False,
        data=running_jobs,
    )
    sessions = _rows(
        cursor, "SELECT COUNT(*) AS user_sessions FROM sys.dm_exec_sessions WHERE is_user_process = 1"
    )[0]["user_sessions"]
    report.add(
        "sql.user_sessions",
        WARN if int(sessions or 0) > 0 else OK,
        f"{sessions} user sessions connected; the patch stops the instance and disconnects them",
        blocking=False,
    )
    return info


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def precheck(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
    report: GateReport | None = None,
) -> dict[str, Any]:
    """Read-only: is this instance safe to patch right now. Changes nothing.

    Run it whenever you like — days before the window to find the blockers while there is still
    time to clear them, and again inside the window. ``apply_cu`` runs the same gates itself
    before touching anything, which is the safety property the CU26 report asked to keep.
    """
    target, policy, overrides = host_ops._prepare(request, data_dir=data_dir)  # noqa: SLF001
    own_report = report is None
    report = report or GateReport("sqlserver-precheck", target=target.describe(), echo=echo)
    if own_report:
        report.note("target", target.to_dict())

    if not target.is_windows:
        report.add(
            "host.platform", FAIL,
            f"{target.describe()} is not a Windows host; a SQL Server cumulative update is "
            "applied by setup.exe and has no Linux path here.",
        )
        return host_ops._finish(report, request, overrides) if own_report else {}  # noqa: SLF001

    installer = str(request.get("installer") or "").strip()
    expected_build = str(request.get("expected_build") or "").strip()
    skip_hash = bool(request.get("skip_hash"))
    host_ops.check_maintenance_window(
        report, request.get("window"), ignore=bool(request.get("ignore_window"))
    )

    connection, resolved = _connect(request, data_dir=data_dir, timeout_seconds=15)
    try:
        info = _gate_sql(
            report, connection.cursor(), policy=policy,
            setup_account=str(request.get("setup_account") or ""),
        )
    finally:
        connection.close()

    instance_name = str(request.get("instance_name") or info.get("instance_name") or
                        resolved.get("instance_name") or "")
    current_build = str(info.get("product_version") or "")
    registry_key = str(request.get("registry_key") or "") or sqlserver_registry_key(
        expected_build or current_build, instance_name
    )
    services = host_ops._services_of(request) or sqlserver_service_names(instance_name)  # noqa: SLF001
    report.note("instance", {"instance_name": instance_name, "registry_key": registry_key,
                             "services": services, "current_build": current_build})

    with host_ops.open_host_session(
        target, data_dir=data_dir, connect_timeout_seconds=int(policy["connect_timeout_seconds"])
    ) as session:
        facts = host_ops.read_facts(
            session, platform=target.platform, services=services,
            timeout_seconds=int(policy["facts_timeout_seconds"]),
        )
        report.note("facts", facts)
        probe = _probe(
            session, installer=installer, registry_key=registry_key, skip_hash=skip_hash,
            timeout_seconds=int(policy["facts_timeout_seconds"]),
        )
    report.note("probe", probe)

    report.add(
        "host.identity",
        OK if facts.get("is_admin") else FAIL,
        f"{facts.get('whoami')} on {facts.get('hostname')} ({facts.get('os')}), "
        f"local administrator = {facts.get('is_admin')} (setup requires it)",
    )
    _gate_disks(report, facts, policy, installer)

    pending = facts.get("reboot_pending", {})
    report.add(
        "host.reboot_pending",
        FAIL if pending.get("required") else OK,
        "SQL Server setup rule RebootRequiredCheck will fail: " + "; ".join(pending.get("reasons", []))
        if pending.get("required")
        else "no pending reboot recorded",
        data=pending,
        override="allow-pending-reboot",
    )
    report.add(
        "host.uptime",
        WARN if host_ops._float(facts.get("uptime_days")) > float(policy["max_uptime_days_warn"]) else OK,  # noqa: SLF001
        f"up {facts.get('uptime_days')} days (last boot {facts.get('last_boot')})",
        blocking=False,
    )
    running_setup = str(probe.get("setup_running") or "")
    report.add(
        "host.no_setup_in_progress",
        OK if not running_setup else FAIL,
        "no SQL Server setup process is running"
        if not running_setup
        else f"setup is already running: {running_setup}. Wait for it; never start a second one.",
    )
    _gate_installer(report, probe, installer=installer, expected_build=expected_build,
                    expected_sha256=str(request.get("installer_sha256") or ""), skip_hash=skip_hash)

    if expected_build:
        if version_tuple(current_build) >= version_tuple(expected_build):
            report.add(
                "instance.needs_patch",
                WARN,
                f"the instance is already at {current_build}; this CU is not required",
                blocking=False,
            )
        else:
            report.add(
                "instance.needs_patch",
                OK,
                f"instance at {current_build} ({info.get('edition')}), target {expected_build}",
            )
    return host_ops._finish(report, request, overrides) if own_report else {}  # noqa: SLF001


def _gate_disks(report: GateReport, facts: dict[str, Any], policy: dict[str, Any], installer: str) -> None:
    disks = {str(disk.get("mount")): disk for disk in facts.get("disks", [])}
    system = disks.get("C:", {})
    system_free = host_ops._float(system.get("free_gb"))  # noqa: SLF001
    report.add(
        "disk.system_free",
        OK if system_free >= float(policy["min_free_gb_system"]) else FAIL,
        f"C: {system_free} GB free (setup extracts and stages here; minimum "
        f"{policy['min_free_gb_system']} GB)",
    )
    if installer[:2].endswith(":") and installer[:2] != "C:":
        drive = installer[:2]
        free = host_ops._float(disks.get(drive, {}).get("free_gb"))  # noqa: SLF001
        report.add(
            "disk.installer_free",
            OK if free >= float(policy["min_free_gb_installer"]) else WARN,
            f"{drive} {free} GB free (the installer unpacks next to itself)",
            blocking=False,
        )


def _gate_installer(
    report: GateReport,
    probe: dict[str, Any],
    *,
    installer: str,
    expected_build: str,
    expected_sha256: str,
    skip_hash: bool,
) -> None:
    """Prove the staged file is the update it claims to be, before running it as SYSTEM."""
    if not installer:
        report.add(
            "installer.present", WARN,
            "no installer path given; the gates that prove which update would run were skipped",
            blocking=False,
        )
        return
    if not probe.get("installer_exists"):
        report.add("installer.present", FAIL, f"{installer} was not found on the target")
        return
    size_mb = host_ops._float(probe.get("installer_size")) / (1024 * 1024)  # noqa: SLF001
    report.add(
        "installer.present", OK,
        f"{installer} ({size_mb:.1f} MB, modified {probe.get('installer_modified')})",
    )
    signature = str(probe.get("installer_signature_status") or "")
    signer = str(probe.get("installer_signer") or "")
    report.add(
        "installer.signature",
        OK if signature == "Valid" and "Microsoft Corporation" in signer else FAIL,
        f"Authenticode {signature or 'unknown'}, signer {signer or 'unknown'}",
    )
    if skip_hash or not expected_sha256:
        report.add(
            "installer.sha256",
            WARN,
            "hash not verified (no installer_sha256 given)" if not expected_sha256
            else "hash verification skipped (skip_hash)",
            blocking=False,
        )
    else:
        actual = str(probe.get("installer_sha256") or "").upper()
        report.add(
            "installer.sha256",
            OK if actual == expected_sha256.strip().upper() else FAIL,
            f"{actual or 'unavailable'} (expected {expected_sha256.strip().upper()})",
        )
    if expected_build:
        product_version = str(probe.get("installer_product_version") or "")
        report.add(
            "installer.target_build",
            OK if product_version == expected_build else FAIL,
            f"installer ProductVersion {product_version or 'unknown'} (expected {expected_build})",
        )


def apply_cu(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the unattended patch, then verify what it produced.

    Every precheck gate runs again first, inside this call. That is deliberate and must not be
    relaxed: a precheck from an hour ago proves nothing about a host that has since started a
    Windows Update, and the gates are cheap compared to a half-applied CU.

    Needs ``confirm: true``. Exit code ``3010`` is reported as success-with-restart-required;
    the caller then runs ``restart`` and ``verify-build``.
    """
    target, policy, overrides = host_ops._prepare(request, data_dir=data_dir)  # noqa: SLF001
    installer = str(request.get("installer") or "").strip()
    if not installer:
        raise SqlServerPatchError("installer is required: the full path of the staged CU .exe on the target.")
    report = GateReport("sqlserver-apply-cu", target=target.describe(), echo=echo)
    report.note("target", target.to_dict())
    report.note("kb", str(request.get("kb") or ""))

    precheck(request, data_dir=data_dir, echo=echo, report=report)
    blockers = report.blockers(overrides)
    if blockers:
        report.add(
            "patch.aborted", FAIL,
            "blocking gates failed; setup was not started: "
            + ", ".join(gate.name for gate in blockers),
        )
        return host_ops._finish(report, request, overrides)  # noqa: SLF001

    instance_name = str((report.facts.get("instance") or {}).get("instance_name") or "")
    arguments = patch_arguments(instance_name)
    command = f'"{installer}" ' + " ".join(arguments)
    if request.get("dry_run"):
        report.add("patch.dry_run", OK, f"would execute on {target.host}: {command}", blocking=False)
        return host_ops._finish(report, request, overrides)  # noqa: SLF001

    expected_build = str(request.get("expected_build") or
                         (report.facts.get("instance") or {}).get("current_build") or "")
    current_build = str((report.facts.get("instance") or {}).get("current_build") or "unknown")
    if not host_ops._authorized(  # noqa: SLF001
        report,
        request,
        operation="sqlserver-apply-cu",
        target=target,
        effects=[
            f"instance {instance_name or 'MSSQLSERVER'} will be PATCHED: "
            f"{current_build} -> {expected_build or 'the installer build'}"
            + (f" ({request['kb']})" if request.get("kb") else ""),
            "the instance stops during the patch; every connected session is disconnected",
            "a cumulative update CANNOT be uninstalled — rollback means restoring a host-level "
            "snapshot or backup",
            f"setup will run: {command}",
        ],
    ):
        return host_ops._finish(report, request, overrides)  # noqa: SLF001
    run_directory = rf"C:\Windows\Temp\db_ops_cu_{report.run_id}"
    script = (
        _PATCH_PS.replace("__INSTALLER__", quote_powershell(installer))
        .replace("__ARGS__", ", ".join(quote_powershell(argument) for argument in arguments))
        .replace("__RUNDIR__", quote_powershell(run_directory))
    )
    report.say(f"Executing on {target.host}: {command}")
    report.say("Waiting for the unattended patch to finish (normally 5-30 minutes)...")

    started = time.monotonic()
    with host_ops.open_host_session(
        target, data_dir=data_dir, connect_timeout_seconds=int(policy["connect_timeout_seconds"])
    ) as session:
        result = session.run_script(
            script, shell="powershell", timeout_seconds=int(policy["patch_timeout_seconds"])
        )
        outcome = host_ops.parse_json_output(result.stdout, result.stderr, what="patch result")
        elapsed = (time.monotonic() - started) / 60.0
        exit_code = int(host_ops._float(outcome.get("exit_code")))  # noqa: SLF001
        outcome["local_elapsed_minutes"] = round(elapsed, 1)
        report.note("patch", outcome)
        report.note("setup_summary", _setup_summary(session, build=expected_build))

    status, verdict = patch_exit_verdict(exit_code, log_root=setup_log_root(expected_build))
    report.add("patch.exit_code", status, f"setup exit code {exit_code} after {elapsed:.1f} "
                                          f"minutes: {verdict}", data=outcome)
    if exit_code == EXIT_SUCCESS_RESTART_REQUIRED:
        report.add(
            "patch.restart_required", WARN,
            "restart the host (host-restart), then run sqlserver-verify-build",
            blocking=False,
        )
    return host_ops._finish(report, request, overrides)  # noqa: SLF001


def patch_exit_verdict(exit_code: int, *, log_root: str = "") -> tuple[str, str]:
    """What setup's exit code means. ``(gate status, sentence)``.

    ``3010`` is the one worth encoding: it means *the patch was applied and the host must be
    restarted to finalise it*. Reported as a failure it invites someone to run setup a second
    time on an instance that is already patched; reported as a bare warning it invites someone
    to skip the restart. It is a success **with one outstanding action**, and this says so.
    """
    if exit_code == EXIT_SUCCESS:
        return OK, "the patch was applied."
    if exit_code == EXIT_SUCCESS_RESTART_REQUIRED:
        return OK, (
            "the patch SUCCEEDED and the host must be restarted to finalise it. "
            "Do NOT re-run the patch."
        )
    return FAIL, "the patch failed" + (f"; read Summary.txt under {log_root}" if log_root else ".")


def verify_build(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Assert the instance is on the expected build. Read-only, and safe to run any time.

    ``SERVERPROPERTY('ProductVersion')`` is the verdict — it is what the running engine reports.
    The registry is read as corroboration, and the value read is **``PatchLevel``**: an RTM
    install patched to CU26 keeps ``Version = 16.0.1000.6`` forever, which is correct and is why
    the previous implementation of this gate failed every successful CU.
    """
    target, policy, overrides = host_ops._prepare(request, data_dir=data_dir)  # noqa: SLF001
    expected_build = str(request.get("expected_build") or "").strip()
    report = GateReport("sqlserver-verify-build", target=target.describe(), echo=echo)
    report.note("target", target.to_dict())

    report.say("Waiting for the instance to accept connections...")
    connection, resolved = _wait_for_sql(
        request, data_dir=data_dir, timeout_seconds=int(policy["sql_reconnect_timeout_seconds"]),
        poll_seconds=int(policy["poll_seconds"]),
    )
    try:
        cursor = connection.cursor()
        info = _rows(cursor, _SERVER_INFO_SQL)[0]
        report.note("sql", info)
        report.add("post.sql_online", OK, "the instance is accepting connections")
        report.add(
            "post.build",
            OK if not expected_build or info["product_version"] == expected_build else FAIL,
            f"ProductVersion {info['product_version']} ({info['update_level'] or 'no CU'} "
            f"{info['update_reference'] or ''})"
            + (f", expected {expected_build}" if expected_build else ""),
            data=info,
        )
        databases = _rows(cursor, "SELECT name, state_desc FROM sys.databases ORDER BY database_id")
        offline = [db["name"] for db in databases if db["state_desc"] != "ONLINE"]
        report.add(
            "post.databases_online",
            OK if not offline else FAIL,
            f"{len(databases)} databases, all ONLINE" if not offline else "not ONLINE: " + ", ".join(offline),
            data=databases,
        )
    finally:
        connection.close()

    instance_name = str(request.get("instance_name") or info.get("instance_name") or
                        resolved.get("instance_name") or "")
    build = expected_build or str(info.get("product_version") or "")
    registry_key = str(request.get("registry_key") or "") or sqlserver_registry_key(build, instance_name)
    services = host_ops._services_of(request) or sqlserver_service_names(instance_name)  # noqa: SLF001

    if target.is_windows:
        with host_ops.open_host_session(
            target, data_dir=data_dir, connect_timeout_seconds=int(policy["connect_timeout_seconds"])
        ) as session:
            facts = host_ops.read_facts(
                session, platform=target.platform, services=services,
                timeout_seconds=int(policy["facts_timeout_seconds"]),
            )
            probe = _probe(session, installer="", registry_key=registry_key, skip_hash=True,
                           timeout_seconds=int(policy["facts_timeout_seconds"]))
        report.note("facts", facts)
        report.note("probe", probe)
        patch_level = str(probe.get("registry_patch_level") or "")
        report.add(
            "post.registry_build",
            OK if not expected_build or patch_level == expected_build else FAIL,
            f"registry PatchLevel {patch_level or 'unknown'}"
            + (f" (expected {expected_build})" if expected_build else "")
            + f"; Version stays at the originally installed {probe.get('registry_version')} by design",
            data={"version": probe.get("registry_version"), "patch_level": patch_level},
        )
        states = {str(item["name"]): str(item["status"]) for item in facts.get("services", [])}
        not_up = [name for name in services if not host_ops.is_service_up(states.get(name, ""))]
        report.add(
            "post.services_running",
            OK if not not_up else FAIL,
            "all instance services are running" if not not_up else "not running: " + ", ".join(not_up),
            data=states,
        )
        pending = facts.get("reboot_pending", {})
        if pending.get("required"):
            report.add(
                "post.reboot_pending", WARN,
                "the host reports a pending reboot after the patch; schedule a restart: "
                + "; ".join(pending.get("reasons", [])),
                blocking=False, data=pending,
            )
    return host_ops._finish(report, request, overrides)  # noqa: SLF001
