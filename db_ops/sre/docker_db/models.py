"""Value objects and validation for SRE-provisioned database Docker instances.

A :class:`DockerDbSpec` fully describes a lab database instance (engine, version,
mode, port, password source) and knows how to validate itself against the rules
the CLI advertises. Engine-specific facts (image repo, default credentials,
in-container port, connection command) live in :data:`ENGINE_META` so the
templates, provisioner and connection registry all agree on one source of truth.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

VALID_ENGINES: tuple[str, ...] = ("postgres", "mysql", "mssql", "oracle", "oracle-xe")
VALID_MODES: tuple[str, ...] = ("single", "ha-lab")

# --name and --password-env must be shell/path/env safe: letters, digits, _ and -.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Environment variable names are conventionally [A-Za-z_][A-Za-z0-9_]*.
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# Host directory bind-mounted into a lab container at the SAME path, so a restore workflow and
# the engine mean the same file by the same string. Sits beside DEFAULT_CONTAINERS_DIR
# (/opt/db_ops/containers) rather than in data/*.json for the same reason that one does: it is
# the tool's own layout on a host it provisions, not an operator's choice about a target.
DEFAULT_BACKUP_MOUNT = "/opt/db_ops/backup"

# Every generated lab pins its own compose network to a /24 in this range — it is NEVER left to
# Docker's default address pool.
#
# Twice now an unpinned lab network has taken a production database off the map. Docker's default
# pool hands out 172.17.0.0/16 ... 172.31.0.0/16, and this estate has real hosts inside that space:
# on 2026-08-05 the db_ops network itself got 172.18.0.0/16 and the worker answered "No route to
# host" for a production SQL Server whose address sat inside that same /16; on 2026-08-14
# `ora11g_lab` was created, took the same /16, and the same
# SQL Server disappeared for two hours — the host routed it into a bridge instead of the LAN. The
# collision happens in the HOST route table, so pinning one project (db_ops was pinned to
# 172.30.240.0/24 after the first incident) protects nothing: any other project can still take the
# range. The only fix that holds is for every project to name its own subnet.
#
# 172.30.x is chosen because the worker's Docker daemon is configured to allocate from 172.31.0.0/16
# — so a pinned lab subnet cannot be handed to some other network behind its back. Reserved inside
# it: .0 (docker0's bip) and .240 (the db_ops runtime network), which is why the allocation window
# stops well short of both.
LAB_NETWORK_PREFIX = "172.30"
LAB_NETWORK_FIRST_OCTET = 1
LAB_NETWORK_LAST_OCTET = 200
_SUBNET_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")


def lab_network_subnet(name: str) -> str:
    """The /24 a lab called ``name`` gets, derived from the name itself.

    Deterministic so the same instance keeps its subnet across a re-provision, and so a dry-run
    prints the subnet the real run will use. Two names can collide; that surfaces as a loud
    ``docker compose up`` error ("Pool overlaps with other one on this address space") which the
    operator settles with ``--network-subnet``. A collision costing one failed create is the cheap
    failure — the expensive one is the silent hijack of a production route this function exists to
    prevent.
    """
    span = LAB_NETWORK_LAST_OCTET - LAB_NETWORK_FIRST_OCTET + 1
    digest = hashlib.sha1(str(name).encode("utf-8")).hexdigest()
    third = LAB_NETWORK_FIRST_OCTET + int(digest[:8], 16) % span
    return f"{LAB_NETWORK_PREFIX}.{third}.0/24"

# Enough for an engine that only has to open a port on first start (postgres, mysql, mssql).
DEFAULT_HEALTH_TIMEOUT = 180
DEFAULT_POST_START_TIMEOUT = 900

# The caller that gives us the least room: the Telegram `spbot_create_db_docker` command,
# whose poller SIGKILLs the process once its own timeout_seconds elapses. Every engine's
# health + post-start budget must finish inside that, with room left for the image pull and
# `compose up` — otherwise the run is killed mid-step and the operator gets a blunt "timed
# out" instead of the provisioner's own message saying which step failed and how to resume.
# Kept in sync by tests/test_docker_db_oracle_remote.py.
CALLER_BUDGET_SECONDS = 3600
PULL_AND_STARTUP_ALLOWANCE = 600


@dataclass(frozen=True)
class EngineMeta:
    """Engine-specific facts shared by the templates, provisioner and registry."""

    engine: str
    image_repo: str          # docker image repo; the tag is the --version value
    container_port: int      # the port the engine listens on *inside* the container
    username: str            # the superuser the image provisions
    database: str            # the default database created on first start
    password_env_key: str    # the env var the image itself reads for the password
    connect_template: str    # client connect hint; {host}/{port}/{username}/{database}
    supports_ha: bool = True
    # How long a first start may take before the provisioner gives up. This is a *ceiling*,
    # not a wait: the poll returns the moment every node is healthy, so a generous value only
    # costs time when something is genuinely wrong. It has to fit the slowest case — a first
    # start on an empty volume, with every node of an ha-lab coming up at once on one host.
    # Characters the engine's own first-start scripts cannot carry in a password. Not a
    # style rule — each one is a character that silently corrupts the password instead of
    # failing, leaving a database nobody can log into. See validate_password().
    forbidden_password_chars: str = ""
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT
    # How long the post-start step may run (the SQL Server AG build, the Oracle Data Guard
    # RMAN duplicate). Separate from the health wait because it is a different operation with
    # a different duration — tying them together made the duplicate inherit a timeout sized
    # for "has the container opened its port yet".
    post_start_timeout: int = DEFAULT_POST_START_TIMEOUT
    # Host path this engine's containers get bind-mounted at the identical path inside, or "" for
    # none. Only SQL Server carries one today: its restore workflow hands the engine a host path
    # in RESTORE ... FROM DISK. The PostgreSQL and Oracle restores move files by other means and
    # would gain an empty directory and nothing else.
    backup_mount: str = ""


ENGINE_META: dict[str, EngineMeta] = {
    "postgres": EngineMeta(
        engine="postgres",
        image_repo="postgres",
        container_port=5432,
        username="postgres",
        database="postgres",
        password_env_key="POSTGRES_PASSWORD",
        connect_template="psql -h {host} -p {port} -U {username} -d {database}",
    ),
    "mysql": EngineMeta(
        engine="mysql",
        image_repo="mysql",
        container_port=3306,
        username="root",
        database="testdb",
        password_env_key="MYSQL_ROOT_PASSWORD",
        connect_template="mysql -h {host} -P {port} -u {username} -p {database}",
    ),
    "mssql": EngineMeta(
        engine="mssql",
        image_repo="mcr.microsoft.com/mssql/server",
        container_port=1433,
        username="sa",
        database="master",
        password_env_key="MSSQL_SA_PASSWORD",
        connect_template="sqlcmd -S {host},{port} -U {username}",
        # A SQL Server lab is almost always a restore target, and RESTORE ... FROM DISK is read
        # by the ENGINE - so the backup files have to exist inside the container, at the same path
        # the restore workflow wrote them to on the host. Without a bind mount the restore fails
        # on a path that plainly exists when you look for it over SSH, which is a confusing hour.
        # Mounted at the identical path on both sides for exactly that reason: one string in
        # restore_config.json means the same file to the workflow and to the engine.
        backup_mount=DEFAULT_BACKUP_MOUNT,
    ),
    # Oracle Database Free via the gvenzl/oracle-free image: anonymous Docker Hub pulls
    # (the official container-registry.oracle.com image needs a login), sets the SYSTEM
    # password from ORACLE_PASSWORD, and ships a built-in HEALTHCHECK (healthcheck.sh).
    # --version is the image tag. NOTE: "Oracle AI Database 26ai" kept the 23.26.x version
    # numbers, so 26ai = tag 23.26.2 (or latest); there is NO '26'/'26ai' tag.
    # ha-lab = Data Guard, exactly 1 primary + 1 physical standby (Free supports Data Guard
    # since 23ai) — see the templates module's ORACLE_DG_SETUP.
    "oracle": EngineMeta(
        engine="oracle",
        image_repo="gvenzl/oracle-free",
        container_port=1521,
        username="system",
        database="FREEPDB1",
        password_env_key="ORACLE_PASSWORD",
        connect_template="sqlplus {username}@//{host}:{port}/{database}",
        supports_ha=True,
        # The image sets the password with `ALTER USER SYS IDENTIFIED BY "<pw>"` through
        # SQL*Plus, which expands `&name` as a substitution variable — so a password
        # containing `&` is silently replaced by whatever follows in the script, and the
        # database ends up with a password nobody knows. `"` closes the quoted identifier
        # in the same statement. Both fail *quietly*: the container starts, reports healthy,
        # and only the first real login says ORA-01017.
        forbidden_password_chars='&"',
        # Oracle is the outlier: on an empty volume the first start *creates the database*,
        # which takes many minutes — and an ha-lab creates two of them at once on one host.
        # The 180s that suffices for the other engines timed both nodes out before either had
        # finished initialising, so the Data Guard step was never reached.
        health_timeout=1500,
        # The RMAN active duplicate copies the whole database across the two containers.
        post_start_timeout=1500,
    ),
    # Oracle Database **Express Edition** via gvenzl/oracle-xe — the only way to get 11g R2 in a
    # container. It is a separate engine rather than a tag of "oracle" because almost nothing
    # about it matches Free 23ai: a different image, a different service name, non-CDB, and no
    # Data Guard. Squeezing it into the other entry would mean the wrong service in every
    # generated connection string.
    #
    # 11g R2 XE is **x86-64 only**. Oracle never shipped a Linux x86 (32-bit) build of 11.2 XE —
    # the 32-bit Express Edition was 10.2. The image manifest is `architecture: amd64`, so there
    # is no 32-bit path here and none to add.
    #
    # Tags: `11` = 11.2.0.2, `18`/`21` are the later XE releases. The service name below is
    # 11g's: XE is a **non-CDB**, so it serves SID/service `XE` directly and has no PDB. On 18c
    # and later XE the pluggable database is `XEPDB1` — pass `--version 21` only if you also
    # mean to connect to XEPDB1, which is why `11` is the documented default here.
    "oracle-xe": EngineMeta(
        engine="oracle-xe",
        image_repo="gvenzl/oracle-xe",
        container_port=1521,
        username="system",
        # Non-CDB: the service IS the database. Not FREEPDB1/XEPDB1.
        database="XE",
        password_env_key="ORACLE_PASSWORD",
        connect_template="sqlplus {username}@//{host}:{port}/{database}",
        # 11g XE has no Data Guard (it is an Enterprise feature, and XE is the most cut-down
        # edition there is). Free 23ai supports it, which is why that engine does and this
        # one does not.
        supports_ha=False,
        # Same first-start mechanism as the Free image — it sets the password through SQL*Plus,
        # where `&` starts a substitution variable and `"` closes a quoted identifier. Both fail
        # silently: the container reports healthy and the first real login says ORA-01017.
        forbidden_password_chars='&"',
        # Creating an 11g XE database on an empty volume is far quicker than Free 23ai (the XE
        # image ships a seed database), but it is still minutes rather than seconds.
        health_timeout=600,
        # post_start_timeout is deliberately left at the default: with no ha-lab there is no
        # post-start step to budget for. Setting one — as the first draft did, by copying the
        # health timeout — invents a number for work that never runs.
    ),
}

# Engines whose HA lab is implemented.
#
# "ha-lab" is each engine's own replication, not one product: PostgreSQL gets physical
# streaming replication, MySQL asynchronous primary/replica, SQL Server an Always On
# availability group with CLUSTER_TYPE = NONE (no WSFC/Pacemaker exists in a container lab, so
# failover is manual and there is no listener — see the mssql ha-lab compose header).
HA_SUPPORTED_ENGINES: tuple[str, ...] = tuple(
    name for name, meta in ENGINE_META.items() if meta.supports_ha
)


@dataclass(frozen=True)
class DockerDbSpec:
    """A validated description of one lab database Docker instance."""

    name: str
    engine: str
    version: str
    mode: str = "single"
    replicas: int = 2
    host_port: int = 0
    password_env: str = ""
    #: Host directory bind-mounted into every node at the same path. ``None`` takes the engine's
    #: default; ``""`` is an explicit "no mount" a caller can ask for.
    backup_mount: str | None = None
    #: The compose network's subnet. Empty takes :func:`lab_network_subnet`; an explicit value is
    #: how an operator settles a collision, or keeps a lab off a range their site already routes.
    network_subnet: str = ""

    @property
    def meta(self) -> EngineMeta:
        return ENGINE_META[self.engine]

    @property
    def resolved_network_subnet(self) -> str:
        """The subnet this instance's compose network is pinned to — never empty."""
        return str(self.network_subnet).strip() or lab_network_subnet(self.name)

    @property
    def resolved_backup_mount(self) -> str:
        """The bind mount this instance actually gets."""
        return self.meta.backup_mount if self.backup_mount is None else str(self.backup_mount)

    @property
    def is_ha(self) -> bool:
        return self.mode == "ha-lab"

    def node_names(self) -> list[str]:
        """Container/service names for this instance.

        Single mode is one node named ``<name>``. HA lab is a primary plus
        ``replicas`` standbys: ``<name>-primary``, ``<name>-standby-1`` ...
        """
        if not self.is_ha:
            return [self.name]
        nodes = [f"{self.name}-primary"]
        nodes += [f"{self.name}-standby-{i}" for i in range(1, self.replicas + 1)]
        return nodes

    def validate(self, *, replicas_explicit: bool = False) -> None:
        """Raise ``ValueError`` with a clear message if the spec is invalid.

        Enforces validation rules 1-5 and 8. Port-in-use (6) and folder-exists
        (7) are runtime checks the provisioner performs against the worker.
        """
        if not self.name or not _NAME_RE.match(self.name):
            raise ValueError(
                f"Invalid --name '{self.name}': only letters, numbers, underscore and dash are allowed."
            )
        if self.engine not in VALID_ENGINES:
            raise ValueError(
                f"Invalid --engine '{self.engine}': choose one of {', '.join(VALID_ENGINES)}."
            )
        if self.mode not in VALID_MODES:
            raise ValueError(
                f"Invalid --mode '{self.mode}': choose one of {', '.join(VALID_MODES)}."
            )
        if not str(self.version).strip():
            raise ValueError("--version is required (e.g. 16, 8.4, 2022-latest).")
        # Rule 4: --replicas is only meaningful for ha-lab.
        if not self.is_ha and replicas_explicit:
            raise ValueError("--replicas is only valid with --mode ha-lab.")
        # Rule 5: mssql has no ha-lab yet.
        if self.is_ha and not self.meta.supports_ha:
            raise ValueError(
                f"--engine {self.engine} does not support --mode ha-lab yet; use --mode single."
            )
        if self.is_ha and self.replicas < 1:
            raise ValueError("--replicas must be at least 1 for ha-lab mode.")
        # Oracle ha-lab is Data Guard with exactly one physical standby (1/1): more standbys
        # would need per-standby dest/FAL wiring the lab script deliberately does not carry.
        if self.is_ha and self.engine == "oracle" and self.replicas != 1:
            raise ValueError("--engine oracle ha-lab is Data Guard 1 primary + 1 standby; --replicas must be 1.")
        # Rule 8: the password must come from a named env var / secret ref, never inline.
        if not self.password_env or not _ENV_RE.match(self.password_env):
            raise ValueError(
                "--password-env is required and must be a valid environment variable name "
                "(the password is read from that env var or the secret store, never hardcoded)."
            )
        if not (1 <= int(self.host_port) <= 65535):
            raise ValueError(f"--host-port must be between 1 and 65535 (got {self.host_port}).")
        subnet = str(self.network_subnet).strip()
        if subnet and not _SUBNET_RE.match(subnet):
            raise ValueError(
                f"--network-subnet must be a CIDR block like 172.30.42.0/24 (got '{subnet}')."
            )
