from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SreOperationalConfig:
    """SRE operational settings from the 'sre' section of config.sre.json."""

    root_dir: Path
    inventory: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, Any] = field(default_factory=dict)
    database_defaults: dict[str, Any] = field(default_factory=dict)
    vmware: dict[str, Any] = field(default_factory=dict)
    automation: dict[str, Any] = field(default_factory=dict)
    oracle: dict[str, Any] = field(default_factory=dict)

    def bastion_host(self) -> str:
        groups = self.inventory.get("groups") or {}
        for item in groups.get("shared", []):
            if item.get("name") == "bastion-01" or item.get("role") == "bastion":
                return str(item["ip"])
        raise RuntimeError("bastion-01 not found in SRE config inventory.shared.")

    def inventory_group(self, group: str) -> list[dict]:
        groups = self.inventory.get("groups") or {}
        nodes = groups.get(group) or []
        if not nodes:
            raise RuntimeError(f"Inventory group not found or empty: {group}")
        return [dict(node) for node in nodes]

    def first_node(self, group: str) -> dict:
        return self.inventory_group(group)[0]

    def net_interface(self) -> str:
        return str(self.vmware.get("net_interface", "")).strip()

    def guest_user(self) -> str:
        return str(self.credentials.get("guest_user", "tuser")).strip() or "tuser"

    def guest_password(self) -> str | None:
        value = self.credentials.get("guest_password")
        return str(value).strip() if value else None

    def ssh_identity_file(self) -> str | None:
        value = self.credentials.get("ssh_identity_file")
        if not value:
            return None
        path = Path(str(value)).expanduser()
        return str(path.resolve() if not path.is_absolute() else path)

    def powershell_dir(self) -> Path | None:
        return self._resolve_auto_path("powershell_dir")

    def bash_dir(self) -> Path | None:
        return self._resolve_auto_path("bash_dir")

    def ansible_dir(self) -> Path | None:
        return self._resolve_auto_path("ansible_dir")

    def _resolve_auto_path(self, key: str) -> Path | None:
        value = self.automation.get(key)
        if not value:
            return None
        path = Path(str(value))
        return (self.root_dir / path).resolve() if not path.is_absolute() else path


def load_sre_operational_config(config_path: str | Path) -> SreOperationalConfig:
    """Load SRE operational config from the 'sre' section of the resolved config file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"SRE config not found: {path}")
    with path.open("r", encoding="utf-8-sig") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"SRE config must be a JSON object: {path}")
    sre_raw = raw.get("sre") or {}
    return SreOperationalConfig(
        root_dir=path.parent.resolve(),
        inventory=dict(sre_raw.get("inventory") or {}),
        credentials=dict(sre_raw.get("credentials") or {}),
        database_defaults=dict(sre_raw.get("database_defaults") or {}),
        vmware=dict(sre_raw.get("vmware") or {}),
        automation=dict(sre_raw.get("automation") or {}),
        oracle=dict(sre_raw.get("oracle") or {}),
    )
