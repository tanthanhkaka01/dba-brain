"""The restore spec: everything a restore needs, stated in the request rather than looked up.

``common`` is an API layer. It holds no data and reads no config - not ``restore_config.json``, not
``db_instances.json``, not the secret store. A caller hands it a complete description of the work
and it performs exactly that. The reason is that a lookup inside the API makes the API's behaviour
depend on files the caller cannot see: two callers passing the same request get different restores
because one machine's ``restore_config.json`` is a week older, and nothing in the request says so.

So the spec names the engine, both ends, and the credentials:

    {"db_type": "sqlserver",
     "source": {"access": "smb", "host": "192.0.2.250", "path": "\\\\...\\SQLBK\\APPDB-DB$APPDB",
                "username": "...", "password": "..."},
     "target": {"platform": "linux", "host": "192.0.2.249", "port": 1433,
                "username": "sa", "password": "...",
                "data_dir": "/var/opt/mssql/data", "import_dir": "/opt/.../SQLBK_IMPORT"},
     "databases": ["APPDB"],
     "point_in_time": "2026-08-06 14:00:00 +07:00"}

**Resolving that spec is the app's job**, and stays there: ``backup_restore`` reads the entry, asks
the inventory for the host, decrypts the password, and hands the finished object down. The split is
what makes the API testable without a store and callable from something that has no
``restore_config.json`` at all - a one-off recovery against a machine nobody has registered yet.

Validation is strict about what it accepts because the failure mode of a silent default here is a
restore that ran somewhere other than where the caller meant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class RestoreSpecError(ValueError):
    """The spec cannot be honoured as written."""


#: Engines this API can restore. Kept explicit so an unsupported one is refused by name rather
#: than falling through to a SQL Server code path that would fail much later and less clearly.
SUPPORTED_DB_TYPES = frozenset({"sqlserver"})

#: How the backup files are reached at the source.
SOURCE_ACCESS = frozenset({
    "smb",    # a UNC share, read with smbclient or by the Windows caller directly
    "ssh",    # a directory on the source host's own filesystem, read over SFTP
    "local",  # already visible to the caller - a mount, or the same machine
})

#: What the target is, which decides how the instance is reached and how paths are written.
TARGET_PLATFORMS = frozenset({"windows", "linux"})


@dataclass(frozen=True)
class SourceSpec:
    """Where the backup files are, and how to read them."""

    access: str
    path: str
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""


@dataclass(frozen=True)
class TargetSpec:
    """The instance being restored into, and where its files go.

    ``container`` is optional and deliberately not a separate platform: a container is a Linux
    host with the instance on a port and an import directory it can read. Naming it buys better
    log lines and nothing else, which is why the restore works the same with or without it.
    """

    platform: str
    host: str
    username: str
    password: str
    port: int = 1433
    instance: str = ""
    data_dir: str = ""
    log_dir: str = ""
    import_dir: str = ""
    container: str = ""
    ssh_username: str = ""
    ssh_password: str = ""
    ssh_port: int = 22


@dataclass(frozen=True)
class RestoreSpec:
    """One restore, fully described."""

    db_type: str
    source: SourceSpec
    target: TargetSpec
    databases: tuple[str, ...] = ()
    point_in_time: str = ""
    copy_hours: int = 24
    dry_run: bool = False
    label: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def is_point_in_time(self) -> bool:
        return bool(self.point_in_time)


def _require(mapping: dict[str, Any], key: str, where: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise RestoreSpecError(f"{where}.{key} is required.")
    return value


def _one_of(value: str, allowed: frozenset[str], where: str) -> str:
    normalised = str(value or "").strip().lower()
    if normalised not in allowed:
        raise RestoreSpecError(
            f"{where} must be one of {', '.join(sorted(allowed))}; got {value!r}."
        )
    return normalised


def parse_source(raw: Any) -> SourceSpec:
    if not isinstance(raw, dict):
        raise RestoreSpecError("source must be an object.")
    access = _one_of(raw.get("access"), SOURCE_ACCESS, "source.access")
    spec = SourceSpec(
        access=access,
        path=_require(raw, "path", "source"),
        host=str(raw.get("host") or "").strip(),
        port=int(raw.get("port") or 0),
        username=str(raw.get("username") or "").strip(),
        password=str(raw.get("password") or ""),
    )
    # A remote read needs somewhere to read from. Defaulting the host to the target would
    # silently restore a machine from its own copy of the backups.
    if access in {"smb", "ssh"} and not spec.host:
        raise RestoreSpecError(f"source.host is required when source.access is {access!r}.")
    return spec


def parse_target(raw: Any) -> TargetSpec:
    if not isinstance(raw, dict):
        raise RestoreSpecError("target must be an object.")
    return TargetSpec(
        platform=_one_of(raw.get("platform"), TARGET_PLATFORMS, "target.platform"),
        host=_require(raw, "host", "target"),
        username=_require(raw, "username", "target"),
        password=str(raw.get("password") or ""),
        port=int(raw.get("port") or 1433),
        instance=str(raw.get("instance") or "").strip(),
        data_dir=str(raw.get("data_dir") or "").strip(),
        log_dir=str(raw.get("log_dir") or "").strip(),
        import_dir=str(raw.get("import_dir") or "").strip(),
        container=str(raw.get("container") or "").strip(),
        ssh_username=str(raw.get("ssh_username") or "").strip(),
        ssh_password=str(raw.get("ssh_password") or ""),
        ssh_port=int(raw.get("ssh_port") or 22),
    )


def parse_restore_spec(raw: Any) -> RestoreSpec:
    """Build a validated spec from the request object. Raises on anything unusable."""
    if not isinstance(raw, dict):
        raise RestoreSpecError("request must be a JSON object.")

    known = {"db_type", "source", "target", "databases", "point_in_time", "copy_hours",
             "dry_run", "label", "extras"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise RestoreSpecError(
            f"Unknown key(s) {', '.join(unknown)}. Known: {', '.join(sorted(known))}."
        )

    db_type = _one_of(raw.get("db_type"), SUPPORTED_DB_TYPES, "db_type")
    databases = raw.get("databases") or []
    if isinstance(databases, str):
        raise RestoreSpecError("databases must be an array, not a string.")

    target = parse_target(raw.get("target"))
    source = parse_source(raw.get("source"))
    # An import directory is where the pieces land before RESTORE reads them. Without one a
    # remote copy has nowhere to go, and the failure would surface as a path error deep inside
    # the copy step rather than as the missing field it is.
    if source.access in {"smb", "ssh"} and not target.import_dir:
        raise RestoreSpecError(
            "target.import_dir is required when the backup has to be copied to the target "
            f"(source.access={source.access!r})."
        )
    return RestoreSpec(
        db_type=db_type,
        source=source,
        target=target,
        databases=tuple(str(name).strip() for name in databases if str(name).strip()),
        point_in_time=str(raw.get("point_in_time") or "").strip(),
        copy_hours=int(raw.get("copy_hours") or 24),
        dry_run=bool(raw.get("dry_run", False)),
        label=str(raw.get("label") or "").strip(),
        extras=dict(raw.get("extras") or {}),
    )


def redacted(spec: RestoreSpec) -> dict[str, Any]:
    """The spec as JSON with every password removed - what a result or a log may carry.

    Built here rather than by each caller so a new secret-bearing field cannot be added to the
    spec and forgotten in one of several redaction copies.
    """
    def _clean(obj: Any) -> dict[str, Any]:
        return {key: ("***" if key.endswith("password") and value else value)
                for key, value in vars(obj).items()}

    return {
        "db_type": spec.db_type,
        "label": spec.label,
        "source": _clean(spec.source),
        "target": _clean(spec.target),
        "databases": list(spec.databases),
        "point_in_time": spec.point_in_time,
        "copy_hours": spec.copy_hours,
        "dry_run": spec.dry_run,
    }
