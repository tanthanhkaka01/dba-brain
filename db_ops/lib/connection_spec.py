"""A database connection stated in full, so nothing has to be looked up.

The counterpart of ``common/backup/spec.py`` — *"everything a backup run needs, stated in the
request rather than looked up"* — for SQL. Same argument, one layer over: a run against a machine
that is in no inventory is then the same call as a scheduled one, which is exactly the situation a
real incident tends to be.

**Why this exists.** ``run-sql`` had one way in: name a ``target``, and the resolver reads
``db_instances.json`` for the host and ``users.json`` for the login. That is the right default and
it stays the default — a `server_id` is what a runbook and a scheduled task both carry. But it made
two things impossible. A host that is not in the inventory could not be reached at all, and every
caller inherited two file reads it may not have wanted: a Telegram action, a CI job, or another
tool holding its own connection details had to write them into ``db_instances.json`` first.

So a request may now carry a ``connection`` block instead, and when it does **no inventory file is
read**. The one thing that can still touch a file is the password, and only when the caller asks
for it by reference:

* ``"password": "..."`` — fully self-contained, nothing is read at all.
* ``"password_ref": "MSSQL_..."`` — resolved from the environment first, then the encrypted secret
  store. That is the *only* remaining read, it is material rather than config, and it is opt-in.

Nothing here opens anything or connects to anything: it validates the block and returns the same
shape ``sql_run.resolve_sqlserver_target`` returns, so everything downstream — ``connect_target``,
the legacy-bridge branch, the answer — is untouched by which of the two doors the caller came in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from db_ops.lib.sql_access import KNOWN_DB_TYPES, normalize_db_type
from db_ops.lib.target_profile import SOURCE_REQUEST, TargetProfile

__all__ = ["ConnectionSpec", "ConnectionSpecError"]


class ConnectionSpecError(ValueError):
    """The block does not describe a connection — an operator message naming the missing field."""


@dataclass(frozen=True)
class ConnectionSpec:
    """One database connection, complete. Build it with :meth:`from_json`, never field by field."""

    db_type: str
    host: str
    username: str
    port: int | None = None
    database: str = ""
    service_name: str = ""
    instance_name: str = ""
    password: str = ""
    password_ref: str = ""
    #: Labels only — they name the connection in the answer and in a log line. Nothing is looked
    #: up from them, which is the entire point of this shape.
    server_id: str = ""
    credential_name: str = ""
    credential_role: str = ""
    sqlserver_driver: str = ""
    oracle_client_mode: str = ""
    sql_access: dict[str, Any] = field(default_factory=lambda: {"method": "direct"})
    profile: TargetProfile = field(default_factory=TargetProfile)

    @classmethod
    def from_json(cls, payload: Any) -> "ConnectionSpec":
        """Parse and validate a ``connection`` block.

        Three fields are required and the rest have engine defaults, because those three are the
        ones no default can invent: **which engine**, **which machine**, **which login**. A block
        missing one of them is not an under-specified connection, it is a different target.
        """
        if not isinstance(payload, dict):
            raise ConnectionSpecError("connection must be a JSON object.")

        db_type = normalize_db_type(payload.get("db_type") or "")
        if not db_type:
            raise ConnectionSpecError(
                f"connection.db_type is required; expected one of {list(KNOWN_DB_TYPES)}."
            )
        if db_type not in KNOWN_DB_TYPES:
            raise ConnectionSpecError(
                f"connection.db_type {db_type!r} is not supported; expected one of "
                f"{list(KNOWN_DB_TYPES)}."
            )
        host = str(payload.get("host") or payload.get("ip") or "").strip()
        if not host:
            raise ConnectionSpecError("connection.host is required (an ip or hostname).")
        username = str(payload.get("username") or "").strip()
        if not username:
            raise ConnectionSpecError("connection.username is required.")

        password = str(payload.get("password") or "")
        password_ref = str(payload.get("password_ref") or "").strip()
        if not password and not password_ref:
            raise ConnectionSpecError(
                "connection needs a password: give \"password\" (nothing is read) or "
                "\"password_ref\" (resolved from the environment, then the secret store)."
            )

        port = payload.get("port")
        return cls(
            db_type=db_type,
            host=host,
            username=username,
            port=int(port) if str(port or "").strip() else None,
            database=str(payload.get("database") or payload.get("database_name") or "").strip(),
            service_name=str(payload.get("service_name") or "").strip(),
            instance_name=str(payload.get("instance_name") or "").strip(),
            password=password,
            password_ref=password_ref,
            server_id=str(payload.get("server_id") or payload.get("label") or host).strip(),
            credential_name=str(payload.get("credential_name") or "").strip(),
            credential_role=str(payload.get("role") or payload.get("credential_role") or "").strip(),
            sqlserver_driver=str(payload.get("sqlserver_driver") or payload.get("driver") or "").strip(),
            oracle_client_mode=str(payload.get("oracle_client_mode") or "").strip(),
            sql_access=dict(payload.get("sql_access") or {"method": "direct"}),
            # The version travels in the same block as the host it describes, so a self-contained
            # request is self-contained about the tool too — otherwise the caller would have to
            # state the connection here and the version somewhere else.
            profile=TargetProfile.from_json({**payload, "db_type": db_type}, source=SOURCE_REQUEST),
        )

    def credential(self) -> dict[str, Any]:
        """The credential shape ``lib.sql_text.resolve_password`` takes."""
        item: dict[str, Any] = {"username": self.username, "role": self.credential_role}
        if self.password_ref:
            item["password_ref"] = self.password_ref
        else:
            item["password"] = self.password
        return item

    def to_resolved(self, *, password: str, database: str = "", default_database: str = "") -> dict[str, Any]:
        """The resolved-target dict every caller downstream already reads.

        Deliberately the *same* dict `resolve_sqlserver_target` returns rather than a parallel
        shape: `connect_target`, the legacy-bridge branch and the answer builder should not be able
        to tell which door the request came in through, or each of them grows two code paths.
        """
        return {
            "server_id": self.server_id,
            "db_type": self.db_type,
            "ip": self.host,
            "port": self.port,
            "instance_name": self.instance_name,
            "service_name": self.service_name,
            "database_name": str(database or self.database or default_database or ""),
            "sqlserver_driver": self.sqlserver_driver,
            "oracle_client_mode": self.oracle_client_mode,
            "credential_name": self.credential_name or f"inline:{self.username}",
            "username": self.username,
            "password": password,
            "credential_role": self.credential_role,
            "sql_access": self.sql_access,
            "profile": self.profile,
        }
