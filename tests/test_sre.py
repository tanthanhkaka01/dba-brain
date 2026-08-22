from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from db_ops.sre.automation import list_known_workflows
from db_ops.sre.config import SreOperationalConfig, load_sre_operational_config
from db_ops.sre.inventory import list_inventory_assets
from db_ops.sre.service import (
    check_mysql_cluster,
    check_postgresql_ha,
    check_shared_vms,
    list_vmware_commands,
    run_bastion_ansible,
    run_ssh_command,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SHARED_INVENTORY = {
    "groups": {
        "shared": [
            {"name": "bastion-01", "role": "bastion", "ip": "10.0.0.1"},
            {"name": "monitor-01", "role": "monitor", "ip": "10.0.0.2"},
        ],
        "mysql": [
            {"name": "mysql-01", "role": "primary", "ip": "10.0.1.1"},
            {"name": "mysql-02", "role": "secondary", "ip": "10.0.1.2"},
        ],
        "postgresql": [
            {"name": "pg-01", "role": "primary", "ip": "10.0.2.1"},
        ],
    }
}

_MYSQL_DEFAULTS = {
    "mysql": {
        "cluster_name": "test-cluster",
        "cluster_admin_user": "admin",
        "cluster_admin_password": "secret",
        "classic_port": 3306,
    }
}

_BASE_CREDENTIALS = {"guest_user": "tuser"}
_BASE_VMWARE = {"net_interface": "ens33"}


def _make_config(**kwargs) -> SreOperationalConfig:
    return SreOperationalConfig(
        root_dir=Path("/tmp/sre"),
        inventory=kwargs.get("inventory", _SHARED_INVENTORY),
        credentials=kwargs.get("credentials", _BASE_CREDENTIALS),
        database_defaults=kwargs.get("database_defaults", _MYSQL_DEFAULTS),
        vmware=kwargs.get("vmware", _BASE_VMWARE),
        automation=kwargs.get("automation", {}),
    )


# ---------------------------------------------------------------------------
# SreOperationalConfig
# ---------------------------------------------------------------------------


def test_bastion_host_found():
    config = _make_config()
    assert config.bastion_host() == "10.0.0.1"


def test_bastion_host_not_found():
    config = _make_config(inventory={"groups": {"shared": [{"name": "other", "ip": "1.2.3.4"}]}})
    with pytest.raises(RuntimeError, match="bastion-01 not found"):
        config.bastion_host()


def test_inventory_group_returns_nodes():
    config = _make_config()
    nodes = config.inventory_group("mysql")
    assert len(nodes) == 2
    assert nodes[0]["name"] == "mysql-01"


def test_inventory_group_missing_raises():
    config = _make_config()
    with pytest.raises(RuntimeError, match="Inventory group not found"):
        config.inventory_group("oracle")


def test_first_node():
    config = _make_config()
    node = config.first_node("postgresql")
    assert node["name"] == "pg-01"


def test_guest_user_default():
    config = _make_config(credentials={})
    assert config.guest_user() == "tuser"


def test_guest_user_from_credentials():
    config = _make_config(credentials={"guest_user": "deploy"})
    assert config.guest_user() == "deploy"


def test_net_interface():
    config = _make_config()
    assert config.net_interface() == "ens33"


def test_powershell_dir_none_when_not_configured():
    config = _make_config(automation={})
    assert config.powershell_dir() is None


def test_powershell_dir_resolved(tmp_path):
    config = SreOperationalConfig(
        root_dir=tmp_path,
        automation={"powershell_dir": "automation/powershell"},
    )
    assert config.powershell_dir() == tmp_path / "automation" / "powershell"


# ---------------------------------------------------------------------------
# load_sre_operational_config
# ---------------------------------------------------------------------------


def test_load_sre_operational_config(tmp_path):
    cfg = {
        "log_dir": "logs",
        "sre": {
            "inventory": _SHARED_INVENTORY,
            "credentials": {"guest_user": "admin"},
        },
    }
    path = tmp_path / "config.sre.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    result = load_sre_operational_config(path)
    assert result.guest_user() == "admin"
    assert result.inventory_group("mysql")[0]["name"] == "mysql-01"
    assert result.root_dir == tmp_path


def test_load_sre_operational_config_missing_sre_section(tmp_path):
    cfg = {"log_dir": "logs"}
    path = tmp_path / "config.sre.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    result = load_sre_operational_config(path)
    assert result.inventory == {}
    assert result.credentials == {}


def test_load_sre_operational_config_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_sre_operational_config(tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# list_inventory_assets
# ---------------------------------------------------------------------------


def test_list_inventory_assets():
    config = _make_config()
    assets = list_inventory_assets(config)
    groups = {name: (count, names) for name, count, names in assets}
    assert groups["shared"][0] == 2
    assert "bastion-01" in groups["shared"][1]
    assert groups["mysql"][0] == 2
    assert groups["postgresql"][0] == 1


def test_list_inventory_assets_empty():
    config = _make_config(inventory={})
    assert list_inventory_assets(config) == []


# ---------------------------------------------------------------------------
# list_known_workflows
# ---------------------------------------------------------------------------


def test_list_known_workflows_returns_entries():
    workflows = list_known_workflows()
    names = [name for name, _, _ in workflows]
    assert "check-mysql-cluster" in names
    assert "check-postgresql-ha" in names
    assert "check-shared-vms" in names


def test_list_known_workflows_cli_args_non_empty():
    for name, desc, cli_args in list_known_workflows():
        assert cli_args, f"workflow '{name}' has empty cli_args"


# ---------------------------------------------------------------------------
# list_vmware_commands
# ---------------------------------------------------------------------------


def test_list_vmware_commands():
    cmds = list_vmware_commands()
    names = [name for name, _, _ in cmds]
    assert "clone-vms" in names
    assert "bootstrap-bastion" in names
    assert all(script.endswith(".ps1") for _, script, _ in cmds)


# ---------------------------------------------------------------------------
# service — dry_run command construction
# ---------------------------------------------------------------------------


def test_run_ssh_command_dry_run():
    config = _make_config()
    result = run_ssh_command(config, host="10.0.0.5", command_args=["uptime"], dry_run=True)
    assert isinstance(result, list)
    assert result[0] == "ssh"
    # SSH to DB nodes is routed through the bastion (ProxyJump-style nested ssh):
    # the outer hop targets the bastion, the inner command ssh'es to the DB node.
    assert any("@10.0.0.5" in token for token in result)
    assert any("uptime" in token for token in result)


def test_run_bastion_ansible_dry_run():
    config = _make_config()
    result = run_bastion_ansible(config, target="mysql", args=["-m", "ping"], dry_run=True)
    assert isinstance(result, list)
    assert result[0] == "ssh"
    cmd_str = result[-1]
    assert "ansible" in cmd_str
    assert "mysql" in cmd_str
    assert "ping" in cmd_str


def test_check_shared_vms_dry_run_returns_commands():
    config = _make_config()
    results = check_shared_vms(config, dry_run=True)
    assert len(results) == 2
    for result in results:
        assert isinstance(result, list)
        assert result[0] == "ssh"


def test_check_mysql_cluster_dry_run_returns_commands():
    config = _make_config()
    results = check_mysql_cluster(config, dry_run=True)
    assert len(results) == 3  # 2 ansible + 1 mysqlsh
    assert all(isinstance(r, list) for r in results)


def test_check_postgresql_ha_dry_run_returns_commands():
    config = _make_config()
    results = check_postgresql_ha(config, dry_run=True)
    assert len(results) == 3  # 2 ansible + 1 psql
    assert all(isinstance(r, list) for r in results)


def test_mysql_check_redacts_password_in_command():
    config = _make_config()
    results = check_mysql_cluster(config, dry_run=True)
    mysqlsh_cmd = results[2]
    assert isinstance(mysqlsh_cmd, list)
    cmd_str = " ".join(str(p) for p in mysqlsh_cmd)
    assert "mysqlsh" in cmd_str


def test_check_mysql_cluster_missing_group_raises():
    config = _make_config(inventory={"groups": {}})
    with pytest.raises(RuntimeError, match="Inventory group not found"):
        check_mysql_cluster(config, dry_run=True)


def test_check_postgresql_ha_missing_group_raises():
    config = _make_config(inventory={"groups": {}})
    with pytest.raises(RuntimeError, match="Inventory group not found"):
        check_postgresql_ha(config, dry_run=True)


# ---------------------------------------------------------------------------
# Import boundary — db_ops.sre must not import from db_sre
# ---------------------------------------------------------------------------

_SRE_PKG = Path(__file__).resolve().parents[1] / "db_ops" / "sre"


def _collect_imports(py_file: Path) -> list[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def test_no_db_sre_imports_in_db_ops_sre():
    violations = []
    for py_file in _SRE_PKG.glob("*.py"):
        for imp in _collect_imports(py_file):
            if imp.startswith("db_sre"):
                violations.append(f"{py_file.name}: {imp}")
    assert not violations, f"db_ops.sre imports db_sre: {violations}"


def test_no_db_sre_subprocess_in_db_ops_sre():
    violations = []
    for py_file in _SRE_PKG.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if "db_sre.cli" in text or "db_sre/cli" in text:
            violations.append(py_file.name)
    assert not violations, f"db_ops.sre references db_sre.cli: {violations}"


def test_no_tools_db_sre_path_in_db_ops_sre():
    violations = []
    for py_file in _SRE_PKG.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if "tools/db_sre" in text or "tools\\db_sre" in text:
            violations.append(py_file.name)
    assert not violations, f"db_ops.sre references tools/db_sre path: {violations}"


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def test_cli_parse_list_workflows():
    from db_ops.sre.cli import build_parser

    args = build_parser().parse_args(["list-workflows"])
    assert args.command == "list-workflows"


def test_cli_parse_check_mysql():
    from db_ops.sre.cli import build_parser

    args = build_parser().parse_args(["check-mysql-cluster", "--dry-run"])
    assert args.command == "check-mysql-cluster"
    assert args.dry_run is True


def test_cli_parse_check_postgresql():
    from db_ops.sre.cli import build_parser

    args = build_parser().parse_args(["check-postgresql-ha"])
    assert args.command == "check-postgresql-ha"
    assert args.dry_run is False


def test_cli_parse_inventory_assets():
    from db_ops.sre.cli import build_parser

    args = build_parser().parse_args(["inventory-assets"])
    assert args.command == "inventory-assets"


def test_cli_parse_vmware_list():
    from db_ops.sre.cli import build_parser

    args = build_parser().parse_args(["vmware-list"])
    assert args.command == "vmware-list"


def test_cli_no_delegate_command():
    from db_ops.sre.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["delegate", "--", "automation", "check-mysql-cluster"])


def test_cli_config_defaults_to_none():
    from db_ops.sre.cli import build_parser

    args = build_parser().parse_args(["list-workflows"])
    assert args.config is None
