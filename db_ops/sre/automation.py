from __future__ import annotations


def list_known_workflows() -> list[tuple[str, str, list[str]]]:
    """Return (name, description, cli_args) for known db_ops.sre workflows."""
    return [
        ("check-shared-vms", "Validate shared VM identity and system state.", ["check-shared-vms"]),
        ("check-mysql-cluster", "Validate MySQL service and InnoDB Cluster topology.", ["check-mysql-cluster"]),
        ("check-postgresql-ha", "Validate PostgreSQL service and streaming replication.", ["check-postgresql-ha"]),
        ("inventory-assets", "List inventory nodes from SRE config.", ["inventory-assets"]),
        ("vmware-list", "List available VMware wrapper commands.", ["vmware-list"]),
    ]
