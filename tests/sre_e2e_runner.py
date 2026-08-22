#!/usr/bin/env python3
"""SRE end-to-end provisioning test runner.

Runs the full provisioning → verify → teardown sequence for each database group
using a separate test config with isolated IPs and VM directory (D:/VMs_test).

Usage (from tools/db_ops/):
    python tests/sre_e2e_runner.py
    python tests/sre_e2e_runner.py --groups mysql postgresql
    python tests/sre_e2e_runner.py --groups oracle_rac --no-cleanup
    python tests/sre_e2e_runner.py --groups all --dry-run

Groups: shared  mysql  postgresql  sqlserver  oracle_rac  oracle_dg
Order : shared is provisioned first as a dependency for DB groups,
        and torn down last after all DB tests complete.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ── Paths ──────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]          # tools/db_ops/
_DEFAULT_CONFIG = _REPO_ROOT / "data" / "sre_test_config.json"
_CLI_MODULE = "db_ops.sre.cli"

# ── Timeouts ───────────────────────────────────────────────────────────────────

T_SHORT  = 300    # 5 min — clone, start, stop, delete
T_MEDIUM = 1800   # 30 min — fix-identity, deploy-key, bootstrap, simple playbooks
T_LONG   = 5400   # 90 min — oracle grid/db install

# ── ANSI colours ───────────────────────────────────────────────────────────────

_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

_USE_COLOR = sys.stdout.isatty() or os.environ.get("FORCE_COLOR")


_SEP_THIN  = "-" * 64
_SEP_THICK = "=" * 64


def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return "".join(codes) + text + _RESET


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class Step:
    name: str
    cli_args: list[str]
    critical: bool = True
    timeout: int = T_MEDIUM


@dataclass
class StepResult:
    name: str
    passed: bool
    returncode: int
    duration: float


@dataclass
class GroupResult:
    group: str
    steps: list[StepResult] = field(default_factory=list)
    aborted: bool = False

    @property
    def passed(self) -> bool:
        return not self.aborted and all(s.passed for s in self.steps)

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.steps if not s.passed)


# ── Step helpers ───────────────────────────────────────────────────────────────

def _ps(script: str, *args: str, name: str | None = None, timeout: int = T_MEDIUM, critical: bool = True) -> Step:
    cli = ["run-powershell", script]
    if args:
        cli += ["--"] + list(args)
    return Step(name=name or f"ps:{script}", cli_args=cli, timeout=timeout, critical=critical)


def _bs(script: str, *args: str, name: str | None = None, timeout: int = T_MEDIUM) -> Step:
    cli = ["run-bastion-script", script]
    if args:
        cli += ["--"] + list(args)
    return Step(name=name or f"bastion:{script}", cli_args=cli, timeout=timeout)


def _bp(playbook: str, name: str | None = None, timeout: int = T_MEDIUM, critical: bool = True, inventory: str | None = None) -> Step:
    stem = Path(playbook).stem
    cli = ["run-bastion-playbook", playbook]
    if inventory:
        cli += ["-i", inventory]
    return Step(
        name=name or f"playbook:{stem}",
        cli_args=cli,
        timeout=timeout,
        critical=critical,
    )


def _check(cmd: str, timeout: int = 120) -> Step:
    return Step(name=f"verify:{cmd}", cli_args=[cmd], timeout=timeout, critical=False)


def _stop(group: str) -> Step:
    return _ps("stop-vms", f"-Group", group, name=f"stop:{group}", timeout=T_SHORT, critical=False)


def _delete(group: str) -> Step:
    return _ps("delete-vms", f"-Group", group, name=f"delete:{group}", timeout=T_SHORT, critical=False)


# ── Step sequences per group ───────────────────────────────────────────────────

def _shared_steps() -> list[Step]:
    # Only bastion-01 is started; mon-01/log-01 are skipped to conserve host RAM.
    return [
        _ps("01-clone-vms",          "-VmName", "bastion-01",                           name="clone-vms",    timeout=T_SHORT),
        _ps("02-set-vm-resources",   "-VmName", "bastion-01", "-CpuCount", "2", "-MemoryGB", "2",
                                                                                         name="set-resources"),
        _ps("03-start-vms",          "-VmName", "bastion-01",                           name="start-vms",    timeout=T_SHORT),
        _ps("04-fix-guest-identity", "-VmName", "bastion-01",                           name="fix-identity"),
        _ps("08-deploy-host-ssh-key", "-VmName", "bastion-01",                          name="deploy-key"),
    ]


def _mysql_steps() -> list[Step]:
    g = "mysql"
    return [
        _ps("01-clone-vms",        "-Group", g,                         name="clone-vms",    timeout=T_SHORT),
        _ps("02-set-vm-resources", "-Group", g, "-CpuCount", "2", "-MemoryGB", "4",
                                                                         name="set-resources"),
        _ps("03-start-vms",        "-Group", g,                         name="start-vms",    timeout=T_SHORT),
        _ps("04-fix-guest-identity", "-Group", g,                       name="fix-identity"),
        _ps("07-bootstrap-bastion-ansible", "-TargetGroup", g,          name="bootstrap-bastion"),
        _bs("bootstrap-bastion-ansible", g,                             name="bootstrap-ansible"),
        _bp("automation/ansible/playbooks/mysql-cluster.yml"),
        _check("check-mysql-cluster"),
    ]


def _postgresql_steps() -> list[Step]:
    g = "postgresql"
    return [
        _ps("01-clone-vms",        "-Group", g,                         name="clone-vms",    timeout=T_SHORT),
        _ps("02-set-vm-resources", "-Group", g, "-CpuCount", "2", "-MemoryGB", "4",
                                                                         name="set-resources"),
        _ps("03-start-vms",        "-Group", g,                         name="start-vms",    timeout=T_SHORT),
        _ps("04-fix-guest-identity", "-Group", g,                       name="fix-identity"),
        _ps("07-bootstrap-bastion-ansible", "-TargetGroup", g,          name="bootstrap-bastion"),
        _bs("bootstrap-bastion-ansible", g,                             name="bootstrap-ansible"),
        _bp("automation/ansible/playbooks/postgresql-ha.yml", inventory="inventory/postgresql/hosts.yml"),
        _check("check-postgresql-ha"),
    ]


def _sqlserver_steps() -> list[Step]:
    g = "sqlserver"
    return [
        _ps("01-clone-vms",        "-Group", g,                         name="clone-vms",    timeout=T_SHORT),
        _ps("02-set-vm-resources", "-Group", g, "-CpuCount", "4", "-MemoryGB", "8",
                                                                         name="set-resources"),
        _ps("03-start-vms",        "-Group", g,                         name="start-vms",    timeout=T_SHORT),
        _ps("04-fix-guest-identity", "-Group", g,                       name="fix-identity"),
        _ps("07-bootstrap-bastion-ansible", "-TargetGroup", g,          name="bootstrap-bastion"),
        _bs("bootstrap-bastion-ansible", g,                             name="bootstrap-ansible"),
        _bp("automation/ansible/playbooks/sqlserver-ag.yml",            timeout=T_LONG, inventory="inventory/sqlserver/hosts.yml"),
    ]


def _oracle_rac_pre_steps() -> list[Step]:
    g = "oracle_rac"
    return [
        _ps("01-clone-vms", "-Group", g, "-Template", "tpl-oraclelinux-r9u6",
                                                                         name="clone-vms",    timeout=T_SHORT),
        _ps("02-set-vm-resources", "-Group", g, "-CpuCount", "4", "-MemoryGB", "12",
                                                                         name="set-resources"),
        # NOTE: shared disks NOT added here — VMs start bare so SSH works for fix-identity.
        # Shared disks are added in post_steps after identity is fixed and kdump disabled.
        _ps("03-start-vms", "-Group", g,                                name="start-vms",    timeout=T_SHORT),
    ]


def _oracle_rac_post_steps(ip_map_json: str) -> list[Step]:
    g = "oracle_rac"
    return [
        # Phase A: fix identity while VMs have no shared disks (SSH works cleanly).
        # -DisableKdumpMultipath disables kdump + multipathd before the reboot so that
        # the subsequent boot with shared SCSI disks does not kernel-panic.
        _ps("04-fix-guest-identity", "-Group", g, "-CurrentIpMapJson", ip_map_json,
                                                   "-DisableKdumpMultipath",
                                                                         name="fix-identity",  timeout=2400),
        # Phase B: stop VMs, attach shared disks, restart.
        _ps("stop-vms",                "-Group", g,                      name="stop-pre-disk", timeout=T_SHORT, critical=False),
        _ps("08-add-oracle-shared-disks",                                name="add-shared-disks"),
        _ps("03-start-vms",            "-Group", g,                      name="start-vms-2",   timeout=T_SHORT),
        # Phase C: remaining deployment (SSH via static IPs, kdump now disabled).
        _ps("08-deploy-host-ssh-key", "-Group", g,                      name="deploy-key"),
        _ps("07-bootstrap-bastion-ansible", "-TargetGroup", g,          name="bootstrap-bastion"),
        _bs("bootstrap-bastion-ansible", g,                             name="bootstrap-ansible"),
        _bp("automation/ansible/playbooks/oracle-rac-os-prep.yml",       inventory="inventory/oracle/rac/hosts.yml"),
        _ps("09-stage-oracle-installer", "-Group", g,                   name="stage-installer", timeout=T_LONG),
        _bp("automation/ansible/playbooks/oracle-rac-grid.yml",         timeout=T_LONG, inventory="inventory/oracle/rac/hosts.yml"),
        _bp("automation/ansible/playbooks/oracle-rac-db.yml",           timeout=T_LONG, inventory="inventory/oracle/rac/hosts.yml"),
    ]


def _oracle_dg_pre_steps() -> list[Step]:
    g = "oracle_dg"
    return [
        _ps("01-clone-vms", "-Group", g, "-Template", "tpl-oraclelinux-r9u6",
                                                                         name="clone-vms",    timeout=T_SHORT),
        _ps("02-set-vm-resources", "-Group", g, "-CpuCount", "4", "-MemoryGB", "16",
                                                                         name="set-resources"),
        _ps("03-start-vms", "-Group", g,                                name="start-vms",    timeout=T_SHORT),
    ]


def _oracle_dg_post_steps(ip_map_json: str) -> list[Step]:
    g = "oracle_dg"
    return [
        _ps("04-fix-guest-identity", "-Group", g, "-CurrentIpMapJson", ip_map_json,
                                                                         name="fix-identity"),
        _ps("08-deploy-host-ssh-key", "-Group", g,                      name="deploy-key"),
        _ps("07-bootstrap-bastion-ansible", "-TargetGroup", g,          name="bootstrap-bastion"),
        _bs("bootstrap-bastion-ansible", g,                             name="bootstrap-ansible"),
        _bp("automation/ansible/playbooks/oracle-dg-os-prep.yml",        inventory="inventory/oracle/dataguard/hosts.yml"),
        _ps("09-stage-oracle-installer", "-Group", g,                   name="stage-installer", timeout=T_LONG),
        _bp("automation/ansible/playbooks/oracle-dg-primary.yml",       timeout=T_LONG, inventory="inventory/oracle/dataguard/hosts.yml"),
        _bp("automation/ansible/playbooks/oracle-dg-standby.yml",       timeout=T_LONG, inventory="inventory/oracle/dataguard/hosts.yml"),
    ]


# ── Runner ─────────────────────────────────────────────────────────────────────

class SreE2eRunner:
    def __init__(self, config_path: Path, *, no_cleanup: bool = False, dry_run: bool = False):
        self.config_path = config_path.resolve()
        self.no_cleanup = no_cleanup
        self.dry_run = dry_run
        self._cfg = json.loads(config_path.read_text(encoding="utf-8"))
        self._sre = self._cfg["sre"]

    # -- config helpers --------------------------------------------------------

    def _vm_root(self) -> Path:
        raw = self._sre["vmware"]["vm_root"].replace("/", os.sep)
        return Path(raw)

    def _inventory(self, group: str) -> list[dict]:
        return self._sre["inventory"]["groups"].get(group, [])

    # -- step execution --------------------------------------------------------

    def run_step(self, step: Step) -> StepResult:
        print(f"\n{_c(_SEP_THIN, _CYAN)}")
        print(_c(f"  {step.name}", _BOLD))
        print(_c(_SEP_THIN, _CYAN))

        cmd = [sys.executable, "-m", _CLI_MODULE, "--config", str(self.config_path)] + step.cli_args
        print(f"  $ {' '.join(cmd)}\n")

        if self.dry_run:
            print(_c("  [DRY-RUN] skipped", _YELLOW))
            return StepResult(name=step.name, passed=True, returncode=0, duration=0.0)

        t0 = time.monotonic()
        proc = subprocess.Popen(cmd)
        try:
            proc.wait(timeout=step.timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            print(_c(f"\n  [TIMEOUT] Step timed out after {step.timeout}s", _RED))
            rc = -1
        elapsed = time.monotonic() - t0

        passed = rc == 0
        label = _c("PASS", _GREEN, _BOLD) if passed else _c("FAIL", _RED, _BOLD)
        print(f"\n  [{label}] {step.name}  rc={rc}  {elapsed:.1f}s")
        return StepResult(name=step.name, passed=passed, returncode=rc, duration=elapsed)

    def _run_steps(self, steps: list[Step], result: GroupResult) -> bool:
        """Run a list of steps, return False if a critical step failed."""
        for step in steps:
            sr = self.run_step(step)
            result.steps.append(sr)
            if not sr.passed and step.critical:
                print(_c(f"\n  [ABORT] Critical step '{step.name}' failed.", _RED))
                return False
        return True

    def _cleanup(self, group: str, result: GroupResult, *, delete_shared_disks: bool = False) -> None:
        if self.no_cleanup:
            print(_c(f"\n  [SKIP CLEANUP] --no-cleanup active for {group}", _YELLOW))
            return
        print(f"\n{_SEP_THICK}")
        print(_c(f"  CLEANUP  {group}", _BOLD))
        for step in [_stop(group), _delete(group)]:
            sr = self.run_step(step)
            result.steps.append(sr)
        if delete_shared_disks:
            shared_dir = self._vm_root() / "oracle_rac_shared"
            if shared_dir.exists():
                print(f"\n  Removing shared disk directory: {shared_dir}")
                if not self.dry_run:
                    shutil.rmtree(shared_dir, ignore_errors=True)
                    print("  Done.")

    # -- IP discovery for Oracle VMs (no VMware Tools) -------------------------

    def _discover_ips(self, group: str) -> dict[str, str]:
        """Match VMX MAC addresses to current ARP entries → {vm_name: ip}."""
        nodes = self._inventory(group)
        vm_root = self._vm_root()

        # Read MAC addresses from VMX files
        mac_to_name: dict[str, str] = {}
        for node in nodes:
            name = node["name"]
            vmx = vm_root / group / name / f"{name}.vmx"
            if vmx.exists():
                text = vmx.read_text(errors="ignore")
                m = re.search(r'ethernet0\.generatedAddress\s*=\s*"([^"]+)"', text, re.I)
                if m:
                    mac_to_name[m.group(1).lower()] = name

        if not mac_to_name:
            print(_c("  [WARN] No MACs found in VMX files for IP discovery.", _YELLOW))
            return {}

        # Ping sweep to refresh ARP cache
        print(f"  Pinging subnet to refresh ARP (for {group}) ...")
        subnet_base = "198.51.100."
        for i in range(100, 200):
            subprocess.run(
                ["ping", "-n", "1", "-w", "100", f"{subnet_base}{i}"],
                capture_output=True, timeout=2
            )

        # Parse ARP table
        arp = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10)
        ip_map: dict[str, str] = {}
        for line in arp.stdout.splitlines():
            m = re.search(
                r'(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2}'
                r'[-:][0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2})',
                line, re.I,
            )
            if m:
                ip  = m.group(1)
                mac = m.group(2).replace("-", ":").lower()
                if mac in mac_to_name:
                    ip_map[mac_to_name[mac]] = ip

        return ip_map

    def _wait_for_ips(self, group: str, expected: int, timeout: int = 180) -> dict[str, str]:
        """Poll until all expected VMs have discoverable IPs."""
        deadline = time.monotonic() + timeout
        print(f"  Waiting for {expected} VMs in '{group}' to get DHCP IPs (up to {timeout}s) ...")
        while time.monotonic() < deadline:
            ip_map = self._discover_ips(group)
            if len(ip_map) >= expected:
                for name, ip in ip_map.items():
                    print(f"    {name} -> {ip}")
                return ip_map
            print(f"  Found {len(ip_map)}/{expected} IPs, retrying in 15s ...")
            time.sleep(15)
        # Return whatever found
        ip_map = self._discover_ips(group)
        for name, ip in ip_map.items():
            print(f"    {name} -> {ip}")
        return ip_map

    # -- Group runners ---------------------------------------------------------

    def run_shared(self) -> GroupResult:
        result = GroupResult(group="shared")
        ok = self._run_steps(_shared_steps(), result)
        if not ok:
            result.aborted = True
        return result

    def _run_ubuntu_group(self, group: str, steps: list[Step]) -> GroupResult:
        result = GroupResult(group=group)
        header = f"{_SEP_THICK}\n  GROUP: {group.upper()}\n{_SEP_THICK}"
        print(_c(f"\n{header}", _BOLD))
        ok = self._run_steps(steps, result)
        if not ok:
            result.aborted = True
        self._cleanup(group, result)
        return result

    def run_mysql(self) -> GroupResult:
        return self._run_ubuntu_group("mysql", _mysql_steps())

    def run_postgresql(self) -> GroupResult:
        return self._run_ubuntu_group("postgresql", _postgresql_steps())

    def run_sqlserver(self) -> GroupResult:
        return self._run_ubuntu_group("sqlserver", _sqlserver_steps())

    def _run_oracle_group(
        self,
        group: str,
        pre_steps: list[Step],
        post_steps_factory: Callable[[str], list[Step]],
        *,
        delete_shared_disks: bool = False,
    ) -> GroupResult:
        result = GroupResult(group=group)
        nodes = self._inventory(group)
        header = f"{_SEP_THICK}\n  GROUP: {group.upper()}\n{_SEP_THICK}"
        print(_c(f"\n{header}", _BOLD))

        # Phase 1: clone → [shared-disks] → start
        if not self._run_steps(pre_steps, result):
            result.aborted = True
            self._cleanup(group, result, delete_shared_disks=delete_shared_disks)
            return result

        # IP discovery (VMs have DHCP IPs — no VMware Tools)
        if self.dry_run:
            ip_map = {node["name"]: node["ip"] for node in nodes}
            print(_c(f"  [DRY-RUN] Using config IPs: {ip_map}", _YELLOW))
        else:
            ip_map = self._wait_for_ips(group, expected=len(nodes))
            if not ip_map:
                print(_c(f"  [ERROR] Could not discover IPs for {group}. Aborting.", _RED))
                result.aborted = True
                self._cleanup(group, result, delete_shared_disks=delete_shared_disks)
                return result

        ip_map_json = json.dumps(ip_map)
        print(f"  IP map: {ip_map_json}")

        # Phase 2: fix-identity (with discovered IPs) → rest
        if not self._run_steps(post_steps_factory(ip_map_json), result):
            result.aborted = True

        self._cleanup(group, result, delete_shared_disks=delete_shared_disks)
        return result

    def run_oracle_rac(self) -> GroupResult:
        return self._run_oracle_group(
            "oracle_rac",
            _oracle_rac_pre_steps(),
            _oracle_rac_post_steps,
            delete_shared_disks=True,
        )

    def run_oracle_dg(self) -> GroupResult:
        return self._run_oracle_group(
            "oracle_dg",
            _oracle_dg_pre_steps(),
            _oracle_dg_post_steps,
        )

    # -- Shared teardown -------------------------------------------------------

    def teardown_shared(self) -> None:
        result = GroupResult(group="shared")
        self._cleanup("shared", result)

    # -- Main orchestration ----------------------------------------------------

    def run(self, groups: list[str]) -> bool:
        db_groups = [g for g in groups if g != "shared"]
        run_shared = bool(db_groups) or "shared" in groups

        results: list[GroupResult] = []

        # Shared VMs are required for bastion-based ansible steps
        shared_result: GroupResult | None = None
        if run_shared or db_groups:
            header = f"{_SEP_THICK}\n  GROUP: SHARED (bastion prerequisite)\n{_SEP_THICK}"
            print(_c(f"\n{header}", _BOLD))
            shared_result = self.run_shared()
            results.append(shared_result)
            if not shared_result.passed and not self.dry_run:
                print(_c("\n  Shared VM setup failed — DB group tests skipped.", _RED))
                self._print_summary(results)
                return False

        dispatch: dict[str, Callable[[], GroupResult]] = {
            "mysql":       self.run_mysql,
            "postgresql":  self.run_postgresql,
            "sqlserver":   self.run_sqlserver,
            "oracle_rac":  self.run_oracle_rac,
            "oracle_dg":   self.run_oracle_dg,
        }

        for g in db_groups:
            fn = dispatch.get(g)
            if fn is None:
                print(_c(f"\n[WARN] Unknown group: {g}", _YELLOW))
                continue
            results.append(fn())

        # Shared teardown (last)
        if shared_result and not self.no_cleanup and not self.dry_run:
            self.teardown_shared()
            # Append cleanup steps to shared_result for reporting
            # (already done inside teardown_shared → _cleanup)

        self._print_summary(results)
        return all(r.passed for r in results)

    # -- Summary ---------------------------------------------------------------

    def _print_summary(self, results: list[GroupResult]) -> None:
        print(_c(f"\n\n{_SEP_THICK}", _BOLD))
        print(_c("  E2E TEST SUMMARY", _BOLD))
        print(_c(_SEP_THICK, _BOLD))

        all_passed = True
        for r in results:
            status = _c("PASS", _GREEN, _BOLD) if r.passed else _c("FAIL", _RED, _BOLD)
            if not r.passed:
                all_passed = False
            total_time = sum(s.duration for s in r.steps)
            print(f"\n  [{status}] {r.group.upper():12s}  {r.total} steps  {total_time:.0f}s")
            for s in r.steps:
                icon = _c("  OK", _GREEN) if s.passed else _c("  XX", _RED)
                print(f"{icon}  {s.name:<40s} {s.duration:6.1f}s  rc={s.returncode}")

        print(f"\n{_c(_SEP_THICK, _BOLD)}")
        overall = _c("ALL PASSED", _GREEN, _BOLD) if all_passed else _c("SOME FAILED", _RED, _BOLD)
        print(f"  {overall}")
        print(_c(_SEP_THICK, _BOLD))


# ── CLI entry point ────────────────────────────────────────────────────────────

_ALL_GROUPS = ["shared", "mysql", "postgresql", "sqlserver", "oracle_rac", "oracle_dg"]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SRE end-to-end provisioning test runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--groups",
        nargs="+",
        metavar="GROUP",
        choices=_ALL_GROUPS + ["all"],
        default=["all"],
        help="Groups to test. 'all' runs every group. Default: all.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"Path to SRE test config JSON. Default: {_DEFAULT_CONFIG}",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip stop/delete after test (useful for debugging failures).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not args.config.exists():
        print(_c(f"ERROR: config not found: {args.config}", _RED), file=sys.stderr)
        return 1

    requested = _ALL_GROUPS if "all" in args.groups else args.groups

    runner = SreE2eRunner(
        config_path=args.config,
        no_cleanup=args.no_cleanup,
        dry_run=args.dry_run,
    )

    print(_c(f"\nSRE E2E Runner", _BOLD, _CYAN))
    print(f"  Config  : {args.config}")
    print(f"  Groups  : {', '.join(requested)}")
    print(f"  Cleanup : {'disabled' if args.no_cleanup else 'enabled'}")
    print(f"  Dry-run : {args.dry_run}")

    ok = runner.run(requested)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
