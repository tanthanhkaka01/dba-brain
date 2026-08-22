from __future__ import annotations

import base64
import json
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from db_ops.lib.shell import powershell_executable
from db_ops.sre.config import SreOperationalConfig


def run_ssh_command(
    sre_config: SreOperationalConfig,
    *,
    host: str,
    command_args: list[str],
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str] | list[str]:
    remote_command = (
        command_args[0] if len(command_args) == 1
        else " ".join(shlex.quote(arg) for arg in command_args)
    )
    command = _ssh_command(sre_config, remote_command, host=host)
    if dry_run:
        return command
    return subprocess.run(command, check=False, capture_output=True, text=True)


def run_bastion_ansible(
    sre_config: SreOperationalConfig,
    *,
    target: str,
    inventory: str | None = None,
    args: list[str] | None = None,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str] | list[str]:
    parts = ["ANSIBLE_CONFIG=automation/ansible/ansible.cfg", "ansible"]
    if inventory:
        parts.extend(["-i", shlex.quote(inventory)])
    parts.append(shlex.quote(target))
    parts.extend(shlex.quote(arg) for arg in (args or []))
    command = _ssh_command(sre_config, _join_remote(["cd /opt/db-sre/repo", " ".join(parts)]))
    if dry_run:
        return command
    return subprocess.run(command, check=False, capture_output=True, text=True)


def run_bastion_ansible_playbook(
    sre_config: SreOperationalConfig,
    *,
    playbook: str,
    inventory: str | None = None,
    args: list[str] | None = None,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str] | list[str]:
    parts = ["ANSIBLE_CONFIG=automation/ansible/ansible.cfg", "ansible-playbook"]
    if inventory:
        parts.extend(["-i", shlex.quote(inventory)])
    parts.append(shlex.quote(playbook))
    parts.extend(shlex.quote(arg) for arg in (args or []))
    command = _ssh_command(sre_config, _join_remote(["cd /opt/db-sre/repo", " ".join(parts)]))
    if dry_run:
        return command
    return subprocess.run(command, check=False, capture_output=True, text=True)


def run_bastion_script(
    sre_config: SreOperationalConfig,
    *,
    script_name: str,
    args: list[str] | None = None,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str] | list[str]:
    bash_dir = sre_config.bash_dir()
    if not bash_dir:
        raise ValueError("SRE config 'sre.automation.bash_dir' is required to run bastion scripts.")
    script_path = _resolve_script(bash_dir, script_name, ".sh")
    try:
        relative_script = script_path.relative_to(sre_config.root_dir).as_posix()
    except ValueError:
        relative_script = f"automation/bash/{script_path.name}"
    guest_pass = sre_config.guest_password()
    env_prefix = f"GUEST_BECOME_PASS={shlex.quote(guest_pass)} " if guest_pass else ""
    # Inject target node IPs so bootstrap scripts use config IPs instead of hardcoded defaults.
    # The first non-flag arg is the group name (e.g. "mysql").
    script_group = next((a for a in (args or []) if not a.startswith("-")), None)
    if script_group:
        nodes = sre_config.inventory.get("groups", {}).get(script_group, [])
        if nodes:
            nodes_str = " ".join(f"{n['name']}:{n['ip']}" for n in nodes)
            env_prefix += f"DB_SRE_TARGET_NODES={shlex.quote(nodes_str)} "
    remote_command = _join_remote([
        "cd /opt/db-sre/repo",
        f"chmod +x {shlex.quote(relative_script)}",
        env_prefix + " ".join([shlex.quote(f"./{relative_script}"), *(shlex.quote(a) for a in (args or []))]),
    ])
    command = _ssh_command(sre_config, remote_command)
    if dry_run:
        return command
    # Retry on TCP-level SSH failures (rc=255) — bastion may be briefly unreachable
    # right after heavy VMware Tools operations (repo sync, key distribution).
    _wait_for_bastion_ssh(sre_config, timeout=120)
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _wait_for_bastion_ssh(sre_config: SreOperationalConfig, *, timeout: int = 120) -> None:
    """Block until bastion's SSH port accepts connections, up to `timeout` seconds."""
    probe = _ssh_command(sre_config, "true")
    deadline = time.monotonic() + timeout
    while True:
        result = subprocess.run(probe, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return
        # rc=255 → TCP failure (connection refused / timed out); anything else is
        # an auth or remote-command error — not a connectivity issue, stop waiting.
        if result.returncode != 255:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        print(f"[sre] bastion SSH not yet ready (rc=255), retrying in 10s "
              f"({int(remaining)}s remaining) ...", flush=True)
        time.sleep(10)


def run_powershell_script(
    sre_config: SreOperationalConfig,
    *,
    script_name: str,
    args: list[str] | None = None,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str] | list[str]:
    ps_dir = sre_config.powershell_dir()
    if not ps_dir:
        raise ValueError("SRE config 'sre.automation.powershell_dir' is required to run PowerShell scripts.")
    script_path = _resolve_script(ps_dir, script_name, ".ps1")
    script_args = list(args or [])
    if script_args[:1] == ["--"]:
        script_args = script_args[1:]
    command = [
        powershell_executable(),
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(script_path),
        "-DbSrePayloadJsonBase64", _build_powershell_payload(sre_config),
        *script_args,
    ]
    if dry_run:
        return command
    return _run_streaming(command)


def check_shared_vms(
    sre_config: SreOperationalConfig,
    *,
    vm_names: list[str] | None = None,
    dry_run: bool = False,
) -> list[subprocess.CompletedProcess[str] | list[str]]:
    results: list[subprocess.CompletedProcess[str] | list[str]] = []
    net_interface = shlex.quote(sre_config.net_interface())
    nodes = sre_config.inventory_group("shared")
    if vm_names:
        nodes = [n for n in nodes if n.get("name") in vm_names]
    for node in nodes:
        name = str(node["name"])
        ip = str(node["ip"])
        remote_command = (
            "set -e; "
            f"test \"$(hostname)\" = {shlex.quote(name)}; "
            f"ip -4 addr show {net_interface} | grep -q {shlex.quote(ip + '/')}; "
            "systemctl is-system-running --wait >/tmp/db-sre-system-state.txt 2>&1 || "
            "grep -Eq '^(running|degraded)$' /tmp/db-sre-system-state.txt; "
            f"echo {shlex.quote(name + ' OK hostname ip system')}"
        )
        command = _ssh_command(sre_config, remote_command, host=ip)
        results.append(command if dry_run else subprocess.run(command, check=False, capture_output=True, text=True))
    return results


def check_mysql_cluster(
    sre_config: SreOperationalConfig,
    *,
    dry_run: bool = False,
) -> list[subprocess.CompletedProcess[str] | list[str]]:
    mysql_defaults = dict(sre_config.database_defaults.get("mysql") or {})
    primary = sre_config.first_node("mysql")
    cluster_name = str(mysql_defaults.get("cluster_name", "mysql-lab"))
    admin_user = str(mysql_defaults.get("cluster_admin_user", "clusteradmin"))
    admin_password = str(mysql_defaults.get("cluster_admin_password", ""))
    port = int(mysql_defaults.get("classic_port", 3306))
    results: list[subprocess.CompletedProcess[str] | list[str]] = [
        run_bastion_ansible(sre_config, target="mysql", args=["-m", "ping"], dry_run=dry_run),
        run_bastion_ansible(sre_config, target="mysql", args=["-m", "shell", "-a", "systemctl is-active mysql"], dry_run=dry_run),
    ]
    status_js = (
        "var s=dba.getCluster("
        f"{json.dumps(cluster_name)}"
        ").status();"
        "print(JSON.stringify(s,null,2));"
        "var t=s.defaultReplicaSet.topology;"
        "var members=Object.keys(t);"
        "var primary=0;var secondary=0;"
        "members.forEach(function(k){"
        "if(t[k].memberRole==='PRIMARY') primary++;"
        "if(t[k].memberRole==='SECONDARY') secondary++;"
        "});"
        "if(s.defaultReplicaSet.status!=='OK'||members.length!==3||primary!==1||secondary!==2)"
        "{throw new Error('MySQL cluster is not healthy');}"
    )
    remote_command = (
        "set -e; "
        f"mysqlsh --js --uri {shlex.quote(admin_user)}@{shlex.quote(str(primary['ip']))}:{port} "
        f"--password={shlex.quote(admin_password)} "
        f"-e {shlex.quote(status_js)}"
    )
    command = _ssh_command(sre_config, remote_command, host=str(primary["ip"]))
    results.append(command if dry_run else subprocess.run(command, check=False, capture_output=True, text=True))
    return results


def check_postgresql_ha(
    sre_config: SreOperationalConfig,
    *,
    dry_run: bool = False,
) -> list[subprocess.CompletedProcess[str] | list[str]]:
    primary = sre_config.first_node("postgresql")
    results: list[subprocess.CompletedProcess[str] | list[str]] = [
        run_bastion_ansible(
            sre_config,
            target="postgresql",
            inventory="inventory/postgresql/hosts.yml",
            args=["-m", "ping"],
            dry_run=dry_run,
        ),
        run_bastion_ansible(
            sre_config,
            target="postgresql",
            inventory="inventory/postgresql/hosts.yml",
            args=["-m", "shell", "-a", "systemctl is-active postgresql"],
            dry_run=dry_run,
        ),
    ]
    sql = (
        "WITH role_check AS (SELECT NOT pg_is_in_recovery() AS is_primary),"
        "repl_check AS (SELECT count(*)::int AS replicas FROM pg_stat_replication WHERE state='streaming')"
        " SELECT CASE WHEN is_primary AND replicas=2 THEN 'OK primary with 2 streaming replicas'"
        " ELSE 'FAIL is_primary='||is_primary||' replicas='||replicas END"
        " FROM role_check, repl_check;"
    )
    remote_command = (
        "set -e; "
        f"result=$(sudo -u postgres psql -d postgres -Atc {shlex.quote(sql)}); "
        "echo \"$result\"; "
        "echo \"$result\" | grep -q '^OK '"
    )
    command = _ssh_command(sre_config, remote_command, host=str(primary["ip"]))
    results.append(command if dry_run else subprocess.run(command, check=False, capture_output=True, text=True))
    return results


def list_vmware_commands() -> list[tuple[str, str, str]]:
    """Return (name, script_file, description) for known VMware wrapper commands."""
    return [
        ("bootstrap-template", "00-bootstrap-template-remote-access.ps1", "Bootstrap remote access for base VM templates."),
        ("clone-vms", "01-clone-vms.ps1", "Clone VMs from the configured VMware template."),
        ("set-vm-resources", "02-set-vm-resources.ps1", "Set CPU and memory for VMs from inventory."),
        ("start-vms", "03-start-vms.ps1", "Start VMs from inventory."),
        ("fix-guest-identity", "04-fix-guest-identity.ps1", "Fix hostname, machine-id, and static IP for VMs."),
        ("vm-info", "05-get-vm-info.ps1", "Query VMware guest and VM information."),
        ("snapshot-vms", "06-snapshot-vms.ps1", "Create snapshots for inventory VMs."),
        ("deploy-host-ssh-key", "08-deploy-host-ssh-key.ps1", "Deploy Windows SSH public key and configure NOPASSWD sudo on VMs."),
        ("bootstrap-bastion", "07-bootstrap-bastion-ansible.ps1", "Sync the repository to bastion-01 for Ansible operations."),
        ("add-oracle-shared-disks", "08-add-oracle-shared-disks.ps1", "Create and attach shared VMDK disks for Oracle RAC (run while RAC VMs are stopped)."),
        ("stage-oracle-installer", "09-stage-oracle-installer.ps1", "Copy Oracle installer ZIPs via bastion to RAC or DG nodes (-Group oracle_rac|oracle_dg)."),
        ("delete-vms", "delete-vms.ps1", "Stop and permanently delete VM directories by group or name (-Group mysql|postgresql|shared|all, -VmName ...)."),
        ("stop-vms", "stop-vms.ps1", "Stop VMs by group or name (-Group mysql|postgresql|shared|all, -VmName ...)."),
        ("stop-all-running-vms", "stop-all-running-vms.ps1", "Stop all running VMware Workstation VMs."),
        ("list-running-vms", "list-running-vms.ps1", "List running VMware Workstation VMs."),
    ]


def _run_streaming(command: list[str]) -> subprocess.CompletedProcess[str]:
    def _drain(stream, file):
        for line in stream:
            print(line, end="", file=file, flush=True)

    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    ) as proc:
        t_out = threading.Thread(target=_drain, args=(proc.stdout, sys.stdout))
        t_err = threading.Thread(target=_drain, args=(proc.stderr, sys.stderr))
        t_out.start()
        t_err.start()
        proc.wait()
        t_out.join()
        t_err.join()

    return subprocess.CompletedProcess(args=command, returncode=proc.returncode, stdout="", stderr="")


def _ssh_command(sre_config: SreOperationalConfig, remote_command: str, *, host: str | None = None) -> list[str]:
    user = sre_config.guest_user()
    bastion = sre_config.bastion_host()
    target_host = host or bastion
    ssh_key = sre_config.ssh_identity_file()
    cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
    ]
    if ssh_key:
        cmd += ["-i", ssh_key]
    # Route through bastion when the target is a non-bastion node.
    # Wrap the remote_command in a second ssh hop executed ON bastion so that
    # Windows host only needs its key on bastion; bastion's key reaches all other nodes.
    # (ProxyCommand is avoided: on Windows the subprocess can't reach the SSH agent.)
    if target_host != bastion:
        remote_command = (
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"
            " -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
            f" {shlex.quote(user)}@{shlex.quote(target_host)}"
            f" {shlex.quote(remote_command)}"
        )
        target_host = bastion
    cmd += [f"{user}@{target_host}", remote_command]
    return cmd


def _build_powershell_payload(sre_config: SreOperationalConfig) -> str:
    payload = {
        "vmware": sre_config.vmware,
        "credentials": sre_config.credentials,
        "inventory": sre_config.inventory,
        "database_defaults": sre_config.database_defaults,
        "oracle": sre_config.oracle,
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _resolve_script(root: Path, script_name: str, suffix: str) -> Path:
    candidate = root / script_name
    if candidate.suffix.lower() != suffix:
        candidate = candidate.with_suffix(suffix)
    if not candidate.exists():
        raise FileNotFoundError(f"Script not found: {candidate}")
    return candidate


def _join_remote(commands: list[str]) -> str:
    return " && ".join(commands)
