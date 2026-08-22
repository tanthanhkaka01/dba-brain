from __future__ import annotations

from db_ops.sre.config import SreOperationalConfig


def list_inventory_assets(sre_config: SreOperationalConfig) -> list[tuple[str, int, list[str]]]:
    """Return (group_name, node_count, node_names) for each inventory group in the SRE config."""
    groups = sre_config.inventory.get("groups") or {}
    result = []
    for group_name, nodes in groups.items():
        node_list = list(nodes) if isinstance(nodes, list) else []
        names = [str(node.get("name", "")) for node in node_list if isinstance(node, dict)]
        result.append((group_name, len(node_list), names))
    return result
