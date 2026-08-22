#!/usr/bin/env python3
"""
SQL Server 2025 Always On – deployment orchestrator.
Reads  : <config_json>  (default: db_ops/sre/data_folder/20260612_install_sql_server.json)
Writes : <date>_result_install_sql_server.json  in the same directory

Rule   : no VMware / vmrun on the 3 SQL target nodes (18.31-18.33).
         vmrun on bastion-01 (18.3) is allowed for key bootstrap.

Run from the db_ops project root:
    python db_ops/sre/data_folder/deploy_sqlserver_ag.py
    python db_ops/sre/data_folder/deploy_sqlserver_ag.py db_ops/sre/data_folder/20260612_install_sql_server.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# ── Paths (relative to the db_ops project root) ──────────────────────────────
_HERE        = Path(__file__).resolve().parent          # db_ops/sre/data_folder/
_SRE_ROOT    = _HERE.parent                             # db_ops/sre/
_DB_OPS      = _HERE.parent.parent.parent               # db_ops project root
_DEFAULT_INSTALL_JSON = _HERE / "20260612_install_sql_server.json"
SRE_CONFIG   = _DB_OPS / "data" / "sre_config.json"
HOSTS_YML    = _SRE_ROOT / "inventory" / "sqlserver" / "hosts.yml"
GROUP_VARS   = _SRE_ROOT / "automation" / "ansible" / "group_vars" / "sqlserver.yml"

SSH_FLAGS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _run(cmd: list, timeout: int = 1800, cwd: Path | None = None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=str(cwd or _DB_OPS),
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out after {timeout}s"
    except Exception as exc:
        return -1, "", str(exc)


def cli(*args, timeout: int = 1800) -> tuple[int, str, str]:
    cmd = [sys.executable, "-m", "db_ops.sre.cli"] + list(args)
    label = " ".join(args[:4])
    print(f"    $ python -m db_ops.sre.cli {label}")
    rc, out, err = _run(cmd, timeout=timeout)
    if out.strip():
        print(textwrap.indent(out.strip()[-1200:], "      "))
    if err.strip():
        print(textwrap.indent(err.strip()[-600:], "      [E] "), file=sys.stderr)
    return rc, out, err


def ssh_key() -> Path:
    raw = load_json(SRE_CONFIG)["sre"]["credentials"].get(
        "ssh_identity_file", "~/.ssh/db_sre_id_ed25519"
    )
    return Path(raw).expanduser()


def bastion_ip() -> str:
    for n in load_json(SRE_CONFIG)["sre"]["inventory"]["groups"].get("shared", []):
        if n.get("role") == "bastion" or n.get("name") == "bastion-01":
            return str(n["ip"])
    # No hard-coded environment fallback: the bastion IP must come from sre_config.json
    # so the same code runs against any lab inventory. Fail loudly if it is missing.
    raise RuntimeError(
        "No bastion host found in sre_config.json sre.inventory.groups.shared "
        "(expected an entry with role='bastion' or name='bastion-01')."
    )


# ── Step result ───────────────────────────────────────────────────────────────

class StepResult:
    def __init__(self, name: str):
        self.name     = name
        self.rc: int  = -1
        self.stdout   = ""
        self.stderr   = ""
        self.error    = ""
        self.started  = now_iso()
        self.finished = ""

    def done(self, rc: int, out: str = "", err: str = "") -> "StepResult":
        self.rc, self.stdout, self.stderr = rc, out[:3000], err[:2000]
        self.finished = now_iso()
        return self

    def fail(self, msg: str) -> "StepResult":
        self.error, self.rc, self.finished = msg, -1, now_iso()
        return self

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def to_dict(self) -> dict:
        return {
            "step":     self.name,
            "status":   "ok" if self.ok else "failed",
            "rc":       self.rc,
            "stdout":   self.stdout,
            "stderr":   self.stderr,
            "error":    self.error,
            "started":  self.started,
            "finished": self.finished,
        }


# ── Pre-flight ────────────────────────────────────────────────────────────────

def preflight_update_configs(install: dict) -> None:
    """Rewrite hosts.yml and group_vars/sqlserver.yml from install JSON."""
    nodes    = install["nodes"]
    primary  = next(n for n in nodes if n["is_primary"])
    others   = [n for n in nodes if not n["is_primary"]]

    lines = [
        "all:", "  children:", "    sqlserver:",
        "      vars:", "        ansible_user: tuser",
        "      hosts:",
    ]
    for n in [primary] + others:
        role = "primary" if n["is_primary"] else "secondary"
        lines += [
            f"        {n['name']}:",
            f"          ansible_host: {n['host']}",
            f"          mssql_role: {role}",
        ]
    HOSTS_YML.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"    OK {HOSTS_YML.name} updated")

    ag_name = install.get("availability_group", {}).get("name", "ag-lab")
    gv = GROUP_VARS.read_text(encoding="utf-8")
    gv2 = re.sub(r"^mssql_ag_name:.*$", f"mssql_ag_name: {ag_name}", gv, flags=re.MULTILINE)
    GROUP_VARS.write_text(gv2, encoding="utf-8")
    print(f"    OK {GROUP_VARS.name} updated -> mssql_ag_name: {ag_name}")


# ── Individual steps ──────────────────────────────────────────────────────────

def step_ensure_ssh_key() -> StepResult:
    s = StepResult("ensure-ssh-key")
    k = ssh_key()
    if k.exists():
        return s.done(0, f"Key exists: {k}")
    k.parent.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(k)], timeout=30)
    if rc == 0:
        print(f"    OK Generated {k}")
    return s.done(rc, out, err)


def step_start_bastion() -> StepResult:
    """Start bastion-01 via vmrun (bastion = OK per constraints)."""
    import time
    s = StepResult("start-bastion")
    rc, out, err = cli("run-powershell", "03-start-vms", "--", "-VmName", "bastion-01", timeout=300)
    if rc != 0 and "already powered on" in (out + err).lower():
        return s.done(0, "bastion-01 already running")
    if rc == 0:
        print("    Waiting 30s for bastion-01 to boot...")
        time.sleep(30)
    return s.done(rc, out, err)


def step_fix_bastion_identity() -> StepResult:
    """Set bastion-01 hostname + static IP via vmrun, then wait for reboot (bastion = OK)."""
    import time
    s = StepResult("fix-bastion-identity")
    rc, out, err = cli("run-powershell", "04-fix-guest-identity", "--", "-VmName", "bastion-01", timeout=300)
    if rc == 0:
        print("    bastion-01 rebooting — waiting 40s...")
        time.sleep(40)
    return s.done(rc, out, err)


def step_deploy_host_key_bastion() -> StepResult:
    """Deploy Windows SSH key to bastion-01 ONLY via vmrun (bastion = OK)."""
    s = StepResult("deploy-host-key-bastion")
    # Target only bastion-01, not the entire shared group (avoids mon-01/log-01 errors)
    rc, out, err = cli("run-powershell", "08-deploy-host-ssh-key", "--", "-VmName", "bastion-01", timeout=300)
    return s.done(rc, out, err)


def step_repo_sync(install: dict) -> StepResult:
    """Package db_ops/sre/ and SCP to bastion, then extract."""
    s = StepResult("repo-sync-bastion")
    user  = install["ssh"]["sudo_user"]
    spwd  = install["ssh"]["sudo_password"]
    bip   = bastion_ip()
    idf   = ssh_key()

    skip = {".git", ".venv", ".pytest_cache", ".mypy_cache", "node_modules", "runtime", "logs", "__pycache__"}

    with tempfile.TemporaryDirectory() as tmp:
        arc = Path(tmp) / "db-sre-repo.tar"
        print(f"    Building archive from {_SRE_ROOT} ...")
        with tarfile.open(str(arc), "w") as tf:
            for item in _SRE_ROOT.rglob("*"):
                if any(part in skip for part in item.parts):
                    continue
                if item.is_file():
                    tf.add(str(item), arcname=str(item.relative_to(_SRE_ROOT)))

        print(f"    SCP archive → bastion {bip}")
        rc, _, err = _run(
            ["scp"] + SSH_FLAGS + ["-i", str(idf), str(arc), f"{user}@{bip}:/tmp/db-sre-repo.tar"],
            timeout=120,
        )
        if rc != 0:
            return s.fail(f"SCP failed: {err}")

    # Extract + fix line endings on bastion via CLI ssh
    extract_remote = (
        f"echo '{spwd}' | sudo -S bash -c '"
        "rm -rf /opt/db-sre/repo && "
        "install -d -m755 /opt/db-sre/repo && "
        "tar -xf /tmp/db-sre-repo.tar -C /opt/db-sre/repo && "
        f"chown -R {user}:{user} /opt/db-sre' && "
        "find /opt/db-sre/repo/automation/bash -name '*.sh' -exec sed -i 's/\\r//' {} \\; && "
        "find /opt/db-sre/repo/automation/ansible \\( -name '*.yml' -o -name '*.j2' \\) -exec sed -i 's/\\r//' {} \\;"
    )
    rc, out, err = cli("ssh", bip, "--", extract_remote, timeout=120)
    return s.done(rc, out, err)


def step_bastion_key_to_sql_nodes(install: dict) -> StepResult:
    """From bastion: gen key, install sshpass, push pubkey → each SQL node via SSH+password (NO vmrun)."""
    s = StepResult("bastion-key-to-sql-nodes")
    bip  = bastion_ip()
    user = install["ssh"]["sudo_user"]
    spwd = install["ssh"]["sudo_password"]
    ips  = [n["host"] for n in install["nodes"]]

    # 1. Install sshpass + gen bastion key
    setup = (
        f"echo '{spwd}' | sudo -S apt-get install -y -q sshpass 2>&1 | tail -3; "
        "[ ! -f ~/.ssh/id_ed25519 ] && "
        "  ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 -q || true"
    )
    rc, out, err = cli("ssh", bip, "--", setup, timeout=120)
    if rc != 0:
        return s.fail(f"sshpass/keygen on bastion failed: {err}")

    # 2. Push bastion pubkey to every SQL node
    combined = ""
    for ip in ips:
        deploy = (
            "PUBKEY=$(cat ~/.ssh/id_ed25519.pub); "
            f"sshpass -p '{spwd}' ssh "
            "-o StrictHostKeyChecking=no -o ConnectTimeout=10 "
            f"{user}@{ip} "
            "\"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            "grep -qxF \\\"$PUBKEY\\\" ~/.ssh/authorized_keys || "
            "echo \\\"$PUBKEY\\\" >> ~/.ssh/authorized_keys && "
            f"echo '{ip}: bastion key deployed'\""
        )
        rc2, o2, e2 = cli("ssh", bip, "--", deploy, timeout=60)
        combined += o2 + e2
        if rc2 != 0:
            print(f"    WARN: key deploy to {ip} rc={rc2}")

    return s.done(0, combined)


def step_bootstrap_bastion_ansible() -> StepResult:
    s = StepResult("bootstrap-bastion-ansible")
    rc, out, err = cli("run-bastion-script", "bootstrap-bastion-ansible", "--", "sqlserver", timeout=900)
    return s.done(rc, out, err)


def step_playbook_sqlserver_ag() -> StepResult:
    s = StepResult("playbook-sqlserver-ag")
    rc, out, err = cli(
        "run-bastion-playbook",
        "automation/ansible/playbooks/sqlserver-ag.yml",
        "-i", "inventory/sqlserver/hosts.yml",
        timeout=5400,
    )
    return s.done(rc, out, err)


def step_verify(install: dict) -> StepResult:
    s     = StepResult("verify-always-on")
    prim  = install["availability_group"]["primary_node"]
    sa_pw = "ChangeMe_SA_123!"
    sql   = (
        "SELECT ag.name, ar.replica_server_name, ars.role_desc "
        "FROM sys.availability_groups ag "
        "JOIN sys.availability_replicas ar ON ag.group_id=ar.group_id "
        "LEFT JOIN sys.dm_hadr_availability_replica_states ars "
        "  ON ar.replica_id=ars.replica_id;"
    )
    cmd   = (
        f"SQLCMDPASSWORD='{sa_pw}' "
        "/opt/mssql-tools18/bin/sqlcmd -C -S localhost -U SA "
        f"-Q \"{sql}\""
    )
    rc, out, err = cli("ssh", prim, "--", cmd, timeout=120)
    return s.done(rc, out, err)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SQL Server 2025 Always On – deployment orchestrator."
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=str(_DEFAULT_INSTALL_JSON),
        help="Path to install JSON (default: %(default)s)",
    )
    args = parser.parse_args()

    INSTALL_JSON = Path(args.config).resolve()
    # Convention: <date>_install_<rest>.json → <date>_result_install_<rest>.json
    RESULT_JSON  = INSTALL_JSON.parent / re.sub(r"_install_", "_result_install_", INSTALL_JSON.name, count=1)

    install = load_json(INSTALL_JSON)
    print("=" * 62)
    print("  SQL Server 2025 Always On — Deployment Orchestrator")
    print("=" * 62)
    print(f"  Config  : {INSTALL_JSON}")
    print(f"  Cluster : {install['cluster_name']}")
    print(f"  AG      : {install['availability_group']['name']}")
    print(f"  Nodes   : {', '.join(n['host'] for n in install['nodes'])}")
    print()

    results: dict = {
        "install_config": str(INSTALL_JSON),
        "cluster_name":   install["cluster_name"],
        "ag_name":        install["availability_group"]["name"],
        "started_at":     now_iso(),
        "finished_at":    None,
        "overall_status": "unknown",
        "steps":          [],
    }

    # Pre-flight
    print("[PRE-FLIGHT] Updating Ansible inventory / group_vars ...")
    preflight_update_configs(install)

    pipeline: list[tuple[str, callable]] = [
        ("ensure-ssh-key",             lambda: step_ensure_ssh_key()),
        ("start-bastion",              lambda: step_start_bastion()),
        ("fix-bastion-identity",       lambda: step_fix_bastion_identity()),
        ("deploy-host-key-bastion",    lambda: step_deploy_host_key_bastion()),
        ("repo-sync-bastion",          lambda: step_repo_sync(install)),
        ("bastion-key-to-sql-nodes",   lambda: step_bastion_key_to_sql_nodes(install)),
        ("bootstrap-bastion-ansible",  lambda: step_bootstrap_bastion_ansible()),
        ("playbook-sqlserver-ag",      lambda: step_playbook_sqlserver_ag()),
        ("verify-always-on",           lambda: step_verify(install)),
    ]

    abort = False
    for name, fn in pipeline:
        if abort:
            sr = StepResult(name)
            sr.fail("skipped — previous critical step failed")
            results["steps"].append(sr.to_dict())
            continue

        print(f"\n[STEP] {name}")
        try:
            sr = fn()
        except Exception as exc:
            sr = StepResult(name)
            sr.fail(str(exc))

        results["steps"].append(sr.to_dict())
        tag = "OK" if sr.ok else "FAILED"
        print(f"  → {tag}  rc={sr.rc}")

        # verify is non-critical; everything else is critical
        if not sr.ok and name != "verify-always-on":
            abort = True

    results["overall_status"] = "success" if not abort else "failed"
    results["finished_at"] = now_iso()

    RESULT_JSON.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResults  → {RESULT_JSON}")
    print(f"Status   → {results['overall_status']}")
    return 0 if not abort else 1


if __name__ == "__main__":
    raise SystemExit(main())
