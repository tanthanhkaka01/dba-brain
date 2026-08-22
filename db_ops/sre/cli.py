from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from db_ops.config import load_config, resolve_config_path
from db_ops.logging_ops import (
    LOG_SCOPE_ENV_VAR,
    build_log_paths,
    log_event,
    log_function_call,
    log_function_error,
    setup_app_logger,
)
from db_ops.logging_ops.runtime_stdout import patch_stdout
from db_ops.sre.automation import list_known_workflows
from db_ops.sre.config import SreOperationalConfig, load_sre_operational_config
from db_ops.sre.docker_db import (
    ENGINE_META,
    VALID_ENGINES,
    VALID_MODES,
    DockerDbSpec,
    ProvisionError,
    provision,
)
from db_ops.sre.docker_db import register_config as docker_register
from db_ops.sre.docker_db.models import DEFAULT_BACKUP_MOUNT
from db_ops.sre.docker_db.provisioner import DEFAULT_CONTAINERS_DIR
from db_ops.sre.docker_db.mover import DEFAULT_STAGE_DIR
from db_ops.sre.inventory import list_inventory_assets
from db_ops.sre.service import (
    check_mysql_cluster,
    check_postgresql_ha,
    check_shared_vms,
    list_vmware_commands,
    run_bastion_ansible,
    run_bastion_ansible_playbook,
    run_bastion_script,
    run_powershell_script,
    run_ssh_command,
)

_REQUIRES_SRE_CONFIG = frozenset({
    "inventory-assets",
    "check-shared-vms",
    "check-mysql-cluster",
    "check-postgresql-ha",
    "run-powershell",
    "run-bastion-script",
    "run-bastion-playbook",
    "run-bastion-ansible",
    "ssh",
    "setup-sequence",
})

# Each entry is a list of argv tokens passed to the child `db_ops.sre.cli` process
# (everything after `--config <path>`). The subcommand is always first.
_SEQUENCE_STEPS: dict[str, list[list[str]]] = {
    "shared": [
        ["run-powershell", "01-clone-vms", "--", "-Group", "shared"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "shared", "-CpuCount", "2", "-MemoryGB", "2"],
        ["run-powershell", "03-start-vms", "--", "-Group", "shared"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "shared"],
        ["run-powershell", "08-deploy-host-ssh-key", "--", "-Group", "shared"],
    ],
    "mysql": [
        ["run-powershell", "01-clone-vms", "--", "-Group", "shared"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "shared", "-CpuCount", "2", "-MemoryGB", "2"],
        ["run-powershell", "03-start-vms", "--", "-Group", "shared"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "shared"],
        ["run-powershell", "stop-vms", "--", "-VmName", "mon-01"],
        ["run-powershell", "stop-vms", "--", "-VmName", "log-01"],
        ["run-powershell", "01-clone-vms", "--", "-Group", "mysql"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "mysql", "-CpuCount", "2", "-MemoryGB", "4"],
        ["run-powershell", "03-start-vms", "--", "-Group", "mysql"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "mysql"],
        ["run-powershell", "07-bootstrap-bastion-ansible", "--", "-TargetGroup", "mysql"],
        # Windows host key must be deployed before run-bastion-script needs SSH access to bastion.
        # Only bastion-01 is running at this point (mon-01/log-01 were stopped in steps 5-6).
        ["run-powershell", "08-deploy-host-ssh-key", "--", "-VmName", "bastion-01"],
        # Install Ansible on bastion and configure SSH/NOPASSWD sudo on DB nodes.
        ["run-bastion-script", "bootstrap-bastion-ansible", "--", "mysql"],
        ["run-bastion-playbook", "automation/ansible/playbooks/mysql-cluster.yml", "-i", "inventory/mysql/hosts.yml"],
    ],
    "postgresql": [
        ["run-powershell", "01-clone-vms", "--", "-Group", "shared"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "shared", "-CpuCount", "2", "-MemoryGB", "2"],
        ["run-powershell", "03-start-vms", "--", "-Group", "shared"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "shared"],
        ["run-powershell", "stop-vms", "--", "-VmName", "mon-01"],
        ["run-powershell", "stop-vms", "--", "-VmName", "log-01"],
        ["run-powershell", "01-clone-vms", "--", "-Group", "postgresql"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "postgresql", "-CpuCount", "2", "-MemoryGB", "4"],
        ["run-powershell", "03-start-vms", "--", "-Group", "postgresql"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "postgresql"],
        ["run-powershell", "07-bootstrap-bastion-ansible", "--", "-TargetGroup", "postgresql"],
        ["run-powershell", "08-deploy-host-ssh-key", "--", "-VmName", "bastion-01"],
        ["run-bastion-script", "bootstrap-bastion-ansible", "--", "postgresql"],
        ["run-bastion-playbook", "automation/ansible/playbooks/postgresql-ha.yml", "-i", "inventory/postgresql/hosts.yml"],
    ],
    "mssql": [
        ["run-powershell", "01-clone-vms", "--", "-Group", "shared"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "shared", "-CpuCount", "2", "-MemoryGB", "2"],
        ["run-powershell", "03-start-vms", "--", "-Group", "shared"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "shared"],
        ["run-powershell", "stop-vms", "--", "-VmName", "mon-01"],
        ["run-powershell", "stop-vms", "--", "-VmName", "log-01"],
        ["run-powershell", "01-clone-vms", "--", "-Group", "sqlserver"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "sqlserver", "-CpuCount", "2", "-MemoryGB", "4"],
        ["run-powershell", "03-start-vms", "--", "-Group", "sqlserver"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "sqlserver"],
        ["run-powershell", "07-bootstrap-bastion-ansible", "--", "-TargetGroup", "sqlserver"],
        ["run-powershell", "08-deploy-host-ssh-key", "--", "-VmName", "bastion-01"],
        ["run-bastion-script", "bootstrap-bastion-ansible", "--", "sqlserver"],
        ["run-bastion-playbook", "automation/ansible/playbooks/sqlserver-ag.yml", "-i", "inventory/sqlserver/hosts.yml"],
    ],
    "oracle-rac": [
        ["run-powershell", "01-clone-vms", "--", "-Group", "shared"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "shared", "-CpuCount", "2", "-MemoryGB", "2"],
        ["run-powershell", "03-start-vms", "--", "-Group", "shared"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "shared"],
        ["run-powershell", "stop-vms", "--", "-VmName", "mon-01"],
        ["run-powershell", "stop-vms", "--", "-VmName", "log-01"],
        # Bootstrap the Oracle Linux template: enables sshd + vmtoolsd, refreshes base-os snapshot.
        # Must run before cloning so the cloned VMs inherit SSH and VMware Tools.
        ["run-powershell", "00-bootstrap-template-remote-access", "--", "-Template", "oraclelinux"],
        # Stop any existing oracle_rac VMs before -Force re-clone (safe if none exist).
        ["run-powershell", "stop-vms", "--", "-Group", "oracle_rac"],
        # -Force removes old VM dirs so clones always come from the freshly bootstrapped snapshot.
        ["run-powershell", "01-clone-vms", "--", "-Group", "oracle_rac", "-Force"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "oracle_rac", "-CpuCount", "4", "-MemoryGB", "12"],
        # Shared disks must be attached BEFORE VMs are powered on.
        ["run-powershell", "08-add-oracle-shared-disks"],
        ["run-powershell", "03-start-vms", "--", "-Group", "oracle_rac"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "oracle_rac"],
        ["run-powershell", "07-bootstrap-bastion-ansible", "--", "-TargetGroup", "oracle_rac"],
        ["run-powershell", "08-deploy-host-ssh-key", "--", "-VmName", "bastion-01"],
        ["run-bastion-script", "bootstrap-bastion-ansible", "--", "oracle_rac"],
        # oracle-rac-os-prep creates the grid user and oinstall group that
        # 09-stage-oracle-installer needs for chown. Run os-prep first.
        ["run-bastion-playbook", "automation/ansible/playbooks/oracle-rac-os-prep.yml", "-i", "inventory/oracle/rac/hosts.yml"],
        ["run-powershell", "09-stage-oracle-installer", "--", "-Group", "oracle_rac"],
        ["run-bastion-playbook", "automation/ansible/playbooks/oracle-rac-grid.yml", "-i", "inventory/oracle/rac/hosts.yml"],
        ["run-bastion-playbook", "automation/ansible/playbooks/oracle-rac-db.yml", "-i", "inventory/oracle/rac/hosts.yml"],
    ],
    "oracle-dg": [
        ["run-powershell", "01-clone-vms", "--", "-Group", "shared"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "shared", "-CpuCount", "2", "-MemoryGB", "2"],
        ["run-powershell", "03-start-vms", "--", "-Group", "shared"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "shared"],
        ["run-powershell", "stop-vms", "--", "-VmName", "mon-01"],
        ["run-powershell", "stop-vms", "--", "-VmName", "log-01"],
        # Bootstrap the Oracle Linux template: enables sshd + vmtoolsd, refreshes base-os snapshot.
        ["run-powershell", "00-bootstrap-template-remote-access", "--", "-Template", "oraclelinux"],
        # Stop any existing oracle_dg VMs before -Force re-clone (safe if none exist).
        ["run-powershell", "stop-vms", "--", "-Group", "oracle_dg"],
        # -Force removes old VM dirs so clones always come from the freshly bootstrapped snapshot.
        ["run-powershell", "01-clone-vms", "--", "-Group", "oracle_dg", "-Force"],
        ["run-powershell", "02-set-vm-resources", "--", "-Group", "oracle_dg", "-CpuCount", "2", "-MemoryGB", "8"],
        ["run-powershell", "03-start-vms", "--", "-Group", "oracle_dg"],
        ["run-powershell", "04-fix-guest-identity", "--", "-Group", "oracle_dg"],
        ["run-powershell", "07-bootstrap-bastion-ansible", "--", "-TargetGroup", "oracle_dg"],
        ["run-powershell", "08-deploy-host-ssh-key", "--", "-VmName", "bastion-01"],
        ["run-bastion-script", "bootstrap-bastion-ansible", "--", "oracle_dg"],
        # oracle-dg-os-prep creates the oracle user and oinstall group that
        # 09-stage-oracle-installer needs for chown. Run os-prep first.
        ["run-bastion-playbook", "automation/ansible/playbooks/oracle-dg-os-prep.yml", "-i", "inventory/oracle/dataguard/hosts.yml"],
        ["run-powershell", "09-stage-oracle-installer", "--", "-Group", "oracle_dg"],
        ["run-bastion-playbook", "automation/ansible/playbooks/oracle-dg-primary.yml", "-i", "inventory/oracle/dataguard/hosts.yml"],
        ["run-bastion-playbook", "automation/ansible/playbooks/oracle-dg-standby.yml", "-i", "inventory/oracle/dataguard/hosts.yml"],
    ],
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DB Ops SRE CLI.")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to DB Ops config JSON. Defaults to config.sre.json or config.json.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Timeout in seconds for remote commands.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list-workflows", help="List known db_ops.sre workflows.").set_defaults(
        handler=_handle_list_workflows
    )
    subparsers.add_parser("inventory-assets", help="List inventory nodes from SRE config.").set_defaults(
        handler=_handle_inventory_assets
    )
    subparsers.add_parser("vmware-list", help="List available VMware wrapper commands.").set_defaults(
        handler=_handle_vmware_list
    )

    for cmd, description in [
        ("check-shared-vms", "Validate shared VM identity and system state."),
        ("check-mysql-cluster", "Validate MySQL service and InnoDB Cluster topology."),
        ("check-postgresql-ha", "Validate PostgreSQL service and streaming replication."),
    ]:
        p = subparsers.add_parser(cmd, help=description)
        p.add_argument("--dry-run", action="store_true", help="Print resolved commands without executing.")
        if cmd == "check-shared-vms":
            p.add_argument(
                "--vm-name",
                nargs="+",
                metavar="NAME",
                help="Limit check to one or more VM names (e.g. bastion-01 mon-01).",
            )
        p.set_defaults(handler=_handle_check, check_command=cmd)

    ps_parser = subparsers.add_parser(
        "run-powershell",
        help="Run a PowerShell script from sre.automation.powershell_dir.",
    )
    ps_parser.add_argument("script", help="Script name, with or without .ps1.")
    ps_parser.add_argument("script_args", nargs=argparse.REMAINDER, help="Arguments passed to the script.")
    ps_parser.add_argument("--dry-run", action="store_true")
    ps_parser.set_defaults(handler=_handle_run_powershell)

    bs_parser = subparsers.add_parser(
        "run-bastion-script",
        help="Run a Bash script on bastion-01 through SSH.",
    )
    bs_parser.add_argument("script", help="Script name under sre.automation.bash_dir.")
    bs_parser.add_argument("script_args", nargs=argparse.REMAINDER)
    bs_parser.add_argument("--dry-run", action="store_true")
    bs_parser.set_defaults(handler=_handle_run_bastion_script)

    bp_parser = subparsers.add_parser(
        "run-bastion-playbook",
        help="Run ansible-playbook on bastion-01 through SSH.",
    )
    bp_parser.add_argument("playbook", help="Playbook path relative to the synced repo root.")
    bp_parser.add_argument("--inventory", "-i", help="Inventory path relative to repo root.")
    bp_parser.add_argument("playbook_args", nargs=argparse.REMAINDER)
    bp_parser.add_argument("--dry-run", action="store_true")
    bp_parser.set_defaults(handler=_handle_run_bastion_playbook)

    ba_parser = subparsers.add_parser(
        "run-bastion-ansible",
        help="Run an Ansible ad hoc command on bastion-01 through SSH.",
    )
    ba_parser.add_argument("target", help="Ansible target pattern.")
    ba_parser.add_argument("--inventory", "-i", help="Inventory path relative to repo root.")
    ba_parser.add_argument("ansible_args", nargs=argparse.REMAINDER)
    ba_parser.add_argument("--dry-run", action="store_true")
    ba_parser.set_defaults(handler=_handle_run_bastion_ansible)

    ssh_parser = subparsers.add_parser(
        "ssh",
        help="Run a command through SSH using the configured guest user.",
    )
    ssh_parser.add_argument("host", help="Target host name or IP.")
    ssh_parser.add_argument("command_args", nargs=argparse.REMAINDER)
    ssh_parser.add_argument("--dry-run", action="store_true")
    ssh_parser.set_defaults(handler=_handle_ssh)

    ss_parser = subparsers.add_parser(
        "setup-sequence",
        help="Run a complete cluster setup sequence by invoking individual SRE sub-commands.",
    )
    ss_parser.add_argument(
        "db_type",
        choices=list(_SEQUENCE_STEPS),
        help="Database type to set up.",
    )
    ss_parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run to every step.")
    ss_parser.add_argument(
        "--from-step",
        type=int,
        default=1,
        metavar="N",
        help="Resume from step N (1-based). Earlier steps are skipped.",
    )
    ss_parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue to the next step even if a step exits non-zero.",
    )
    ss_parser.set_defaults(handler=_handle_setup_sequence)

    cdd = subparsers.add_parser(
        "create-db-docker",
        help="Create a lab database Docker instance (postgres/mysql/mssql/oracle) and register its "
             "connection — locally, or directly on a remote Ubuntu host via --remote-host/--remote-user.",
    )
    cdd.add_argument("--name", required=True, help="Instance name (letters, numbers, underscore, dash).")
    cdd.add_argument("--engine", required=True, choices=list(VALID_ENGINES))
    cdd.add_argument("--version", required=True, help="Image version/tag (e.g. 16, 8.4, 2022-latest).")
    cdd.add_argument("--mode", default="single", choices=list(VALID_MODES))
    cdd.add_argument("--replicas", type=int, default=None,
                     help="Standby count for ha-lab mode (default 2). Invalid with --mode single.")
    cdd.add_argument("--host-port", dest="host_port", type=int, default=None,
                     help="Host port to publish the (primary) instance on. Default: the engine's "
                          "own port (postgres 5432, mysql 3306, mssql 1433) — pick another when it "
                          "is already taken on the worker.")
    cdd.add_argument("--password-env", dest="password_env", default=None,
                     help="Env var / secret ref holding the DB password (never hardcoded). "
                          "Default: <NAME>_PASSWORD, so each instance has its own ref.")
    cdd.add_argument("--password-text", dest="password_text", default=None,
                     help="The password value itself. Stored under --password-env in the encrypted "
                          "secret store, then used to provision. Convenient by hand; note it is "
                          "visible in the process list while the command runs.")
    cdd.add_argument("--password-text-env", dest="password_text_env", default=None,
                     help="Name of an environment variable holding the password value instead. "
                          "This is how the Telegram command passes it: nothing sensitive is "
                          "rendered into argv.")
    cdd.add_argument("--overwrite-secret", dest="overwrite_secret", action="store_true",
                     help="Allow --password-text-env to replace an existing value for that ref.")
    cdd.add_argument("--containers-dir", default=DEFAULT_CONTAINERS_DIR,
                     help=f"Base dir for instance folders (default {DEFAULT_CONTAINERS_DIR}).")
    cdd.add_argument("--backup-mount", dest="backup_mount", default=None,
                     help="Host directory bind-mounted into the container at the SAME path, so a "
                          "restore can hand the engine a path the workflow wrote to over SSH. "
                          f"SQL Server defaults to {DEFAULT_BACKUP_MOUNT}; other engines have none. "
                          "Pass an empty string (or '-') to create no mount.")
    cdd.add_argument("--network-subnet", dest="network_subnet", default="",
                     help="CIDR for this instance's compose network (e.g. 172.30.42.0/24). Default: a "
                          "/24 derived from --name inside 172.30.0.0/16. Never left to Docker's own "
                          "address pool, which spans 172.17-172.31 and has twice taken a production "
                          "database off the map by claiming a range the estate routes.")
    cdd.add_argument("--worker-host", default="", help="DB host/IP to record in the connection entry.")
    cdd.add_argument("--data-dir", default=None, help="Override the data dir for the connection registry.")
    cdd.add_argument("--registry", default=None, help="Override the connection registry file path.")
    cdd.add_argument("--no-register", dest="register", action="store_false",
                     help="Create the instance but do not register the connection.")
    cdd.add_argument("--key", default=None, help="Secret passphrase (resolves the password from the secret store).")
    cdd.add_argument("--key-base64", dest="key_base64", default=None, help="Base64 secret passphrase.")
    cdd.add_argument("--health-timeout", dest="health_timeout", type=int, default=None,
                     help="Seconds to wait for every node to become healthy. Default: the "
                          "engine's own ceiling (oracle needs far longer than the rest - its "
                          "first start creates the database).")
    cdd.add_argument("--force", action="store_true",
                     help="Clean-recreate an existing instance: docker compose down -v (removes its "
                          "containers AND data volumes) before rebuilding. Required to change engine version.")
    cdd.add_argument("--dry-run", action="store_true",
                     help="Print the generated compose/.env/connection/commands without changing anything.")
    cdd.add_argument("--remote-host", dest="remote_host", default="",
                     help="Provision on this Ubuntu host over SSH instead of locally (run the CLI on "
                          "the master, docker runs on the remote machine — no intermediate hop). "
                          "Needs docker + compose installed there and the containers dir writable by the user.")
    cdd.add_argument("--remote-user", dest="remote_user", default="",
                     help="SSH user on --remote-host.")
    cdd.add_argument("--remote-port", dest="remote_port", type=int, default=22,
                     help="SSH port on --remote-host (default 22).")
    cdd.add_argument("--remote-password", dest="remote_password", default=None,
                     help="SSH password value (visible in the process list; prefer -ref or -env).")
    cdd.add_argument("--remote-password-ref", dest="remote_password_ref", default=None,
                     help="Secret-store ref holding the SSH password (decrypted with --key/--key-base64).")
    cdd.add_argument("--remote-password-env", dest="remote_password_env", default=None,
                     help="Name of an env var holding the SSH password (how the Telegram command "
                          "passes it, so nothing sensitive reaches argv).")
    cdd.add_argument("--remote-key", dest="remote_key", default=None,
                     help="SSH private key for key-auth hosts (e.g. Oracle Cloud), used instead of a "
                          "password. A bare file name is looked up in data/ssh_keys/; an absolute "
                          "path is used as-is.")
    cdd.add_argument("--install-docker", dest="install_docker", action="store_true",
                     help="With --remote-host: install docker + compose on the VM over SSH if "
                          "missing (needs sudo), add the SSH user to the docker group, and create "
                          "the containers dir. Idempotent — a host that already has Docker is left as-is.")
    cdd.set_defaults(handler=_handle_create_db_docker)

    mdd = subparsers.add_parser(
        "move-db-docker",
        help="Move an existing lab database Docker instance to another host: its image, compose "
             "file, .env and the contents of its named volumes, then start it there.",
    )
    mdd.add_argument("--name", required=True,
                     help="Instance name — the directory name under the containers dir, which is "
                          "also the compose project name.")
    mdd.add_argument("--from-target", dest="from_target", required=True,
                     help="Source host: a server_id or ip from db_instances.json (its cmd_access "
                          "block and credential are used, so no password is typed here).")
    mdd.add_argument("--to-target", dest="to_target", required=True,
                     help="Destination host, resolved the same way.")
    mdd.add_argument("--containers-dir", default=DEFAULT_CONTAINERS_DIR,
                     help=f"Instance folders on the SOURCE (default {DEFAULT_CONTAINERS_DIR}).")
    mdd.add_argument("--to-containers-dir", dest="dest_containers_dir", default="",
                     help="Instance folders on the DESTINATION. Default: the same as the source.")
    mdd.add_argument("--stage-dir", dest="stage_dir", default=DEFAULT_STAGE_DIR,
                     help=f"Where the bundle is written on both hosts (default {DEFAULT_STAGE_DIR}).")
    mdd.add_argument("--no-volumes", dest="include_volumes", action="store_false",
                     help="Ship the image and the compose files only. The moved instance then "
                          "initialises an EMPTY database on first start — say this deliberately.")
    mdd.add_argument("--commit-container", dest="commit_container", action="store_true",
                     help="Move the container's own filesystem too, by committing it to an image "
                          "first. Needed whenever the engine writes outside its declared volumes "
                          "— gvenzl/oracle-xe:11 keeps XE's datafiles at /u01/app/oracle/oradata, "
                          "which no volume covers. Without it such a move ships an EMPTY database "
                          "that reports itself healthy; the mover refuses rather than let that "
                          "happen, and names this flag.")
    mdd.add_argument("--stop-source", dest="stop_source", action="store_true",
                     help="A move rather than a clone: take the source stack down once the "
                          "destination is healthy. Its volumes and files are kept.")
    mdd.add_argument("--keep-stage", dest="keep_stage", action="store_true",
                     help="Leave the bundle on both hosts (for inspection, or a second attempt).")
    mdd.add_argument("--engine", default="",
                     help="Engine of the instance, for the health probe. Default: whatever "
                          "data/docker_db_connections.json records for it.")
    mdd.add_argument("--health-timeout", dest="health_timeout", type=int, default=0,
                     help="Seconds to wait for the moved stack to become healthy. Default: the "
                          "engine's own ceiling.")
    mdd.add_argument("--no-register", dest="register", action="store_false",
                     help="Do not repoint the connection entry at the new host.")
    mdd.add_argument("--force", action="store_true",
                     help="Replace an instance of the same name on the destination — this DESTROYS "
                          "it and its volumes there before the moved one is written.")
    mdd.add_argument("--data-dir", default=None)
    mdd.add_argument("--dry-run", action="store_true",
                     help="Read the source and print what would be moved. Touches nothing.")
    mdd.add_argument("--key", default=None, help="Secret passphrase (resolves the SSH credentials).")
    mdd.add_argument("--key-base64", dest="key_base64", default=None, help="Base64 secret passphrase.")
    mdd.set_defaults(handler=_handle_move_db_docker)

    rdc = subparsers.add_parser(
        "register-db-connection",
        help="Register a database connection under data/ without creating a Docker instance.",
    )
    rdc.add_argument("--name", required=True)
    rdc.add_argument("--engine", required=True, choices=list(VALID_ENGINES))
    rdc.add_argument("--host", required=True)
    rdc.add_argument("--port", type=int, required=True)
    rdc.add_argument("--database", default=None, help="Default database (defaults to the engine default).")
    rdc.add_argument("--username", default=None, help="Login user (defaults to the engine default).")
    rdc.add_argument("--password-env", dest="password_env", required=True)
    rdc.add_argument("--version", default="", help="Optional image version to record.")
    rdc.add_argument("--mode", default="single", choices=list(VALID_MODES))
    rdc.add_argument("--compose-path", dest="compose_path", default="", help="Optional compose path to record.")
    rdc.add_argument("--data-dir", default=None)
    rdc.add_argument("--registry", default=None)
    rdc.add_argument("--dry-run", action="store_true")
    rdc.set_defaults(handler=_handle_register_db_connection)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv or sys.argv[1:]))
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0

    logger = None
    try:
        config_path = str(resolve_config_path("sre", args.config))
        config = load_config(config_path)
        log_scope = os.getenv(LOG_SCOPE_ENV_VAR) or "sre"
        _main_log_path, runtime_log_path = build_log_paths(config.log_dir, log_scope)
        patch_stdout(runtime_log_path, app_name="db_ops_sre")
        logger = setup_app_logger(config, app_name="db_ops_sre", log_scope=log_scope)

        sre_config: SreOperationalConfig | None = None
        if args.command in _REQUIRES_SRE_CONFIG:
            sre_config = load_sre_operational_config(config_path)

        return int(args.handler(args, logger, sre_config=sre_config))
    except Exception as exc:  # noqa: BLE001 - CLI boundary.
        if logger:
            log_function_error(logger, function_name="sre.cli", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _handle_list_workflows(args: argparse.Namespace, logger, *, sre_config=None) -> int:
    log_function_call(logger, function_name="sre.list_workflows")
    for name, description, cli_args in list_known_workflows():
        print(f"{name}\t{description}\t{subprocess.list2cmdline(cli_args)}")
    log_event(logger, level="logging", message="Listed db_ops.sre workflows.")
    return 0


def _handle_inventory_assets(args: argparse.Namespace, logger, *, sre_config: SreOperationalConfig) -> int:
    log_function_call(logger, function_name="sre.inventory_assets")
    assets = list_inventory_assets(sre_config)
    if not assets:
        print("No inventory groups found in SRE config.")
    for group_name, count, names in assets:
        nodes_str = ", ".join(names) if names else "(none)"
        print(f"{group_name}\t{count} nodes\t{nodes_str}")
    log_event(logger, level="logging", message=f"Listed SRE inventory assets groups={len(assets)}.")
    return 0


def _handle_vmware_list(args: argparse.Namespace, logger, *, sre_config=None) -> int:
    log_function_call(logger, function_name="sre.vmware_list")
    for name, script, description in list_vmware_commands():
        print(f"{name}\t{script}\t{description}")
    log_event(logger, level="logging", message="Listed VMware wrapper commands.")
    return 0


def _handle_check(args: argparse.Namespace, logger, *, sre_config: SreOperationalConfig) -> int:
    cmd = args.check_command
    log_function_call(logger, function_name=f"sre.{cmd}")
    dispatch = {
        "check-shared-vms": check_shared_vms,
        "check-mysql-cluster": check_mysql_cluster,
        "check-postgresql-ha": check_postgresql_ha,
    }
    kwargs: dict = {"dry_run": args.dry_run}
    if cmd == "check-shared-vms" and getattr(args, "vm_name", None):
        kwargs["vm_names"] = args.vm_name
    results = dispatch[cmd](sre_config, **kwargs)
    failed = _emit_results(results, dry_run=args.dry_run)
    level = "logging" if failed == 0 else "error"
    log_event(logger, level=level, message=f"{cmd} checks={len(results)} failed={failed}.")
    return 1 if failed else 0


def _handle_run_powershell(args: argparse.Namespace, logger, *, sre_config: SreOperationalConfig) -> int:
    log_function_call(logger, function_name="sre.run_powershell")
    result = run_powershell_script(
        sre_config, script_name=args.script, args=_strip_sep(args.script_args), dry_run=args.dry_run
    )
    return _emit_single(result, dry_run=args.dry_run, logger=logger, label=f"run-powershell:{args.script}")


def _handle_run_bastion_script(args: argparse.Namespace, logger, *, sre_config: SreOperationalConfig) -> int:
    log_function_call(logger, function_name="sre.run_bastion_script")
    result = run_bastion_script(
        sre_config, script_name=args.script, args=_strip_sep(args.script_args), dry_run=args.dry_run
    )
    return _emit_single(result, dry_run=args.dry_run, logger=logger, label=f"run-bastion-script:{args.script}")


def _handle_run_bastion_playbook(args: argparse.Namespace, logger, *, sre_config: SreOperationalConfig) -> int:
    log_function_call(logger, function_name="sre.run_bastion_playbook")
    result = run_bastion_ansible_playbook(
        sre_config,
        playbook=args.playbook,
        inventory=args.inventory,
        args=_strip_sep(args.playbook_args),
        dry_run=args.dry_run,
    )
    return _emit_single(result, dry_run=args.dry_run, logger=logger, label=f"run-bastion-playbook:{args.playbook}")


def _handle_run_bastion_ansible(args: argparse.Namespace, logger, *, sre_config: SreOperationalConfig) -> int:
    log_function_call(logger, function_name="sre.run_bastion_ansible")
    result = run_bastion_ansible(
        sre_config,
        target=args.target,
        inventory=args.inventory,
        args=_strip_sep(args.ansible_args),
        dry_run=args.dry_run,
    )
    return _emit_single(result, dry_run=args.dry_run, logger=logger, label=f"run-bastion-ansible:{args.target}")


def _handle_ssh(args: argparse.Namespace, logger, *, sre_config: SreOperationalConfig) -> int:
    log_function_call(logger, function_name="sre.ssh")
    result = run_ssh_command(
        sre_config, host=args.host, command_args=_strip_sep(args.command_args), dry_run=args.dry_run
    )
    return _emit_single(result, dry_run=args.dry_run, logger=logger, label=f"ssh:{args.host}")


def _handle_setup_sequence(args: argparse.Namespace, logger, *, sre_config=None) -> int:
    log_function_call(logger, function_name="sre.setup_sequence")
    config_path = str(resolve_config_path("sre", args.config))
    db_type: str = args.db_type
    dry_run: bool = bool(args.dry_run)
    from_step: int = int(args.from_step or 1)
    continue_on_error: bool = bool(args.continue_on_error)

    steps = list(_SEQUENCE_STEPS[db_type])

    # Oracle sequences clone from Oracle Linux, not the shared Ubuntu template.
    # Inject -Template from oracle.rac_template_name / oracle.dg_template_name.
    if sre_config and db_type in ("oracle-rac", "oracle-dg"):
        _tmpl_key = "rac_template_name" if db_type == "oracle-rac" else "dg_template_name"
        _oracle_group = "oracle_rac" if db_type == "oracle-rac" else "oracle_dg"
        _oracle_tmpl = str(sre_config.oracle.get(_tmpl_key) or "").strip()
        if _oracle_tmpl:
            steps = [
                step + ["-Template", _oracle_tmpl]
                if (step[:2] == ["run-powershell", "01-clone-vms"]
                    and "-Group" in step and _oracle_group in step)
                else step
                for step in steps
            ]

    total = len(steps)
    log_event(
        logger,
        level="logging",
        message=f"sre.setup_sequence.start|db_type={db_type} steps={total} dry_run={dry_run} from_step={from_step}",
    )
    print(f"[setup-sequence] db_type={db_type}  total_steps={total}  dry_run={dry_run}", flush=True)

    errors = 0
    for index, step_argv in enumerate(steps, start=1):
        step_label = subprocess.list2cmdline(step_argv)
        if index < from_step:
            print(f"[step {index:>2}/{total}] SKIP  {step_label}", flush=True)
            continue

        if dry_run:
            child_step = [step_argv[0], "--dry-run", *step_argv[1:]]
        else:
            child_step = list(step_argv)
        child_argv = [sys.executable, "-m", "db_ops.sre.cli", "--config", config_path, *child_step]

        print(f"[step {index:>2}/{total}] START {step_label}", flush=True)
        log_event(logger, level="logging", message=f"sre.setup_sequence.step.start|step={index} db_type={db_type} cmd={step_label}")

        result = subprocess.run(child_argv, check=False)
        rc = result.returncode

        status = "OK" if rc == 0 else f"FAIL (exit={rc})"
        level = "logging" if rc == 0 else "error"
        print(f"[step {index:>2}/{total}] {status}  {step_label}", flush=True)
        log_event(logger, level=level, message=f"sre.setup_sequence.step.done|step={index} db_type={db_type} exit={rc}")

        if rc != 0:
            errors += 1
            if not continue_on_error:
                print(
                    f"[setup-sequence] Aborted at step {index}/{total}. "
                    "Use --continue-on-error to proceed past failures.",
                    file=sys.stderr,
                )
                return rc

    if errors:
        print(f"[setup-sequence] Completed with {errors} failed step(s).", file=sys.stderr)
        log_event(logger, level="error", message=f"sre.setup_sequence.done|db_type={db_type} steps={total} errors={errors}")
        return 1

    print(f"[setup-sequence] All {total} steps completed successfully.", flush=True)
    log_event(logger, level="logging", message=f"sre.setup_sequence.done|db_type={db_type} steps={total} errors=0")
    return 0


def _resolve_data_dir(config_path: str, override: str | None) -> str:
    if override:
        return override
    from pathlib import Path
    path = Path(config_path).resolve()
    # sre config often resolves to data/sre_config.json -> its parent is the data dir.
    if path.parent.name == "data":
        return str(path.parent)
    return str(path.parent / "data")


def _handle_create_db_docker(args: argparse.Namespace, logger, *, sre_config=None) -> int:
    log_function_call(logger, function_name="sre.create_db_docker")
    config_path = str(resolve_config_path("sre", args.config))
    data_dir = _resolve_data_dir(config_path, args.data_dir)

    # Defaults an operator should not have to know by heart: the engine's own port, and a
    # secret ref scoped to this instance so two lab databases never share one password.
    meta = ENGINE_META.get(args.engine)
    host_port = args.host_port or (meta.container_port if meta else None)
    password_env = args.password_env or f"{str(args.name or '').upper()}_PASSWORD"
    password_text = _supplied_password_text(args)

    replicas_explicit = args.replicas is not None
    # Oracle ha-lab is Data Guard 1/1, so its implicit default is one standby.
    default_replicas = 1 if args.engine == "oracle" else 2
    spec = DockerDbSpec(
        name=args.name,
        engine=args.engine,
        version=args.version,
        mode=args.mode,
        replicas=args.replicas if replicas_explicit else default_replicas,
        host_port=host_port,
        password_env=password_env,
        # None = the engine's default. "-" is the Telegram skip sentinel and, like an explicit
        # empty string, means "no mount" rather than "use the default".
        backup_mount=(None if args.backup_mount is None
                      else ("" if str(args.backup_mount).strip() in ("", SKIP_SENTINEL)
                            else str(args.backup_mount).strip())),
        # "-" is the Telegram skip sentinel: it means "no opinion", which here is the derived
        # default — not "no subnet", because an unpinned network is the thing we are preventing.
        network_subnet=("" if str(getattr(args, "network_subnet", "") or "").strip() == SKIP_SENTINEL
                        else str(getattr(args, "network_subnet", "") or "").strip()),
    )
    remote_host_obj = None
    try:
        spec.validate(replicas_explicit=replicas_explicit)
        if password_text is not None:
            _store_password(args, spec, password_text, data_dir=data_dir, logger=logger)
        else:
            # No value supplied: the ref is expected to be in the store already. Say so, because
            # the alternative failure - "ref not found" from deep inside the provisioner - reads
            # like a store problem rather than a missing argument.
            print(f"Secret ref {password_env}: no password given, reusing the stored value.")

        # --remote-host: run every docker/file operation on that Ubuntu machine over SSH.
        # The CLI (and the connection registry write) stays on this node — typically the
        # master — so no intermediate hop is involved.
        runner_kwargs = {}
        worker_host = args.worker_host
        if getattr(args, "remote_host", ""):
            from db_ops.lib.ssh_errors import SshError
            from db_ops.sre.remote import RemoteHostError, RemoteUbuntuHost, resolve_remote_ssh_password
            if not args.remote_user:
                raise ProvisionError("--remote-host needs --remote-user.")
            remote_key = getattr(args, "remote_key", None)
            try:
                # A key-auth VM (e.g. Oracle Cloud) needs no SSH password; a password-auth host
                # resolves it from --remote-password / -ref / -env. Exactly one path is required.
                if remote_key:
                    from db_ops.common.data_sources import resolve_ssh_key
                    remote_key = resolve_ssh_key(remote_key, data_dir)  # bare name -> data/ssh_keys/
                ssh_password = None
                if not remote_key:
                    ssh_password = resolve_remote_ssh_password(
                        password=args.remote_password, password_ref=args.remote_password_ref,
                        password_env=getattr(args, "remote_password_env", None),
                        key=args.key, key_base64=args.key_base64, data_dir=data_dir,
                    )
                remote_host_obj = RemoteUbuntuHost(
                    args.remote_host, args.remote_user, ssh_password, port=args.remote_port,
                    key_filename=remote_key,
                )
            except (RemoteHostError, SshError) as exc:
                raise ProvisionError(str(exc)) from exc
            runner_kwargs = {"runner": remote_host_obj.run, "fs": remote_host_obj}
            # The connection entry must point at the machine the DB actually runs on: the
            # remote host wins over any --worker-host default carried in the base command.
            worker_host = args.remote_host or args.worker_host
            # Optionally install docker + compose on the VM first, so a fresh Ubuntu host can be
            # provisioned with one command. Reconnects internally so docker works as the user.
            if getattr(args, "install_docker", False):
                from db_ops.sre.remote import ensure_docker
                summary = ensure_docker(remote_host_obj, sudo_password=ssh_password,
                                        containers_dir=args.containers_dir)
                log_event(logger, level="logging",
                          message=f"sre.create_db_docker.ensure_docker|host={summary['host']} "
                                  f"already_present={summary['already_present']} installed={summary['installed']}")
                print(f"Docker ready on {summary['host']} "
                      f"({'already present' if summary['already_present'] else 'installed'}).", flush=True)

        rc = provision(
            spec,
            containers_dir=args.containers_dir,
            worker_host=worker_host,
            data_dir=data_dir,
            key=args.key,
            key_base64=args.key_base64,
            dry_run=args.dry_run,
            force=args.force,
            register=args.register,
            registry_path=args.registry,
            health_timeout=args.health_timeout,
            **runner_kwargs,
        )
    except (ValueError, ProvisionError) as exc:
        log_function_error(logger, function_name="sre.create_db_docker", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if remote_host_obj is not None:
            remote_host_obj.close()
    log_event(logger, level="logging",
              message=f"sre.create_db_docker|name={args.name} engine={args.engine} mode={args.mode} "
                      f"dry_run={args.dry_run} rc={rc}")
    return int(rc)


def _handle_move_db_docker(args: argparse.Namespace, logger, *, sre_config=None) -> int:
    """``move-db-docker`` — relocate an existing lab instance, data included.

    The two hosts are named as db_ops *targets*, not as host/user/password: both of them already
    run db_ops containers, so both are already in ``db_instances.json`` with a credential the
    store resolves. Everything else is :mod:`db_ops.sre.docker_db.mover`.
    """
    from db_ops.lib.secret_text import set_key_env
    from db_ops.sre.docker_db import mover
    from db_ops.sre.remote import RemoteHostError, open_ubuntu_host

    log_function_call(logger, function_name="sre.move_db_docker")
    config_path = str(resolve_config_path("sre", args.config))
    data_dir = _resolve_data_dir(config_path, args.data_dir)

    spec = mover.MoveSpec(
        name=args.name,
        source_target=args.from_target,
        dest_target=args.to_target,
        containers_dir=args.containers_dir,
        dest_containers_dir=args.dest_containers_dir,
        stage_dir=args.stage_dir,
        include_volumes=args.include_volumes,
        commit_container=args.commit_container,
        stop_source=args.stop_source,
        keep_stage=args.keep_stage,
        force=args.force,
        engine=args.engine,
        health_timeout=args.health_timeout,
        register=args.register,
    )

    source = destination = None
    try:
        # Both hosts' OS credentials live encrypted in the store, and `relay_file` opens its own
        # sessions from the same targets — so the key has to be in the environment, not only in
        # this function's arguments.
        set_key_env(args.key, args.key_base64)
        source = open_ubuntu_host(spec.source_target, data_dir=data_dir)
        destination = open_ubuntu_host(spec.dest_target, data_dir=data_dir)
        result = mover.move(spec, source=source, destination=destination,
                            data_dir=data_dir, dry_run=args.dry_run)
    except (ValueError, RemoteHostError, mover.MoveError) as exc:
        log_function_error(logger, function_name="sre.move_db_docker", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for host in (source, destination):
            if host is not None:
                try:
                    host.close()
                except Exception:  # noqa: BLE001 - the move already succeeded or already failed
                    pass

    if not args.dry_run:
        print("\n" + mover.format_summary(result))
    log_event(logger, level="logging",
              message=f"sre.move_db_docker|name={args.name} from={args.from_target} "
                      f"to={args.to_target} volumes={args.include_volumes} "
                      f"commit={args.commit_container} "
                      f"stop_source={args.stop_source} dry_run={args.dry_run}")
    return 0


#: What every optional field of the Telegram `spbot_create_db_docker` command carries when the
#: operator wants it skipped. It is a *value*, so it can arrive on a flag or inside the variable a
#: flag names — both have to be recognised.
SKIP_SENTINEL = "-"


def _supplied_password_text(args: argparse.Namespace) -> str | None:
    """The password the caller actually gave, or ``None`` meaning "reuse the stored ref".

    The sentinel used to be normalised on the *flags* only. But the Telegram command passes the
    value through an environment variable whose NAME is fixed (``--password-text-env
    DB_OPS_NEW_DB_PASSWORD``), so a skipped password arrived as a variable containing ``-`` and the
    flag looked perfectly legitimate. Both of the command's failures on 2026-08-10 came from that
    one gap, and neither of them mentioned a password:

    * a new ref was stored holding the literal ``-``, which SQL Server then rejected for failing
      its password policy;
    * an existing ref reported ``already exists with a different value`` and aborted — the reuse
      path the operator was asking for could never be reached.

    So the decision is made once, here, on the resolved *value*. An explicitly named variable that
    is genuinely empty is still an error: that is a caller who meant to set a password and lost it,
    which must not be silently downgraded to "reuse whatever is stored".
    """
    direct = str(getattr(args, "password_text", None) or "").strip()
    if direct and direct != SKIP_SENTINEL:
        return direct

    env_name = str(getattr(args, "password_text_env", None) or "").strip()
    if not env_name or env_name == SKIP_SENTINEL:
        return None
    value = os.environ.get(env_name, "").strip()
    if value == SKIP_SENTINEL:
        return None
    if not value:
        raise ProvisionError(
            f"--password-text-env {env_name} names an environment variable that is empty; "
            "pass the password there, or give --password-env alone to reuse a stored ref."
        )
    return value


def _store_password(args: argparse.Namespace, spec, value: str, *, data_dir, logger) -> None:
    """Persist ``value`` into the encrypted secret store under ``spec.password_env``, then put it
    in this process's environment so the provisioner resolves it like any other secret (env first,
    then the store).

    Resolving *which* value was supplied belongs to :func:`_supplied_password_text`; by the time
    this runs the caller has already decided that a password was given.
    """
    from db_ops.lib.secret_text import resolve_cli_key, set_secret_text

    key = resolve_cli_key(args.key, args.key_base64) or os.environ.get("DB_OPS_SECRET_KEY")
    if not key:
        raise ProvisionError(
            "Storing a password needs the secret-store passphrase (--key/--key-base64 or "
            "DB_OPS_SECRET_KEY): the store is encrypted at rest."
        )
    written = set_secret_text(data_dir, spec.password_env, value, key=key,
                              overwrite=bool(args.overwrite_secret))
    os.environ[spec.password_env] = value  # so resolve_password_value finds it without a re-read
    log_event(logger, level="logging",
              message=f"sre.create_db_docker.secret|ref={spec.password_env} "
                      f"stored={'yes' if written else 'already-current'}")
    print(f"Secret ref {spec.password_env}: "
          f"{'stored in the encrypted secret store' if written else 'already had this value'}.")


def _handle_register_db_connection(args: argparse.Namespace, logger, *, sre_config=None) -> int:
    log_function_call(logger, function_name="sre.register_db_connection")
    config_path = str(resolve_config_path("sre", args.config))
    data_dir = _resolve_data_dir(config_path, args.data_dir)
    meta = ENGINE_META[args.engine]
    docker: dict = {"instance_name": args.name, "mode": args.mode}
    if args.version:
        docker["version"] = args.version
    if args.compose_path:
        docker["compose_path"] = args.compose_path
    entry = {
        "id": docker_register.connection_id(args.name),
        "engine": args.engine,
        "host": args.host,
        "port": args.port,
        "database": args.database or meta.database,
        "username": args.username or meta.username,
        "password_env": args.password_env,
        "docker": docker,
        "created_by": docker_register.CREATED_BY,
    }
    registry = args.registry or str(docker_register.default_registry_path(data_dir))
    if args.dry_run:
        import json
        print(f"# would register into {registry}:")
        print(json.dumps({docker_register.REGISTRY_ROOT_KEY: [entry]}, indent=2, ensure_ascii=False))
        return 0
    action = docker_register.register_connection(registry, entry)
    print(f"Connection {action} in {registry}.")
    log_event(logger, level="logging",
              message=f"sre.register_db_connection|name={args.name} action={action}")
    return 0


def _emit_results(results, *, dry_run: bool) -> int:
    failed = 0
    for index, result in enumerate(results, start=1):
        if dry_run:
            print(f"# check {index}")
            print(_redact_command(subprocess.list2cmdline(result)))
            continue
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode != 0:
            failed += 1
    return failed


def _emit_single(result, *, dry_run: bool, logger, label: str) -> int:
    if dry_run:
        print(_redact_command(subprocess.list2cmdline(result)))
        return 0
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    rc = int(result.returncode)
    level = "logging" if rc == 0 else "error"
    log_event(logger, level=level, message=f"{label} exit={rc}")
    return rc


def _strip_sep(args: list[str]) -> list[str]:
    return args[1:] if args[:1] == ["--"] else args


def _redact_command(command: str) -> str:
    return re.sub(r"(--password=)(\"[^\"]*\"|'[^']*'|\S+)", r"\1***", command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
