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
        value = resolve_password_fields(self.credentials, data_dir=self.data_dir()).get(
            "guest_password")
        return str(value).strip() if value else None

    def data_dir(self) -> Path:
        """Where the encrypted secret store lives, for resolving a `*_password_ref`."""
        return self.root_dir / "data"

    def resolved_credentials(self) -> dict[str, Any]:
        """`credentials` with every password ref replaced by its value."""
        return resolve_password_fields(self.credentials, data_dir=self.data_dir())

    def resolved_database_defaults(self) -> dict[str, Any]:
        """`database_defaults` with every password ref replaced, engine by engine."""
        data_dir = self.data_dir()
        return {
            engine: (resolve_password_fields(settings, data_dir=data_dir)
                     if isinstance(settings, dict) else settings)
            for engine, settings in self.database_defaults.items()
        }

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


#: A password in this config may be written three ways, and the file on disk should carry the
#: third: `<name>_password` (a literal), `<name>_password_env` (an environment variable), or
#: `<name>_password_ref` (a key in the encrypted secret store). The precedence is the toolkit's
#: usual one and comes from the shared resolver rather than a copy of it.
#:
#: The literal stays supported because these values configure a machine that is about to be
#: created and destroyed, and typing one into a lab file is a reasonable thing to do. What it must
#: not be is the *only* option: a lab that gets kept turns a throwaway password into a stored one,
#: and at that point it belongs where every other credential in this toolkit lives.
PASSWORD_SUFFIXES = ("_password", "password")


def resolve_password_fields(
    section: dict[str, Any] | None, *, data_dir: str | Path | None = None
) -> dict[str, Any]:
    """A config section with every `<name>_password_ref` / `_env` resolved to `<name>_password`.

    Resolution happens here, at the point of use, rather than at load time — `sre` hands whole
    config sections to PowerShell and Ansible as a serialized payload, and those consumers need
    the value. Resolving on load would put the secret in every dump of the config; resolving
    never would ship a ref to a script that cannot look it up.

    The ref and env keys are dropped from the result, so a payload built from it carries the
    password and not the name of where the password is kept.
    """
    from db_ops.common.remote_exec import resolve_secret_value

    if not section:
        return {}
    resolved = dict(section)
    bases = {
        key.rsplit("_", 1)[0]
        for key in section
        if key.endswith(("_ref", "_env")) and key.rsplit("_", 1)[0].endswith(PASSWORD_SUFFIXES)
    }
    for base in sorted(bases):
        value = resolve_secret_value(
            section, data_dir=data_dir,
            value_key=base, env_key=f"{base}_env", ref_key=f"{base}_ref",
        )
        if value:
            resolved[base] = value
        resolved.pop(f"{base}_env", None)
        resolved.pop(f"{base}_ref", None)
    return resolved


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
