"""Operating on a **host** — facts, services, restart-and-wait — on Windows or Linux alike.

``remote_exec`` answers *how do I reach a machine and run a command*. This module answers the
next question, the one every maintenance task actually asks: *what state is this host in, are
its services up, and can I restart it and know it came back*. It is deliberately not about SQL
Server, or backups, or any one operation — patching, a failover drill and routine host
maintenance all need the same three things, and each used to grow its own copy.

The copy that motivated this was ``scripts/archive/python/sqlserver_apply_cu_patch.py``, the
ORD 129 CU26 script: one file, hard-wired to one host, one instance and one KB, in which the
restart-and-wait logic could not be reused by a task that only needed a restart. Its execution
report (``docs/reports/20260803_ord129-sqlserver-2022-cu26-execution-report.md``, §5.8) asked
for exactly this split. Two behaviours from that night are baked in here rather than left to
each caller:

* **Waiting for SSH to answer is not waiting for the host to be ready.** ``sshd`` is back long
  before the services are, so a single sample taken when the port opens reported healthy SQL
  Server services as down on both restarts of a successful run. :func:`wait_for_services` polls
  until every named service is up or the budget expires, and says which budget it spent.
* **A pending reboot is a fact about the host, not about the operation.** Windows refuses to
  patch while ``PendingFileRenameOperations`` is populated; that is worth reporting on any host
  at any time, so it lives in :func:`host_facts` rather than inside a patch script.

**Input is a JSON object** — the same contract as ``run-sql`` / ``remote_exec``::

    {
      "target": "ACME-192-0-2-250",   // server_id / ip / "<db_type> <ip>" in db_instances.json
      "access": { ... },                // OR an inline cmd_access object, for a host not in config
      "services": ["MSSQL$APPDB"],       // Windows service names, or Linux systemd units
      "confirm": true,                  // required by anything that changes the host
      "dry_run": false,
      "reason": "ORD 129 CU26",         // recorded on the host's restart notice and in evidence
      "window": {"start": "2026-08-03 19:00", "end": "2026-08-03 21:00"},
      "ignore_window": false,
      "wait": {"up_timeout_seconds": 1800},   // overrides data/maintenance_policy.json
      "evidence": true
    }

Every entry point returns the :class:`db_ops.common.evidence.GateReport` dict — gates, facts,
one verdict, and the path of the evidence file — so a shell caller, a Telegram action and a
Python caller all read the same result.
"""

from __future__ import annotations
from db_ops.lib.cmd_access import (  # noqa: F401 - one definition, see that module
    PLATFORM_LINUX,
    PLATFORM_WINDOWS,
    SUPPORTED_CMD_ACCESS_METHODS,
    SUPPORTED_PLATFORMS,
    infer_platform_from_os,
    resolve_cmd_access,
    resolve_cmd_credential,
    resolve_platform,
)

import json
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from db_ops.common import confirm, data_sources, hostcmd, remote_exec
from db_ops.common import data_sources as target_resolve
from db_ops.lib import time_window
from db_ops.lib.target_profile import (
    RUNTIME_DOCKER, RUNTIME_K8S, SOURCE_CONFIG, SOURCE_REQUEST, TargetProfile,
    parse_os_version, select_powershell_dialect,
)
from db_ops.common.evidence import FAIL, OK, WARN, GateReport
from db_ops.common.remote_exec import RemoteExecError, RemoteSession, quote_powershell

__all__ = [
    "HostOpsError",
    "HostTarget",
    "PLATFORM_LINUX",
    "PLATFORM_WINDOWS",
    "SUPPORTED_CMD_ACCESS_METHODS",
    "SUPPORTED_PLATFORMS",
    "check_maintenance_window",
    "host_facts",
    "is_service_up",
    "load_maintenance_policy",
    "open_host_session",
    "parse_json_output",
    "read_facts",
    "resolve_cmd_access",
    "resolve_cmd_credential",
    "resolve_host",
    "resolve_platform",
    "restart_host",
    "service_control",
    "service_states",
    "wait_for_port",
    "wait_for_services",
    "os_drift",
    "wrap_for_runtime",
]


# Service states that mean "up", per platform vocabulary. One helper so no caller compares
# against "Running" and silently reports every Linux unit as down.
_UP_STATES = {"running", "active"}

POLICY_FILE = "maintenance_policy.json"

# Defaults for every timing decision here. They are policy, so they live in
# data/maintenance_policy.json; these are the fallbacks when it says nothing.
DEFAULT_POLICY: dict[str, Any] = {
    "down_timeout_seconds": 600,      # host stops answering after the restart is issued
    "up_timeout_seconds": 1800,       # ... and answers again
    "services_timeout_seconds": 600,  # ... and its services finish starting
    "poll_seconds": 15,
    "connect_timeout_seconds": 30,
    "facts_timeout_seconds": 300,
    "max_uptime_days_warn": 90,
    "min_free_gb_system": 15.0,
    "min_free_gb_installer": 5.0,
    "max_backup_age_hours": 30.0,
    "patch_timeout_seconds": 5400,
    "sql_reconnect_timeout_seconds": 900,
}


class HostOpsError(RuntimeError):
    """A user-facing failure: unknown target, unusable cmd_access, refused confirmation."""


# `cmd_access` resolution is `db_ops.lib.cmd_access` since 2026-08-15 — the mirror of
# `lib.sql_access`, and moved for the same reason. `metrics` reads the same block while loading
# its target list, in-process and once per target, so it cannot come from a subprocess; and none
# of it connects to anything, so it was never this module's work in the first place. The names
# stay in this module's published surface because operating on a host is where callers look for
# them. See the import at the top of the file.


# --------------------------------------------------------------------------- #
# The target
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HostTarget:
    """One machine to operate on: where it is, how to get in, and what it runs."""

    server_id: str
    host: str
    platform: str
    access: dict[str, Any] = field(default_factory=dict)
    credential: dict[str, Any] | None = None
    instance: dict[str, Any] = field(default_factory=dict)
    #: What this machine *is* — the OS version and runtime on top of the platform. Carried since
    #: 2026-08-19 because `platform` alone cannot answer "which cmdlets exist here": the fact
    #: script is written in `Get-CimInstance`/`ConvertTo-Json`, which is PowerShell 3.0 and
    #: Windows Server 2012 or newer. See :mod:`db_ops.lib.target_profile`.
    profile: TargetProfile = field(default_factory=TargetProfile)
    #: What the runtime names, when the profile says the command runs inside one. Empty for a
    #: plain host. Separate from `access` on purpose: `access` is how to reach the *machine*, this
    #: is what to step into once there — the two questions `hostcmd.Host` collapses into one field.
    runtime_target: dict[str, Any] = field(default_factory=dict)

    @property
    def method(self) -> str:
        return str(self.access.get("method") or "")

    @property
    def port(self) -> int:
        return int(self.access.get("port") or (22 if self.method == "ssh" else 5985))

    @property
    def is_windows(self) -> bool:
        return self.platform == PLATFORM_WINDOWS

    def describe(self) -> str:
        return f"{self.server_id or self.host} ({self.platform or 'unknown platform'}, {self.method or 'no method'})"

    def to_dict(self) -> dict[str, Any]:
        access = dict(self.access)
        # The access block can carry a literal password; never let it reach an evidence file.
        for secret_key in ("password", "passphrase"):
            if access.get(secret_key):
                access[secret_key] = "***"
        return {
            "server_id": self.server_id,
            "host": self.host,
            "platform": self.platform,
            "access": access,
            "credential_name": str((self.credential or {}).get("credential_name") or ""),
            "profile": self.profile.to_dict(),
            "shell_dialect": select_powershell_dialect(self.profile).to_dict() if self.is_windows else None,
        }


def resolve_host(
    spec: str | dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    access: dict[str, Any] | None = None,
    platform: str = "",
    profile: TargetProfile | None = None,
) -> HostTarget:
    """Resolve a host target from ``db_instances.json``, or from an inline access object.

    ``spec`` accepts what every other db_ops entry point accepts — a ``server_id``, a bare ip, or
    ``<db_type> <ip> [port]`` — because an operator holding a runbook has the ip, and a
    scheduled task has the server_id. An explicit ``access`` object skips the inventory
    entirely, so a host that is not (yet) in ``db_instances.json`` can still be operated on.

    ``profile`` is what the *caller* knows about the machine — ``os``, ``os_major``, ``runtime``.
    It is merged over whatever the inventory says, never under it, and the merged answer rides on
    the returned target so a script builder downstream can ask which PowerShell exists there
    rather than assuming the newest.
    """
    stated = profile if profile is not None else TargetProfile()
    original_spec = spec if isinstance(spec, dict) else {}
    if isinstance(spec, dict):
        access = access or spec.get("access")
        platform = platform or str(spec.get("platform") or "")
        # The bare keys, for a request that states one fact rather than a whole block. Same
        # precedence as run-sql: an explicit `profile` object wins over them.
        stated = stated.merge(TargetProfile.from_json(spec.get("profile") or {})).merge(
            TargetProfile.from_json(spec)
        )
        spec = str(spec.get("target") or spec.get("server_id") or "")
    text = str(spec or "").strip()

    if access:
        resolved_platform = (
            str(platform or access.get("platform") or "").strip().lower()
            or infer_platform_from_os(str(access.get("os") or ""))
            or (PLATFORM_WINDOWS if str(access.get("method") or "") == "winrm" else PLATFORM_LINUX)
        )
        block = resolve_cmd_access({"cmd_access": access}, platform=resolved_platform,
                                   host=str(access.get("host") or text))
        credential = resolve_cmd_credential(block, _remote_credentials_for(block, data_dir))
        return HostTarget(
            server_id=text or str(block.get("host") or ""),
            host=str(block.get("host") or text),
            platform=resolved_platform,
            access=block,
            credential=credential,
            profile=stated.merge(TargetProfile.from_json(access)).with_(platform=resolved_platform),
            runtime_target=_runtime_target(original_spec, access),
        )

    if not text:
        raise HostOpsError("target is required (a server_id, an ip, or '<db_type> <ip> [port]').")

    instance = _find_instance(text, data_dir=data_dir)
    try:
        resolved_platform = str(platform or "").strip().lower() or resolve_platform(instance)
        block = resolve_cmd_access(instance, platform=resolved_platform, host=str(instance.get("ip") or ""))
        credential = resolve_cmd_credential(block, _remote_credentials_for(block, data_dir))
    except RuntimeError as exc:
        raise HostOpsError(f"{text}: {exc}") from exc

    if not block or not block.get("enabled", True):
        raise HostOpsError(
            f"{text} has no usable cmd_access block in db_instances.json, so there is no way to "
            "reach the host. Add one (method ssh|winrm plus a credential_name), or pass an "
            "inline 'access' object."
        )
    return HostTarget(
        server_id=str(instance.get("server_id") or text),
        host=str(block.get("host") or instance.get("ip") or ""),
        platform=resolved_platform,
        access=block,
        credential=credential,
        instance=instance,
        profile=stated.merge(
            TargetProfile.from_json(instance, source=SOURCE_CONFIG)
        ).with_(platform=resolved_platform),
        runtime_target=_runtime_target(original_spec, instance),
    )


def _remote_credentials_for(block: dict[str, Any], data_dir: str | Path | None) -> list[dict[str, Any]]:
    """The credential groups — loaded only when the block cannot answer for itself.

    An access block carrying ``username`` plus ``password``/``password_ref`` is the self-contained
    door: reading ``users.json`` for it would defeat the point and would fail outright on a node
    that has no inventory, which is precisely the case that door exists for.
    """
    if str(block.get("credential_name") or "").strip():
        return data_sources.load_remote_credentials(data_dir)
    if str(block.get("username") or "").strip() and (block.get("password") or block.get("password_ref")):
        return []
    return data_sources.load_remote_credentials(data_dir)


def _runtime_target(request: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """What the container/pod is called, from the request first and the record second.

    ``container_name`` on a db instance is the field that already exists and already means this;
    the request's ``container`` wins because a caller naming one is naming *that* one.
    """
    resolved: dict[str, Any] = {}
    for key, aliases in (
        ("container", ("container", "container_name")),
        ("pod", ("pod",)),
        ("namespace", ("namespace",)),
        ("pod_container", ("pod_container",)),
        ("container_shell", ("container_shell",)),
    ):
        for holder in (request, source):
            value = next((str(holder.get(name) or "").strip() for name in aliases if holder.get(name)), "")
            if value:
                resolved[key] = value
                break
    if request.get("sudo") is not None:
        resolved["sudo"] = bool(request.get("sudo"))
    return resolved


def wrap_for_runtime(target: HostTarget, command: str) -> str:
    """The command as it must be typed to land *inside* this target's runtime.

    Closes the last half of the two-vocabularies finding: ``hostcmd`` could put a command inside a
    container and ``host_ops`` could not, so ``backup-database`` reached a containerised engine and
    ``run-cmd`` could only reach the machine hosting it. The wrapping itself is not rewritten here
    — :func:`db_ops.common.hostcmd.wrap` already knows that a container needs ``sh -lc`` because
    the binaries live on a login shell's PATH — it is only reached from the other vocabulary.

    **Only a runtime the caller stated wraps anything.** A ``container_name`` on the instance is
    enough for `TargetProfile` to infer ``runtime: docker`` — right for describing the target, and
    wrong as an instruction, because it would silently move every existing ``run-cmd`` against a
    containerised instance off the host and into the container. ``hostcmd``'s own header made this
    call first: *"``runtime`` is stated rather than guessed. A ``container`` field alone would
    leave 'no container' meaning both 'run on the host' and 'the caller forgot'."*

    A plain host, or an inferred runtime, is returned unchanged — so nothing about the existing
    path moves.
    """
    runtime = target.profile.runtime
    if runtime not in {RUNTIME_DOCKER, RUNTIME_K8S}:
        return command
    if target.profile.sources.get("runtime") != SOURCE_REQUEST:
        return command
    named = dict(target.runtime_target)
    if runtime == RUNTIME_DOCKER and not named.get("container"):
        raise HostOpsError(
            'runtime "docker" needs a "container" (or a container_name on the instance) — without '
            "it the command would run on the host and report, quite truthfully, that the database "
            "is not there."
        )
    if runtime == RUNTIME_K8S and not named.get("pod"):
        raise HostOpsError('runtime "k8s" needs a "pod".')
    if target.is_windows:
        return _wrap_windows_runtime(runtime, named, command)
    host = hostcmd.Host(
        runtime=runtime,
        container=str(named.get("container") or ""),
        pod=str(named.get("pod") or ""),
        namespace=str(named.get("namespace") or "default"),
        pod_container=str(named.get("pod_container") or ""),
        sudo=bool(named.get("sudo", False)),
    )
    return hostcmd.wrap(host, command)


#: The shell to start *inside* the container. A property of the image, not of the host: a Linux
#: container on a Windows Docker host still wants `sh`, and a Windows container wants `cmd /c`.
#: Configurable rather than derived for exactly that reason — the host OS cannot answer it.
DEFAULT_CONTAINER_SHELL = "sh -lc"


def _wrap_windows_runtime(runtime: str, named: dict[str, Any], command: str) -> str:
    """``docker exec`` / ``kubectl exec`` composed for a **PowerShell** session.

    The Linux form goes through :func:`hostcmd.wrap`, which quotes with ``shlex`` — POSIX rules
    that a PowerShell host does not read. This is the same command line under PowerShell's quoting
    instead: a single-quoted literal, doubling any apostrophe inside it, which
    ``remote_exec.quote_powershell`` already owns. Nothing else differs, and ``sudo`` is dropped
    because it does not exist here.

    **Implemented, and not verified against a live Windows container host** — this estate has
    none, so the guarantee is limited to the command text (pinned by tests) rather than to a run.
    That is why it exists at all: the alternative was a refusal that would have to be discovered
    and then implemented under time pressure by whoever first needs it.
    """
    shell = str(named.get("container_shell") or DEFAULT_CONTAINER_SHELL).strip()
    inner = quote_powershell(command)
    if runtime == RUNTIME_DOCKER:
        return f"docker exec -i {quote_powershell(str(named['container']))} {shell} {inner}"
    where = f"-n {quote_powershell(str(named.get('namespace') or 'default'))} {quote_powershell(str(named['pod']))}"
    if named.get("pod_container"):
        where += f" -c {quote_powershell(str(named['pod_container']))}"
    return f"kubectl exec -i {where} -- {shell} {inner}"


def os_drift(profile: TargetProfile | None, observed: str) -> str:
    """``""`` when the inventory's ``os`` agrees with the host, else a sentence naming both.

    Compares the *NT version* rather than the caption text, because the two are written
    differently by different sources — ``Windows NT 6.2 (Build 9200)`` and ``Microsoft Windows
    Server 2012 Standard 6.2.9200`` are the same machine — and a diff on wording would cry wolf on
    every host while missing the one that matters. ``ACME-192-0-2-245`` is the one that mattered:
    recorded as NT 6.2, and the host answers ``Windows Server 2016 ... 10.0.14393``.
    """
    if profile is None or not observed:
        return ""
    recorded = profile.os_version
    seen = parse_os_version(observed, platform=PLATFORM_WINDOWS)
    if recorded is None or seen == (None, None):
        return ""
    if recorded == seen:
        return ""
    return (
        f"db_instances.json records os {profile.os_text!r} (NT {recorded[0]}.{recorded[1]}) but the "
        f"host reports {observed!r} (NT {seen[0]}.{seen[1]}) - the recorded value is what decides "
        "the PowerShell dialect and whether this host can be managed at all"
    )


def _find_instance(spec: str, *, data_dir: str | Path | None) -> dict[str, Any]:
    """The db instance a spec names — server_id / triple first, then a bare ip.

    ``target_resolve`` reads a single token as a ``server_id``, which is right for a database
    target but wrong for a host: OS-only entries (an application server, a host whose database
    is not monitored) are reached by ip far more often than by id.
    """
    try:
        return target_resolve.resolve_target_instance(spec, data_dir=data_dir)
    except target_resolve.TargetResolveError as exc:
        matches = [
            item
            for item in data_sources.load_db_instances(data_dir)
            if str(item.get("ip") or "").strip() == spec
        ]
        if not matches:
            raise HostOpsError(str(exc)) from exc
        # Several instances can share a host (two SQL Server instances on one VM). They also
        # share the machine, so any of them answers "how do I reach this host" identically.
        return dict(matches[0])


def open_host_session(
    target: HostTarget,
    *,
    data_dir: str | Path | None = None,
    secrets: dict[str, str] | None = None,
    connect_timeout_seconds: int | None = None,
) -> RemoteSession:
    """Open a session to the target's host, with the credential its config names."""
    access = dict(target.access)
    if connect_timeout_seconds:
        access["timeout_seconds"] = int(connect_timeout_seconds)
    try:
        return remote_exec.open_session(
            access, credential=target.credential, secrets=secrets, data_dir=data_dir
        )
    except RemoteExecError as exc:
        raise HostOpsError(f"{target.describe()}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
def load_maintenance_policy(
    data_dir: str | Path | None = None, *, server_id: str = "", overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Timing and threshold policy: built-in defaults < file defaults < per-server < request.

    The numbers live in ``data/maintenance_policy.json`` because they are config, not code: a
    host with slow storage needs a longer services budget, and that is an operator's decision to
    record, not a constant to edit in Python.
    """
    policy = dict(DEFAULT_POLICY)
    path = Path(data_dir) / POLICY_FILE if data_dir else data_sources.users_path().parent / POLICY_FILE
    if path.exists():
        try:
            raw = json.loads(path.read_bytes().decode("utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HostOpsError(f"{path} is not readable JSON: {exc}") from exc
        block = raw.get("maintenance_policy", raw) or {}
        policy.update({k: v for k, v in (block.get("defaults") or {}).items()})
        if server_id:
            policy.update({k: v for k, v in ((block.get("servers") or {}).get(server_id) or {}).items()})
    for key, value in (overrides or {}).items():
        if value not in (None, ""):
            policy[key] = value
    return policy


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #
_WINDOWS_FACTS_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = [ordered]@{}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$out.whoami = $identity.Name
$out.is_admin = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$out.hostname = $env:COMPUTERNAME
$os = Get-CimInstance Win32_OperatingSystem
$out.os = $os.Caption + ' ' + $os.Version
$out.last_boot = $os.LastBootUpTime.ToString('s')
$out.uptime_days = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalDays, 2)
$out.remote_time = (Get-Date).ToString('s')
$out.disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    [ordered]@{ mount = $_.DeviceID; free_gb = [math]::Round($_.FreeSpace / 1GB, 2); total_gb = [math]::Round($_.Size / 1GB, 2) }
})
$names = @(__SERVICES__)
$out.services = @(Get-Service | Where-Object { $names -contains $_.Name } | ForEach-Object {
    [ordered]@{ name = $_.Name; display = $_.DisplayName; status = $_.Status.ToString(); start_type = $_.StartType.ToString() }
})
$reasons = @()
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $reasons += 'Component Based Servicing' }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\WindowsUpdate\Auto Update\RebootRequired') { $reasons += 'Windows Update' }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Updates\UpdateExeVolatile') { $reasons += 'UpdateExeVolatile' }
if ((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName').ComputerName -ne (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName').ComputerName) { $reasons += 'computer rename' }
$pfro = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations).PendingFileRenameOperations
$pfroCount = @($pfro | Where-Object { $_ }).Count
if ($pfroCount -gt 0) { $reasons += "PendingFileRenameOperations ($pfroCount entries)" }
$out.reboot_pending = [ordered]@{
    required = ($reasons.Count -gt 0)
    reasons = @($reasons)
    pending_file_rename_count = $pfroCount
}
$out | ConvertTo-Json -Depth 6 -Compress
"""

# The same facts for a PowerShell **2.0** host — Windows Server 2008 / 2008 R2, and anything else
# that never had WMF 3.0 installed. `Get-CimInstance` and `ConvertTo-Json` both arrived with
# PowerShell 3.0 / NT 6.2, so the script above does not merely return less there: it fails at
# cmdlet lookup, with a message that reads like a permissions problem and is not one.
#
# **It emits line records, not JSON, and for the reason already written above the Linux script**:
# a hand-rolled JSON printer breaks on the first value containing a quote, and PowerShell 2.0 has
# no other way to produce JSON. Tab-separated records parse with the same function the Linux facts
# use, so this variant adds a script rather than a second parser.
#
# `Win32_Service` rather than `Get-Service` because PowerShell 2.0's service object has no
# `StartType` (that property arrived in 4.0); WMI has carried `StartMode` since 2003.
_WINDOWS_FACTS_PS2 = r"""
$ErrorActionPreference = 'SilentlyContinue'
$TAB = [string][char]9
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
Write-Output ('whoami' + $TAB + $identity.Name)
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Output ('is_admin' + $TAB + 'true')
} else {
    Write-Output ('is_admin' + $TAB + 'false')
}
Write-Output ('hostname' + $TAB + $env:COMPUTERNAME)
$os = Get-WmiObject Win32_OperatingSystem
Write-Output ('os' + $TAB + $os.Caption + ' ' + $os.Version)
$boot = $os.ConvertToDateTime($os.LastBootUpTime)
Write-Output ('last_boot' + $TAB + $boot.ToString('s'))
Write-Output ('uptime_seconds' + $TAB + [string][long]((Get-Date) - $boot).TotalSeconds)
Write-Output ('remote_time' + $TAB + (Get-Date).ToString('s'))
foreach ($disk in Get-WmiObject Win32_LogicalDisk -Filter 'DriveType=3') {
    Write-Output ('disk' + $TAB + $disk.DeviceID + $TAB + [string][long]($disk.FreeSpace / 1024) + $TAB + [string][long]($disk.Size / 1024))
}
$names = @(__SERVICES__)
foreach ($service in Get-WmiObject Win32_Service) {
    if ($names -contains $service.Name) {
        Write-Output ('service' + $TAB + $service.Name + $TAB + $service.State + $TAB + $service.StartMode)
    }
}
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { Write-Output ('reboot_reason' + $TAB + 'Component Based Servicing') }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\WindowsUpdate\Auto Update\RebootRequired') { Write-Output ('reboot_reason' + $TAB + 'Windows Update') }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Updates\UpdateExeVolatile') { Write-Output ('reboot_reason' + $TAB + 'UpdateExeVolatile') }
$active = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName').ComputerName
$configured = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName').ComputerName
if ($active -ne $configured) { Write-Output ('reboot_reason' + $TAB + 'computer rename') }
$pfro = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations).PendingFileRenameOperations
$pfroCount = @($pfro | Where-Object { $_ }).Count
Write-Output ('pending_file_rename_count' + $TAB + [string]$pfroCount)
if ($pfroCount -gt 0) { Write-Output ('reboot_reason' + $TAB + 'PendingFileRenameOperations (' + [string]$pfroCount + ' entries)') }
"""

# Line records, not JSON: a minimal Linux host is not guaranteed to have jq or python3, and a
# hand-rolled JSON printer in shell breaks on the first hostname with a quote in it. One
# tab-separated record per fact parses the same everywhere.
_LINUX_FACTS_SH = r"""
set -u
printf 'hostname\t%s\n' "$(hostname 2>/dev/null)"
if [ -r /etc/os-release ]; then . /etc/os-release; fi
printf 'os\t%s\n' "${PRETTY_NAME:-$(uname -sr 2>/dev/null)}"
printf 'remote_time\t%s\n' "$(date +%Y-%m-%dT%H:%M:%S 2>/dev/null)"
printf 'uptime_seconds\t%s\n' "$(cut -d' ' -f1 /proc/uptime 2>/dev/null || echo 0)"
printf 'last_boot\t%s\n' "$(uptime -s 2>/dev/null || echo '')"
if [ "$(id -u)" = "0" ]; then printf 'is_admin\ttrue\n'; else printf 'is_admin\tfalse\n'; fi
df -Pk -x tmpfs -x devtmpfs 2>/dev/null | awk 'NR>1 {printf "disk\t%s\t%s\t%s\n", $6, $4, $2}'
for unit in __SERVICES__; do
    state=$(systemctl is-active "$unit" 2>/dev/null || true)
    enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
    printf 'service\t%s\t%s\t%s\n' "$unit" "${state:-unknown}" "${enabled:-unknown}"
done
if [ -f /var/run/reboot-required ]; then printf 'reboot_reason\t%s\n' "package upgrade (/var/run/reboot-required)"; fi
"""


def read_facts(
    session: RemoteSession,
    *,
    platform: str = "",
    services: Sequence[str] = (),
    timeout_seconds: int | None = None,
    dialect: str = "",
) -> dict[str, Any]:
    """Collect the host's state over an open session, in one round trip.

    The returned shape is the **same on both platforms** — ``hostname``, ``os``, ``uptime_days``,
    ``disks``, ``services``, ``reboot_pending`` — because a caller deciding "is this host safe to
    touch" asks the same questions of Windows and Linux, and only the commands differ.
    """
    platform = (platform or getattr(session.access, "platform", "") or "").lower()
    is_windows = platform == PLATFORM_WINDOWS or getattr(session.access, "is_powershell", False)
    names = [str(name).strip() for name in services if str(name).strip()]
    if is_windows:
        # `wmi` is the PowerShell 2.0 variant, for a host that never had WMF 3.0 — see
        # `lib.target_profile.select_powershell_dialect`, which decides it from the OS version.
        # Defaulting to `cim` keeps every existing caller on the script it has always run.
        legacy = str(dialect or "").strip().lower() == "wmi"
        script = (_WINDOWS_FACTS_PS2 if legacy else _WINDOWS_FACTS_PS).replace(
            "__SERVICES__", ", ".join(quote_powershell(name) for name in names)
        )
        result = session.run_script(script, shell="powershell", timeout_seconds=timeout_seconds)
        if legacy:
            return _parse_record_facts(result.stdout, result.stderr, platform=PLATFORM_WINDOWS)
        return _parse_windows_facts(result.stdout, result.stderr)
    script = _LINUX_FACTS_SH.replace(
        "__SERVICES__", " ".join(f"'{name}'" for name in names) if names else ""
    )
    result = session.run_script(script, shell="bash", timeout_seconds=timeout_seconds)
    return _parse_linux_facts(result.stdout, result.stderr)


def parse_json_output(stdout: str, stderr: str, *, what: str = "facts") -> dict[str, Any]:
    """Read a PowerShell ``ConvertTo-Json`` reply, or say what came back instead.

    Empty stdout with something on stderr is the common failure (a PowerShell exception, a
    missing module), and it is worth reporting *that text* rather than a JSON decode error
    about an empty string — the two send the reader to completely different places.
    """
    text = (stdout or "").strip()
    if not text:
        raise HostOpsError(
            f"The host returned no {what}. stderr: " + (stderr or "").strip()[:500]
        )
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HostOpsError(f"The host returned non-JSON {what}: {text[:500]}") from exc
    if not isinstance(raw, dict):
        raise HostOpsError(f"The host returned {what} that are not a JSON object: {text[:200]}")
    return raw


def _parse_windows_facts(stdout: str, stderr: str) -> dict[str, Any]:
    raw = parse_json_output(stdout, stderr)
    facts: dict[str, Any] = {
        "platform": PLATFORM_WINDOWS,
        "hostname": _text(raw.get("hostname")),
        "os": _text(raw.get("os")),
        "whoami": _text(raw.get("whoami")),
        "is_admin": bool(raw.get("is_admin")),
        "last_boot": _text(raw.get("last_boot")),
        "uptime_days": _float(raw.get("uptime_days")),
        "remote_time": _text(raw.get("remote_time")),
        "disks": [
            {
                "mount": _text(disk.get("mount")),
                "free_gb": _float(disk.get("free_gb")),
                "total_gb": _float(disk.get("total_gb")),
            }
            for disk in _as_list(raw.get("disks"))
        ],
        "services": [
            {
                "name": _text(service.get("name")),
                "status": _text(service.get("status")),
                "start_type": _text(service.get("start_type")),
            }
            for service in _as_list(raw.get("services"))
        ],
    }
    pending = raw.get("reboot_pending") or {}
    facts["reboot_pending"] = {
        "required": bool(pending.get("required")),
        "reasons": [_text(reason) for reason in _as_list(pending.get("reasons"))],
        "pending_file_rename_count": int(pending.get("pending_file_rename_count") or 0),
    }
    return facts


def _parse_linux_facts(stdout: str, stderr: str) -> dict[str, Any]:
    return _parse_record_facts(stdout, stderr, platform=PLATFORM_LINUX)


def _parse_record_facts(stdout: str, stderr: str, *, platform: str) -> dict[str, Any]:
    """Tab-separated fact records into the shape both platforms return.

    Written for Linux and reused unchanged by the PowerShell 2.0 Windows script, which emits the
    same records for the same reason: neither shell can produce JSON that survives a value with a
    quote in it. One parser rather than two is the point — a second copy would eventually disagree
    about what `uptime_seconds` means.
    """
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        raise HostOpsError("The host returned no facts. stderr: " + (stderr or "").strip()[:500])
    facts: dict[str, Any] = {
        "platform": platform,
        "hostname": "",
        "os": "",
        "whoami": "",
        "is_admin": False,
        "last_boot": "",
        "uptime_days": 0.0,
        "remote_time": "",
        "disks": [],
        "services": [],
    }
    reasons: list[str] = []
    pending_renames = 0
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        key = parts[0]
        if key == "disk" and len(parts) >= 4:
            facts["disks"].append(
                {
                    "mount": parts[1],
                    "free_gb": round(_float(parts[2]) / (1024 * 1024), 2),
                    "total_gb": round(_float(parts[3]) / (1024 * 1024), 2),
                }
            )
        elif key == "service" and len(parts) >= 4:
            facts["services"].append({"name": parts[1], "status": parts[2], "start_type": parts[3]})
        elif key == "reboot_reason" and len(parts) >= 2:
            reasons.append(parts[1])
        elif key == "uptime_seconds" and len(parts) >= 2:
            facts["uptime_days"] = round(_float(parts[1]) / 86400.0, 2)
        elif key == "is_admin" and len(parts) >= 2:
            facts["is_admin"] = parts[1].strip().lower() == "true"
        elif key == "pending_file_rename_count" and len(parts) >= 2:
            pending_renames = int(_float(parts[1]))
        elif key in facts and len(parts) >= 2:
            facts[key] = parts[1]
    facts["reboot_pending"] = {
        "required": bool(reasons),
        "reasons": reasons,
        # A Windows-only concept, and 0 on Linux so the shape does not change per platform.
        "pending_file_rename_count": pending_renames,
    }
    return facts


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
def is_service_up(status: str) -> bool:
    """True for a running Windows service or an active systemd unit."""
    return str(status or "").strip().lower() in _UP_STATES


def service_states(
    session: RemoteSession,
    services: Sequence[str],
    *,
    platform: str = "",
    timeout_seconds: int | None = None,
) -> dict[str, str]:
    """``{service name: status}`` — one sample, no waiting."""
    facts = read_facts(session, platform=platform, services=services, timeout_seconds=timeout_seconds)
    states = {str(item["name"]): str(item["status"]) for item in facts.get("services", [])}
    # A Windows service that does not exist is simply absent from Get-Service; saying so beats
    # reporting nothing, because "not installed" and "stopped" have different fixes.
    for name in services:
        states.setdefault(str(name), "not-found")
    return states


def wait_for_services(
    session: RemoteSession,
    services: Sequence[str],
    *,
    platform: str = "",
    timeout_seconds: int = 600,
    poll_seconds: int = 15,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, str], list[str], float]:
    """Poll until every named service is up, or the budget expires.

    Returns ``(states, not_up, waited_seconds)``. Sampling once — as soon as the transport
    answers — is what made a healthy restart report ``not running: MSSQL$APPDB`` twice on
    2026-08-03: ``sshd`` is back long before SQL Server is. The elapsed budget is returned so
    the failure message can say *how long we actually waited* rather than implying an instant
    verdict.
    """
    names = [str(name).strip() for name in services if str(name).strip()]
    if not names:
        return {}, [], 0.0
    started = time.monotonic()
    deadline = started + max(1, int(timeout_seconds))
    while True:
        states = service_states(session, names, platform=platform)
        not_up = [name for name in names if not is_service_up(states.get(name, ""))]
        waited = round(time.monotonic() - started, 1)
        if not not_up or time.monotonic() >= deadline:
            return states, not_up, waited
        sleep(max(1, int(poll_seconds)))


def _service_command(action: str, name: str, *, platform: str) -> str:
    """The command that performs ``action`` on one service, per platform."""
    action = str(action).strip().lower()
    if platform == PLATFORM_WINDOWS:
        verbs = {
            "start": f"Start-Service -Name {quote_powershell(name)}",
            # -Force stops dependent services too; without it stopping SQL Server fails
            # whenever SQL Agent is running, which is always.
            "stop": f"Stop-Service -Name {quote_powershell(name)} -Force",
            "restart": f"Restart-Service -Name {quote_powershell(name)} -Force",
        }
    else:
        verbs = {
            "start": f"systemctl start {name}",
            "stop": f"systemctl stop {name}",
            "restart": f"systemctl restart {name}",
        }
    if action not in verbs:
        raise HostOpsError(f"Unknown service action '{action}'. Use: status, start, stop, restart.")
    return verbs[action]


def run_privileged(session: RemoteSession, command: str, *, timeout_seconds: int | None = None):
    """Public face of :func:`_run_privileged` for callers that hold a session but not a target.

    The platform is read off the session's own access block rather than passed in — a caller that
    has to state it can state it wrongly, and "run this as root" is not a question about which OS
    the caller thinks it is talking to.
    """
    platform = PLATFORM_WINDOWS if str(
        getattr(session.access, "shell", "") or "").strip().lower() == "powershell" else PLATFORM_LINUX
    return _run_privileged(session, command, platform=platform, timeout_seconds=timeout_seconds)


def _run_privileged(session: RemoteSession, command: str, *, platform: str, timeout_seconds: int | None = None):
    """Run a command that needs administrative rights on the target.

    Windows: the session's account is already the administrator (WinRM/SSH log in as one).
    Linux: root is needed for ``systemctl`` and ``shutdown``, so a non-root login goes through
    ``sudo -S`` with the password on **stdin** — never on the remote argv, where ``ps`` would
    show it to every user on the box.
    """
    if platform == PLATFORM_WINDOWS:
        return session.run_script(command, shell="powershell", timeout_seconds=timeout_seconds)
    run_sudo = getattr(session, "run_sudo", None)
    username = str(getattr(session.access, "username", "") or "")
    if callable(run_sudo) and username != "root":
        return run_sudo(
            command,
            sudo_password=getattr(session.access, "password", "") or None,
            timeout_seconds=timeout_seconds,
        )
    return session.run(command, timeout_seconds=timeout_seconds)


# --------------------------------------------------------------------------- #
# Reachability
# --------------------------------------------------------------------------- #
def wait_for_port(
    host: str,
    port: int,
    *,
    up: bool,
    timeout_seconds: int,
    poll_seconds: int = 10,
    connect_timeout: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, float]:
    """Wait until ``host:port`` accepts connections (``up=True``) or stops (``up=False``).

    Returns ``(reached, waited_seconds)``. Both directions matter for a restart: without the
    down-wait, a machine that has not started shutting down yet is indistinguishable from one
    that already came back, and the whole restart is declared finished in two seconds.
    """
    started = time.monotonic()
    deadline = started + max(1, int(timeout_seconds))
    while True:
        try:
            with socket.create_connection((host, int(port)), timeout=connect_timeout):
                reachable = True
        except OSError:
            reachable = False
        if reachable == up:
            return True, round(time.monotonic() - started, 1)
        if time.monotonic() >= deadline:
            return False, round(time.monotonic() - started, 1)
        sleep(max(1, int(poll_seconds)))


# --------------------------------------------------------------------------- #
# The maintenance-window guard
# --------------------------------------------------------------------------- #
def check_maintenance_window(
    report: GateReport,
    window: dict[str, Any] | None,
    *,
    ignore: bool = False,
    now: datetime | None = None,
) -> None:
    """Gate an operation on its approved window. No window configured = no gate.

    Two shapes are accepted, and both are honest about what they mean:

    * ``{"start": "2026-08-03 19:00", "end": "2026-08-03 21:00"}`` — one approved window, the
      form a change request is written in;
    * any ``time_window`` block (``from_hour`` / ``to_hour`` ...) — the recurring form, evaluated
      by :mod:`db_ops.lib.time_window`, which stays the only authority on those comparisons.

    A caller may override with ``ignore_window``; the gate is still recorded, so the evidence
    shows the operation ran outside its window *and* that someone chose to.
    """
    if not window:
        return
    current = now or datetime.now()
    start = str(window.get("start") or "").strip()
    end = str(window.get("end") or "").strip()
    if start or end:
        try:
            start_at = _parse_moment(start) if start else current
            end_at = _parse_moment(end) if end else current
        except ValueError as exc:
            report.add("schedule.maintenance_window", FAIL, str(exc))
            return
        inside = start_at <= current <= end_at
        detail = (
            f"inside the approved window {start} - {end}"
            if inside
            else f"now {current:%Y-%m-%d %H:%M} is outside the approved window {start} - {end}"
        )
    else:
        parsed = time_window.parse_time_window_config({"time_window": window}, context="maintenance window")
        reason = time_window.time_window_closed_reason(parsed.time_window, current)
        inside = not reason
        detail = "inside the approved window" if inside else f"now {current:%Y-%m-%d %H:%M} is {reason}"
    report.add(
        "schedule.maintenance_window",
        OK if inside else FAIL,
        detail,
        blocking=not ignore,
        override="ignore-window",
    )


def _parse_moment(text: str) -> datetime:
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    raise ValueError(f"maintenance window bound is not a date/time: {text!r} (use 'YYYY-MM-DD HH:MM').")


# --------------------------------------------------------------------------- #
# Entry points — JSON in, GateReport dict out
# --------------------------------------------------------------------------- #
def host_facts(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Read-only: what state is this host in. Never changes anything.

    Gates the things an operator would otherwise have to eyeball in the facts: a pending reboot,
    a full system disk, a service that is not up. Read-only means these are warnings, not
    blockers — this command reports, the operation that follows decides.
    """
    target, policy, overrides = _prepare(request, data_dir=data_dir)
    services = _services_of(request)
    report = GateReport("facts", target=target.describe(), echo=echo)
    report.note("target", target.to_dict())

    with open_host_session(
        target, data_dir=data_dir, connect_timeout_seconds=int(policy["connect_timeout_seconds"])
    ) as session:
        # Which PowerShell exists on that host decides which script can run at all. Passing the
        # dialect rather than assuming the newest is the difference between a fact sheet and
        # "Get-CimInstance is not recognized" on a 2008 R2 box — a message that reads like a
        # permissions problem. Linux ignores it.
        dialect = select_powershell_dialect(target.profile).tool if target.is_windows else ""
        facts = read_facts(
            session,
            platform=target.platform,
            services=services,
            timeout_seconds=int(policy["facts_timeout_seconds"]),
            dialect=dialect,
        )
    report.note("shell_dialect", dialect)
    report.note("facts", facts)
    _gate_facts(report, facts, policy, services, profile=target.profile)
    return _finish(report, request, overrides)


def service_control(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Start / stop / restart services on a host, and wait for the end state.

    ``action: "status"`` is read-only. Anything else changes the host and therefore needs
    ``confirm: true`` — the same contract as :func:`restart_host`, because stopping a database
    service is exactly as disruptive as restarting the machine it runs on.
    """
    target, policy, overrides = _prepare(request, data_dir=data_dir)
    services = _services_of(request)
    if not services:
        raise HostOpsError("services is required: a list of Windows service names or systemd units.")
    action = str(request.get("action") or "status").strip().lower()
    report = GateReport(f"service-{action}", target=target.describe(), echo=echo)
    report.note("target", target.to_dict())
    report.note("services", services)

    changing = action != "status"
    with open_host_session(
        target, data_dir=data_dir, connect_timeout_seconds=int(policy["connect_timeout_seconds"])
    ) as session:
        before = service_states(session, services, platform=target.platform)
        report.note("states_before", before)
        report.add(
            "service.before",
            OK,
            ", ".join(f"{name}={state}" for name, state in before.items()),
            data=before,
        )
        # Rehearsing is not performing: a dry run asks for no confirmation, so nobody learns to
        # type "yes" at a prompt for something that was never going to happen.
        if changing and request.get("dry_run"):
            report.add(
                "service.dry_run", OK,
                f"would {action}: {', '.join(services)} on {target.host}", blocking=False,
            )
            return _finish(report, request, overrides)
        if changing and not _authorized(
            report,
            request,
            operation=f"host-service ({action})",
            target=target,
            effects=[
                f"{action.upper()}: {', '.join(services)}",
                "every application connected through these services is interrupted"
                if action in ("stop", "restart")
                else "the services are started; existing connections are unaffected",
            ],
        ):
            return _finish(report, request, overrides)

        if changing:
            for name in services:
                result = _run_privileged(
                    session, _service_command(action, name, platform=target.platform),
                    platform=target.platform,
                    timeout_seconds=int(policy["facts_timeout_seconds"]),
                )
                report.add(
                    f"service.{action}",
                    OK if result.exit_code == 0 else FAIL,
                    f"{name}: exit {result.exit_code}"
                    + (f" - {(result.stderr or result.stdout).strip()[:200]}" if result.exit_code else ""),
                    data={"service": name, "exit_code": result.exit_code},
                )
            if action in ("start", "restart"):
                report.say("Waiting for the services to report up...")
                states, not_up, waited = wait_for_services(
                    session, services, platform=target.platform,
                    timeout_seconds=int(policy["services_timeout_seconds"]),
                    poll_seconds=int(policy["poll_seconds"]),
                )
                report.note("states_after", states)
                report.add(
                    "service.up",
                    OK if not not_up else FAIL,
                    f"all services up after {waited}s"
                    if not not_up
                    else f"still not up after {waited}s (budget {policy['services_timeout_seconds']}s): "
                         + ", ".join(not_up),
                    data=states,
                )
            else:
                states = service_states(session, services, platform=target.platform)
                report.note("states_after", states)
                still_up = [name for name, state in states.items() if is_service_up(state)]
                report.add(
                    "service.stopped",
                    OK if not still_up else FAIL,
                    "all services stopped" if not still_up else "still running: " + ", ".join(still_up),
                    data=states,
                )
    return _finish(report, request, overrides)


def restart_host(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restart a host and prove it came back — Windows or Linux, same contract.

    The sequence is the one the CU26 night validated, and each step exists because skipping it
    hides a real failure mode: record what the host looked like *before*; wait for it to stop
    answering (otherwise "it's back" is just "it never left"); wait for it to answer again; wait
    for its services; then re-read the facts to show the pending-reboot state actually cleared.

    Needs ``confirm: true``. ``dry_run`` runs everything up to the restart and prints what it
    would do — the safe way to prove the target, the credential and the service list are right.
    """
    target, policy, overrides = _prepare(request, data_dir=data_dir)
    services = _services_of(request)
    reason = str(request.get("reason") or "db_ops planned restart").strip()
    report = GateReport("restart", target=target.describe(), echo=echo)
    report.note("target", target.to_dict())
    check_maintenance_window(report, request.get("window"), ignore=bool(request.get("ignore_window")))

    session = open_host_session(
        target, data_dir=data_dir, connect_timeout_seconds=int(policy["connect_timeout_seconds"])
    )
    try:
        facts = read_facts(
            session, platform=target.platform, services=services,
            timeout_seconds=int(policy["facts_timeout_seconds"]),
        )
        report.note("facts_before", facts)
        pending = facts.get("reboot_pending", {})
        report.add(
            "restart.target",
            OK,
            f"{facts.get('hostname')} ({facts.get('os')}), up {facts.get('uptime_days')} days"
            + (f", pending reboot: {'; '.join(pending.get('reasons', []))}" if pending.get("required") else ""),
            data={"uptime_days": facts.get("uptime_days"), "reboot_pending": pending},
        )
        if report.blockers(overrides):
            report.add("restart.aborted", FAIL, "blocking gates failed; the host was not restarted")
            return _finish(report, request, overrides)
        if request.get("dry_run"):
            report.add(
                "restart.dry_run", OK,
                f"would restart {target.host} ({reason}) and wait up to "
                f"{policy['up_timeout_seconds']}s for it to return",
                blocking=False,
            )
            return _finish(report, request, overrides)
        if not _authorized(
            report,
            request,
            operation="host-restart",
            target=target,
            effects=[
                f"{facts.get('hostname') or target.host} will be REBOOTED now",
                "every session, service and running job on it is interrupted",
                f"services that must come back: {', '.join(services)}" if services
                else "no services were named, so nothing is waited for after the restart",
                f"it has been up {facts.get('uptime_days')} days",
            ],
        ):
            return _finish(report, request, overrides)

        report.say(f"Restarting {target.host}...")
        _issue_restart(session, target, reason=reason, report=report)
    finally:
        session.close()

    port = target.port
    went_down, waited = wait_for_port(
        target.host, port, up=False,
        timeout_seconds=int(policy["down_timeout_seconds"]),
        poll_seconds=int(policy["poll_seconds"]),
    )
    # Not a blocker: a host that reboots fast enough to be missed between two polls is fine,
    # and refusing to continue would leave the operator with less information, not more.
    report.add(
        "restart.went_down",
        OK if went_down else WARN,
        f"host stopped answering on port {port} after {waited}s"
        if went_down
        else f"host never stopped answering on port {port} within {policy['down_timeout_seconds']}s "
             "(a very fast restart looks the same as no restart from here)",
        blocking=False,
    )

    report.say("Waiting for the host to come back...")
    came_back, waited = wait_for_port(
        target.host, port, up=True,
        timeout_seconds=int(policy["up_timeout_seconds"]),
        poll_seconds=int(policy["poll_seconds"]),
    )
    report.add(
        "restart.back_online",
        OK if came_back else FAIL,
        f"{target.host}:{port} is answering again after {waited}s"
        if came_back
        else f"{target.host}:{port} did not answer within {policy['up_timeout_seconds']}s",
    )
    if not came_back:
        return _finish(report, request, overrides)

    with open_host_session(
        target, data_dir=data_dir, connect_timeout_seconds=int(policy["connect_timeout_seconds"])
    ) as session:
        if services:
            report.say("Waiting for the services to start...")
            states, not_up, waited = wait_for_services(
                session, services, platform=target.platform,
                timeout_seconds=int(policy["services_timeout_seconds"]),
                poll_seconds=int(policy["poll_seconds"]),
            )
            report.add(
                "restart.services_up",
                OK if not not_up else FAIL,
                f"all {len(services)} service(s) up after {waited}s"
                if not not_up
                else f"still not up after {waited}s (budget {policy['services_timeout_seconds']}s): "
                     + ", ".join(not_up),
                data=states,
            )
        facts_after = read_facts(
            session, platform=target.platform, services=services,
            timeout_seconds=int(policy["facts_timeout_seconds"]),
        )
    report.note("facts_after", facts_after)
    pending_after = facts_after.get("reboot_pending", {})
    report.add(
        "restart.pending_cleared",
        OK if not pending_after.get("required") else WARN,
        "no pending reboot recorded after the restart"
        if not pending_after.get("required")
        else "the host still reports a pending reboot: " + "; ".join(pending_after.get("reasons", [])),
        blocking=False,
        data=pending_after,
    )
    report.add(
        "restart.uptime",
        OK if _float(facts_after.get("uptime_days")) < _float(facts.get("uptime_days")) else WARN,
        f"uptime is now {facts_after.get('uptime_days')} days (was {facts.get('uptime_days')})",
        blocking=False,
    )
    return _finish(report, request, overrides)


def _issue_restart(session: RemoteSession, target: HostTarget, *, reason: str, report: GateReport) -> None:
    """Ask the host to restart, tolerating the transport dying mid-command.

    A restart is the one command that can legitimately kill its own connection before it
    reports an exit code — Linux tears down sshd as part of the shutdown. Treating that as a
    failure would abort the wait that proves whether the restart worked, which is the only part
    that matters.
    """
    if target.is_windows:
        # /t 20 gives sessions a moment to close; /d p:2:4 records it as a planned OS
        # reconfiguration in the shutdown event log, so the restart is attributable later.
        command = f'shutdown.exe /r /t 20 /c {_windows_reason(reason)} /d p:2:4'
    else:
        command = "shutdown -r +0"
    try:
        result = _run_privileged(session, command, platform=target.platform, timeout_seconds=60)
        detail = f"restart requested ({reason})"
        if result.exit_code != 0:
            detail += f"; exit {result.exit_code}: {(result.stderr or result.stdout).strip()[:200]}"
        report.add(
            "restart.requested",
            OK if result.exit_code == 0 else WARN,
            detail,
            blocking=False,
            data={"command": command, "exit_code": result.exit_code},
        )
    except RemoteExecError as exc:
        report.add(
            "restart.requested",
            OK,
            f"restart requested ({reason}); the session closed before the command replied, "
            f"which is normal for a shutdown: {exc}",
            blocking=False,
            data={"command": command},
        )


def _windows_reason(reason: str) -> str:
    """Quote the shutdown comment for cmd's parser (PowerShell hands it through verbatim)."""
    # /c takes at most 512 characters and no embedded double quotes.
    clean = str(reason).replace('"', "'")[:500]
    return f'"{clean}"'


# --------------------------------------------------------------------------- #
# Shared request plumbing
# --------------------------------------------------------------------------- #
def _prepare(
    request: dict[str, Any], *, data_dir: str | Path | None
) -> tuple[HostTarget, dict[str, Any], list[str]]:
    if not isinstance(request, dict):
        raise HostOpsError("request must be a JSON object.")
    target = resolve_host(
        str(request.get("target") or ""),
        data_dir=data_dir,
        access=request.get("access") or None,
        platform=str(request.get("platform") or ""),
    )
    policy = load_maintenance_policy(
        data_dir, server_id=target.server_id, overrides=request.get("wait") or {}
    )
    overrides = list(request.get("overrides") or [])
    if request.get("ignore_window"):
        overrides.append("ignore-window")
    return target, policy, overrides


def _services_of(request: dict[str, Any]) -> list[str]:
    raw = request.get("services") or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    return [str(name).strip() for name in raw if str(name).strip()]


def _authorized(
    report: GateReport,
    request: dict[str, Any],
    *,
    operation: str,
    target: HostTarget,
    effects: Sequence[str] = (),
) -> bool:
    """Authorize a change to a live host — one shared control, never a per-command one.

    Delegates to :func:`db_ops.common.confirm.require_confirmation`: declared intent
    (``confirm``) **plus** a human typing ``yes`` at a terminal that shows this target and this
    consequence. Callers must have handled ``dry_run`` before reaching here.
    """
    rules = confirm.load_operation(operation)
    return confirm.require_confirmation(
        report,
        request,
        operation=operation,
        target=f"{target.describe()} — {target.host}",
        effects=[*rules["effects"], *effects],
        confirmations=int(rules["confirmations"]),
        # The challenge is the target's own id from the inventory, not the host string the caller
        # typed: an operator who reproduces what they were shown has read the resolved target,
        # which is the one that will actually be restarted.
        challenge=str(getattr(target, "server_id", "") or target.host),
    )


def _finish(report: GateReport, request: dict[str, Any], overrides: Iterable[str]) -> dict[str, Any]:
    result = report.to_dict(overrides)
    wanted = request.get("evidence", True)
    if wanted:
        root = None if wanted is True else str(wanted)
        result["evidence_file"] = str(report.write(root, overrides=overrides))
    return result


def _gate_facts(
    report: GateReport, facts: dict[str, Any], policy: dict[str, Any], services: Sequence[str],
    profile: TargetProfile | None = None,
) -> None:
    """Turn raw facts into the gates every host operation cares about."""
    report.add(
        "host.identity",
        OK,
        f"{facts.get('hostname')} ({facts.get('os')}), local time {facts.get('remote_time')}",
        blocking=False,
    )
    drift = os_drift(profile, str(facts.get("os") or ""))
    if drift:
        # The inventory's `os` is not decoration: `lib.target_profile` reads a PowerShell dialect
        # and a "can this host be managed at all" verdict out of it. A caption that is two
        # Windows generations out of date is a wrong answer waiting to be given, and nothing
        # compared the two until 2026-08-19 — this gate is where the comparison finally happens,
        # on the one command that already holds both values.
        report.add("host.os_matches_inventory", WARN, drift, blocking=False)
    uptime = _float(facts.get("uptime_days"))
    report.add(
        "host.uptime",
        WARN if uptime > _float(policy["max_uptime_days_warn"]) else OK,
        f"up {uptime} days (last boot {facts.get('last_boot')})",
        blocking=False,
    )
    for disk in facts.get("disks", []):
        free = _float(disk.get("free_gb"))
        total = _float(disk.get("total_gb")) or 1.0
        report.add(
            "disk.free",
            WARN if free < _float(policy["min_free_gb_system"]) else OK,
            f"{disk.get('mount')} {free} GB free of {total} GB ({round(free / total * 100, 1)}%)",
            blocking=False,
            data=disk,
        )
    pending = facts.get("reboot_pending", {})
    report.add(
        "host.reboot_pending",
        WARN if pending.get("required") else OK,
        "; ".join(pending.get("reasons", [])) or "no pending reboot recorded",
        blocking=False,
        data=pending,
    )
    if services:
        states = {str(item["name"]): str(item["status"]) for item in facts.get("services", [])}
        not_up = [name for name in services if not is_service_up(states.get(name, ""))]
        report.add(
            "services.up",
            OK if not not_up else WARN,
            "all requested services are up" if not not_up else "not up: " + ", ".join(not_up),
            blocking=False,
            data=states,
        )


# --------------------------------------------------------------------------- #
# Small conversions
# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> list[Any]:
    """PowerShell's ConvertTo-Json collapses a one-element array to a bare object.

    So ``services`` comes back as a list on a host with two matching services and as a dict on a
    host with one. Normalizing here keeps every caller from re-learning that.
    """
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    return [value]


def _text(value: Any) -> str:
    # PowerShell sometimes serializes a long string as {"value": ..., "Length": ...}.
    if isinstance(value, dict):
        return str(value.get("value", ""))
    return "" if value is None else str(value)


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


