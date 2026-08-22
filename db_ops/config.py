from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module


DEFAULT_CONFIG_PATH = TOOL_ROOT / "config.json"

# Telegram settings live in their own file under data/ so they can be edited (and
# the update_offset persisted) without touching the shared config.json.
DEFAULT_TELEGRAM_CONFIG_FILE = "data/telegram_config.json"

# Which database db_ops stores its own runtime data in (job runs, metric results, telegram
# messages, reports, SLA runs, backup/restore history). Declared in its own file under data/
# so the answer is in one readable place instead of implied by a bare ``sqlite_path`` key.
DEFAULT_STORE_CONFIG_FILE = "data/store_config.json"
STORE_CONFIG_ENV_VAR = "DB_OPS_STORE_CONFIG"

SQLITE_BACKEND = "sqlite"
POSTGRESQL_BACKEND = "postgresql"
STORE_BACKENDS = (SQLITE_BACKEND, POSTGRESQL_BACKEND)

# Per-app config file names searched next to the shared config or in cwd.
APP_CONFIG_NAMES: dict[str, str] = {
    "backup_restore": "config.backup_restore.json",
    "jobs": "config.jobs.json",
    "metrics": "config.metrics.json",
    "reports": "config.reports.json",
    "sla": "config.sla.json",
    "sql_tasks": "config.sql_tasks.json",
    "sre": "data/sre_config.json",
    "telegram": "config.telegram.json",
}

# Per-app environment variables that override the config path.
APP_CONFIG_ENV_VARS: dict[str, str] = {
    "backup_restore": "DB_OPS_BACKUP_RESTORE_CONFIG",
    "jobs": "DB_OPS_JOBS_CONFIG",
    "metrics": "DB_OPS_METRICS_CONFIG",
    "reports": "DB_OPS_REPORTS_CONFIG",
    "sla": "DB_OPS_SLA_CONFIG",
    "sql_tasks": "DB_OPS_SQL_TASKS_CONFIG",
    "sre": "DB_OPS_SRE_CONFIG",
    "telegram": "DB_OPS_TELEGRAM_CONFIG",
}


def resolve_config_path(app_name: str, cli_path: str | Path | None = None) -> Path:
    """Resolve config path for an app using this priority chain:

    1. Explicit CLI argument (``--config``).
    2. Per-app environment variable (e.g. ``DB_OPS_METRICS_CONFIG``).
    3. App-specific config file (e.g. ``config.metrics.json``) next to the
       shared ``config.json`` or in the current working directory.
    4. Shared ``config.json`` fallback.

    The resolved source is printed to stderr so startup is traceable without
    requiring a logger (which needs the config to be loaded first).
    """
    # 1. Explicit CLI argument.
    if cli_path is not None:
        path = Path(cli_path)
        _print_config_source(app_name, "cli", path)
        return path

    # 2. Per-app environment variable.
    env_var = APP_CONFIG_ENV_VARS.get(app_name, "")
    if env_var:
        env_value = os.environ.get(env_var, "").strip()
        if env_value:
            path = Path(env_value)
            _print_config_source(app_name, f"env:{env_var}", path)
            return path

    # 3. App-specific config file.
    app_config_name = APP_CONFIG_NAMES.get(app_name, "")
    if app_config_name:
        # Search next to the shared config first (in-repo / installed layout).
        candidate = DEFAULT_CONFIG_PATH.parent / app_config_name
        if candidate.exists():
            _print_config_source(app_name, "app_config", candidate)
            return candidate
        # Then the current working directory (standalone EXE layout).
        cwd_candidate = Path.cwd() / app_config_name
        if cwd_candidate.exists():
            _print_config_source(app_name, "app_config_cwd", cwd_candidate)
            return cwd_candidate

    # 4. Shared config fallback.
    _print_config_source(app_name, "shared_fallback", DEFAULT_CONFIG_PATH)
    return DEFAULT_CONFIG_PATH


def _print_config_source(app_name: str, source: str, path: Path) -> None:
    print(f"[db_ops.config] app={app_name} source={source} config={path}", file=sys.stderr)


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    bot_config_file: Path | None = None
    secret_text_file: Path | None = None
    telegram_bot_token_ref: str = ""
    telegram_bot_id: str = ""
    telegram_bot_username: str = ""
    bot_token_secret_file: Path | None = None
    bot_token: str = ""
    api_url: str = "https://api.telegram.org"
    timeout_seconds: int = 20
    update_offset: int | None = None
    groups_file: Path | None = None
    # level -> chat_id. **The** routing table: a level with a chat sends there, a level with
    # no chat does not send. There is no second list to keep in sync — an `alert_levels`
    # allow-list used to gate this as well, which meant every level name had to be spelled
    # twice and a typo muted a level silently (and did, for `private`). Mute a level by
    # clearing its chat: blank the group's `notify_level`, or take the group out of `active`.
    level_chat_map: dict[str, str] = field(default_factory=dict)

    @property
    def resolved_bot_token(self) -> str:
        env_token = os.getenv(self.bot_token_env, "").strip() if self.bot_token_env else ""
        if env_token:
            return env_token

        secret_text_token = self._resolve_secret_text_token()
        if secret_text_token:
            return secret_text_token

        if self.bot_token_secret_file and self.bot_token_secret_file.exists():
            with self.bot_token_secret_file.open("r", encoding="utf-8-sig") as file:
                data = json.load(file)
            secret_token = str(data.get("telegram_bot_token", "")).strip()
            if secret_token:
                return secret_token

        return self.bot_token.strip()

    def _resolve_secret_text_token(self) -> str:
        if not self.telegram_bot_token_ref:
            return ""
        if not self.secret_text_file:
            return ""

        # Read from the data dir's encrypted_secret_text.json (decrypted with
        # DB_OPS_SECRET_KEY). Encrypted-only: there is no plaintext fallback.
        from db_ops.lib import secret_text

        data = secret_text.load_secret_text(self.secret_text_file.parent)
        if not data:
            return ""
        secret_token = str(data.get(self.telegram_bot_token_ref, "")).strip()
        if not secret_token:
            raise RuntimeError(
                f"Telegram bot token ref '{self.telegram_bot_token_ref}' was not found in secret text "
                f"under {self.secret_text_file.parent}."
            )
        return secret_token


@dataclass(frozen=True)
class SqliteStoreConfig:
    """The SQLite side of ``data/store_config.json``."""
    path: Path = Path("runtime/db_ops.sqlite")

    @property
    def connection_string(self) -> str:
        """SQLAlchemy-style URL for the store file. Safe to log — a file path holds no secret."""
        return f"sqlite:///{self.path.as_posix()}"

    def resolved_connection_string(self, key: str | None = None) -> str:  # noqa: ARG002 - uniform API
        return self.connection_string


@dataclass(frozen=True)
class PostgresStoreConfig:
    """The PostgreSQL side of ``data/store_config.json``.

    ``connection_string`` here is the password-free form: the value read from the config file
    when it set one, otherwise built from the fields below. ``resolved_connection_string()``
    is the only thing that ever carries the password, pulled from the encrypted secret store
    via ``password_ref`` — so the safe string is what gets logged and the resolved one is what
    gets connected with.
    """
    host: str = ""
    port: int = 5432
    database: str = ""
    schema: str = ""
    username: str = ""
    password_ref: str = ""
    credential_name: str = ""
    sslmode: str = "prefer"
    connect_timeout_seconds: int = 10
    application_name: str = "db_ops"
    # Verbatim value from the config file, "" when it left the key blank.
    explicit_connection_string: str = ""
    secret_text_dir: Path | None = None

    @property
    def connection_string(self) -> str:
        if self.explicit_connection_string:
            return self.explicit_connection_string
        return self._build_connection_string()

    def _build_connection_string(self) -> str:
        from urllib.parse import quote

        user = quote(self.username, safe="") if self.username else ""
        authority = f"{user}:{{password}}@" if user else ""
        host = self.host or "localhost"
        params = [f"sslmode={self.sslmode or 'prefer'}"]
        if self.connect_timeout_seconds:
            params.append(f"connect_timeout={int(self.connect_timeout_seconds)}")
        if self.application_name:
            params.append(f"application_name={quote(self.application_name, safe='')}")
        if self.schema:
            params.append(f"options={quote(f'-csearch_path={self.schema}', safe='')}")
        return f"postgresql://{authority}{host}:{int(self.port)}/{self.database}?{'&'.join(params)}"

    def resolved_password(self, key: str | None = None) -> str:
        """Decrypt this store's password from ``data/encrypted_secret_text.json``.

        A ``password_ref`` that is configured but absent from the store raises: a store the
        app cannot authenticate to must fail loudly, not connect as nobody.
        """
        if not self.password_ref:
            return ""
        if self.secret_text_dir is None:
            raise RuntimeError(
                f"Store password ref '{self.password_ref}' cannot be resolved: no secret-text "
                "directory was resolved for the store config."
            )

        from db_ops.lib import secret_text

        secrets = secret_text.load_secret_text(self.secret_text_dir, key=key)
        password = str(secrets.get(self.password_ref, "")).strip()
        if not password:
            raise RuntimeError(
                f"Store password ref '{self.password_ref}' was not found in the secret text under "
                f"{self.secret_text_dir}."
            )
        return password

    def resolved_connection_string(self, key: str | None = None) -> str:
        """``connection_string`` with the real password substituted in. Never log this."""
        from urllib.parse import quote

        template = self.connection_string
        if "{password}" not in template:
            return template
        return template.replace("{password}", quote(self.resolved_password(key), safe=""))


@dataclass(frozen=True)
class StoreConfig:
    """Where db_ops keeps its *own* data — the answer to "sqlite or postgres?".

    Loaded from ``data/store_config.json``. ``backend`` names the section that is live; the
    other one stays in the file as a prepared, fully-specified target so switching is a
    one-word edit rather than a rediscovery of host/port/credentials.
    """
    backend: str = SQLITE_BACKEND
    sqlite: SqliteStoreConfig = field(default_factory=SqliteStoreConfig)
    postgresql: PostgresStoreConfig = field(default_factory=PostgresStoreConfig)
    # Where the declaration came from; None when no store config file was found and the
    # legacy config.json ``sqlite_path`` supplied the answer.
    config_file: Path | None = None

    @property
    def is_sqlite(self) -> bool:
        return self.backend == SQLITE_BACKEND

    @property
    def is_postgresql(self) -> bool:
        return self.backend == POSTGRESQL_BACKEND

    @property
    def active(self) -> SqliteStoreConfig | PostgresStoreConfig:
        return self.postgresql if self.is_postgresql else self.sqlite

    @property
    def connection_string(self) -> str:
        """Password-free connection string for the *active* backend. Safe to log/report."""
        return self.active.connection_string

    def resolved_connection_string(self, key: str | None = None) -> str:
        """Connection string for the active backend with the password filled in."""
        return self.active.resolved_connection_string(key)

    def describe(self) -> dict[str, Any]:
        """Serializable summary for status output. Carries the safe string only."""
        return {
            "backend": self.backend,
            "connection_string": self.connection_string,
            "config_file": str(self.config_file) if self.config_file else "",
            "sqlite_path": str(self.sqlite.path),
        }

    def validate(self) -> None:
        """Refuse a store declaration that cannot be connected with.

        Both backends are supported by the store layer (see :mod:`db_ops.db.backend`), so this
        only checks that the *selected* section is complete enough to open. It used to reject
        ``postgresql`` outright, back when ``DbOpsStore`` and the metrics/SLA stores spoke SQLite
        only; that refusal is gone now that they run on either backend.
        """
        if self.backend not in STORE_BACKENDS:
            raise RuntimeError(
                f"Unknown store backend '{self.backend}'. Use one of: {', '.join(STORE_BACKENDS)}."
            )
        if self.is_sqlite:
            if not str(self.sqlite.path):
                raise RuntimeError("Store backend 'sqlite' requires a sqlite.path (or connection_string).")
            return

        postgres = self.postgresql
        if not postgres.explicit_connection_string and not (postgres.host and postgres.database):
            raise RuntimeError(
                "Store backend 'postgresql' needs either a connection_string or host + database "
                f"in {self.config_file or DEFAULT_STORE_CONFIG_FILE}."
            )


@dataclass(frozen=True)
class ClusterNode:
    """One node in the db_ops master/worker cluster."""
    node_id: str
    host: str = ""
    description: str = ""
    user: str = ""  # default SSH username for master->this-node operations (control app)
    password_ref: str = ""  # secret_text ref for this node's SSH password (control app auto-resolves it)


@dataclass(frozen=True)
class DbOpsConfig:
    app_name: str = "db_ops"
    log_dir: Path = Path("logs")
    runtime_dir: Path = Path("runtime")
    # The store db_ops writes its own data to, declared in data/store_config.json.
    # ``sqlite_path`` stays as the field every app already reads; it is now derived from
    # ``store`` rather than being the declaration itself.
    store: StoreConfig = field(default_factory=StoreConfig)
    sqlite_path: Path = Path("runtime/db_ops.sqlite")
    console_level: str = "INFO"
    file_level: str = "INFO"
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    # Master/worker cluster. master/worker are the cluster definition (same on every
    # node, shipped in config.json); node_role is THIS node's role, resolved per-node
    # (env DB_OPS_NODE_ROLE wins, so the deployed worker is not mislabelled by the
    # copied config.json).
    master: tuple[ClusterNode, ...] = ()
    worker: tuple[ClusterNode, ...] = ()
    node_role: str = "master"

    def __post_init__(self) -> None:
        """Keep ``sqlite_path`` and ``store`` from ever disagreeing.

        :func:`parse_config` derives one from the other, but ``DbOpsConfig`` is also constructed
        directly - by tests and by standalone callers - and there both fields default independently.
        A caller passing only ``sqlite_path=<tmp>`` would get a ``store`` still pointing at the
        tool's default location, so ``DbOpsStore.from_config()`` quietly opened a different database
        than ``DbOpsStore(config.sqlite_path)``. Two settable fields describing one destination is
        exactly the kind of pair that drifts, so the explicit one wins and the other is rebuilt from
        it.

        Only meaningful for a SQLite store: when the declared backend is PostgreSQL the path is
        informational and is left as-is.
        """
        if self.store.is_sqlite and Path(self.sqlite_path) != Path(self.store.sqlite.path):
            object.__setattr__(
                self,
                "store",
                replace(self.store, sqlite=SqliteStoreConfig(path=Path(self.sqlite_path))),
            )


def _parse_cluster_nodes(raw_list: Any) -> tuple[ClusterNode, ...]:
    nodes: list[ClusterNode] = []
    for item in (raw_list or []):
        if isinstance(item, dict):
            host = str(item.get("host") or item.get("ip") or "").strip()
            node_id = str(item.get("node_id") or item.get("id") or host).strip()
            if node_id:
                nodes.append(ClusterNode(node_id=node_id, host=host,
                                         description=str(item.get("description", "")),
                                         user=str(item.get("user") or item.get("ssh_user") or "").strip(),
                                         password_ref=str(item.get("password_ref") or item.get("password_env") or "").strip()))
        elif isinstance(item, str) and item.strip():
            nodes.append(ClusterNode(node_id=item.strip(), host=item.strip()))
    return tuple(nodes)


def _resolve_node_role() -> str:
    """This node's role, declared at run time by the ``DB_OPS_NODE_ROLE`` env — the same
    mechanism on every node (e.g. ``docker ... -e DB_OPS_NODE_ROLE=worker`` on the worker,
    ``-e DB_OPS_NODE_ROLE=master`` on the master). With no env it defaults to ``master``.
    No host autodetection and no role baked into the shared config, so a config.json copied
    between nodes can never mislabel one."""
    env_role = os.getenv("DB_OPS_NODE_ROLE", "").strip().lower()
    if env_role in ("master", "worker"):
        return env_role
    return "master"


def load_config(path: str | Path | None = None) -> DbOpsConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        example = config_path.with_name("config.example.json")
        raise FileNotFoundError(f"Config file not found: {config_path}. Create it from {example}.")

    with config_path.open("r", encoding="utf-8-sig") as file:
        raw = json.load(file)

    return parse_config(raw, base_dir=config_path.parent)


def parse_config(raw: dict[str, Any], *, base_dir: Path) -> DbOpsConfig:
    telegram_raw = _load_telegram_settings(raw, base_dir=base_dir)
    bot_config_file = _resolve_optional_path(
        telegram_raw.get("bot_config_file", "data/bot_telegram.json"),
        base_dir=base_dir,
    )
    bot_config = _load_telegram_bot_config(bot_config_file)
    secret_text_file = _resolve_optional_path(
        telegram_raw.get("secret_text_file", "data/secret_text.json"),
        base_dir=base_dir,
    )
    bot_token_secret_file = _resolve_optional_path(
        telegram_raw.get("bot_token_secret_file"),
        base_dir=base_dir,
    )
    groups_file = _resolve_optional_path(
        telegram_raw.get("groups_file"),
        base_dir=base_dir,
    )
    level_chat_map = _load_telegram_groups_file(groups_file)
    # The settings file may override/extend the map from the groups file — that is where a
    # chat which is not a group lives (a private DM has a positive chat_id, so it cannot be
    # declared as a group). `groups` is the old spelling of the same object, still read so a
    # settings file that predates the rename keeps its routes instead of silently losing them.
    overrides = telegram_raw.get("level_chat_map")
    if overrides is None:
        overrides = telegram_raw.get("groups")
    level_chat_map.update(
        {str(k).lower(): str(v) for k, v in (overrides or {}).items() if str(v).strip()}
    )

    telegram = TelegramConfig(
        enabled=bool(telegram_raw.get("enabled", False)),
        bot_token_env=str(telegram_raw.get("bot_token_env", "TELEGRAM_BOT_TOKEN")),
        bot_config_file=bot_config_file,
        secret_text_file=secret_text_file,
        telegram_bot_token_ref=str(
            telegram_raw.get("telegram_bot_token_ref") or bot_config.get("telegram_bot_token_ref", "")
        ),
        telegram_bot_id=str(telegram_raw.get("telegram_bot_id") or bot_config.get("telegram_bot_id", "")),
        telegram_bot_username=str(
            telegram_raw.get("telegram_bot_username") or bot_config.get("telegram_bot_username", "")
        ),
        bot_token_secret_file=bot_token_secret_file,
        bot_token=str(telegram_raw.get("bot_token", "")),
        api_url=str(telegram_raw.get("api_url", "https://api.telegram.org")),
        timeout_seconds=int(telegram_raw.get("timeout_seconds", 20)),
        update_offset=_parse_optional_int(telegram_raw.get("update_offset")),
        groups_file=groups_file,
        level_chat_map=level_chat_map,
    )

    log_dir = _resolve_path_setting(raw, "log_dir", "logs", base_dir=base_dir)
    runtime_dir = _resolve_path_setting(raw, "runtime_dir", "runtime", base_dir=base_dir)
    store = _load_store_config(
        raw,
        base_dir=base_dir,
        explicit_sqlite_path=_explicit_path_setting(raw, "sqlite_path", base_dir=base_dir),
    )
    store.validate()
    # One value, one source: apps keep reading ``sqlite_path``, and it is whatever the store
    # declaration resolved to, so the two can never point at different files.
    sqlite_path = store.sqlite.path

    master = _parse_cluster_nodes(raw.get("master"))
    worker = _parse_cluster_nodes(raw.get("worker"))
    node_role = _resolve_node_role()

    return DbOpsConfig(
        app_name=str(raw.get("app_name", "db_ops")),
        log_dir=log_dir,
        runtime_dir=runtime_dir,
        store=store,
        sqlite_path=sqlite_path,
        console_level=str(raw.get("console_level", "INFO")).upper(),
        file_level=str(raw.get("file_level", "INFO")).upper(),
        telegram=telegram,
        master=master,
        worker=worker,
        node_role=node_role,
    )


def resolve_store_config_path(
    base_dir: Path | None = None, raw: dict[str, Any] | None = None
) -> Path:
    """Resolve the store declaration file (``data/store_config.json`` by default).

    ``DB_OPS_STORE_CONFIG`` wins, then the config's own ``store_config_file`` pointer — so a
    test or a side-install can name a different store without editing the shipped data folder.
    Same tool-root fallback as the telegram settings: the value is written relative to the tool
    root, so a config living under ``data/`` still finds it instead of looking in ``data/data``.
    """
    env_value = os.environ.get(STORE_CONFIG_ENV_VAR, "").strip()
    if env_value:
        return Path(env_value)
    rel = str((raw or {}).get("store_config_file") or DEFAULT_STORE_CONFIG_FILE)
    return _resolve_optional_path(
        rel, base_dir=base_dir or DEFAULT_CONFIG_PATH.parent
    ) or (TOOL_ROOT / rel)


def _load_store_config(
    raw: dict[str, Any], *, base_dir: Path, explicit_sqlite_path: Path | None
) -> StoreConfig:
    """Build the :class:`StoreConfig` from ``data/store_config.json``.

    Three inputs, in priority order:

    1. ``sqlite_path`` set explicitly in the config being loaded. Still supported and still
       documented (the standalone-EXE layouts point it at a local absolute path), so it
       overrides the store file's path — an explicit value always wins, the same rule
       :func:`_resolve_path_setting` follows.
    2. An inline ``store`` block in the main config, merged key-by-key over the file — the same
       file-plus-inline-override shape the telegram settings use, so a temporary local switch
       does not mean editing the deployed data folder.
    3. ``data/store_config.json`` — the declaration: which backend, and the full connection
       details for it.

    With none of the three, the SQLite store falls back to the tool's standard location. That is
    what keeps a tree deployed before this file existed working unchanged.
    """
    default_path = explicit_sqlite_path or (TOOL_ROOT / "runtime/db_ops.sqlite").resolve()
    store_path = resolve_store_config_path(base_dir, raw)
    file_settings = _load_json_object(store_path if store_path.exists() else None)
    inline = raw.get("store")
    settings: dict[str, Any] = dict(file_settings)
    if isinstance(inline, dict):
        settings.update(inline)

    if not settings:
        return StoreConfig(sqlite=SqliteStoreConfig(path=default_path), config_file=None)

    backend = str(settings.get("backend", SQLITE_BACKEND)).strip().lower()
    if backend == "postgres":  # accepted alias, same as data_sources.CREDENTIAL_DB_TYPES
        backend = POSTGRESQL_BACKEND

    sqlite = (
        SqliteStoreConfig(path=explicit_sqlite_path)
        if explicit_sqlite_path
        else _parse_sqlite_store(settings.get(SQLITE_BACKEND), fallback_path=default_path)
    )
    return StoreConfig(
        backend=backend,
        sqlite=sqlite,
        postgresql=_parse_postgres_store(settings.get(POSTGRESQL_BACKEND), store_dir=store_path.parent),
        config_file=store_path if file_settings else None,
    )


def _parse_sqlite_store(raw: Any, *, fallback_path: Path) -> SqliteStoreConfig:
    if not isinstance(raw, dict):
        return SqliteStoreConfig(path=fallback_path)
    # connection_string wins when set, and the path is read back out of it, so the file can
    # never show a URL and a path that disagree.
    connection_string = str(raw.get("connection_string", "")).strip()
    path_value = _sqlite_path_from_url(connection_string) if connection_string else str(raw.get("path", "")).strip()
    if not path_value:
        return SqliteStoreConfig(path=fallback_path)
    path = Path(path_value)
    # Relative to the tool root, not to data/ — see _resolve_path_setting's note on why a
    # config living under data/ must not default its runtime into data/runtime.
    return SqliteStoreConfig(path=path if path.is_absolute() else (TOOL_ROOT / path).resolve())


def _sqlite_path_from_url(connection_string: str) -> str:
    """Extract the file path from a ``sqlite:///`` URL (a bare path is returned as-is).

    ``sqlite:///runtime/db_ops.sqlite`` is relative, ``sqlite:////var/lib/x.sqlite`` and
    ``sqlite:///C:/x.sqlite`` are absolute — one leading slash belongs to the URL form, the
    rest is the path.
    """
    if "://" not in connection_string:
        return connection_string
    rest = connection_string.split("://", 1)[1]
    return rest[1:] if rest.startswith("/") else rest


def _parse_postgres_store(raw: Any, *, store_dir: Path) -> PostgresStoreConfig:
    if not isinstance(raw, dict):
        return PostgresStoreConfig(secret_text_dir=store_dir)
    return PostgresStoreConfig(
        host=str(raw.get("host", "")).strip(),
        port=int(raw.get("port") or 5432),
        database=str(raw.get("database", "")).strip(),
        schema=str(raw.get("schema", "")).strip(),
        username=str(raw.get("username", "")).strip(),
        password_ref=str(raw.get("password_ref") or raw.get("password_env") or "").strip(),
        credential_name=str(raw.get("credential_name", "")).strip(),
        sslmode=str(raw.get("sslmode", "prefer")).strip() or "prefer",
        connect_timeout_seconds=int(raw.get("connect_timeout_seconds") or 10),
        application_name=str(raw.get("application_name", "db_ops")).strip(),
        explicit_connection_string=str(raw.get("connection_string", "")).strip(),
        secret_text_dir=store_dir,
    )


def _resolve_path_setting(raw: dict[str, Any], key: str, default: str, *, base_dir: Path) -> Path:
    """Resolve log_dir / runtime_dir / sqlite_path.

    An explicit value is relative to the config file that declares it; an omitted one falls back
    to the tool root. Without that split, a config living under ``data/`` defaulted its runtime
    to ``data/runtime`` and its database to ``data/runtime/db_ops.sqlite`` — which is why those
    files carried ``"../runtime"`` workarounds just to point back at the real locations. Leaving
    the key out now means "the tool's standard location", and an explicit value still wins so a
    test or a side-install can put them somewhere else.
    """
    value = raw.get(key)
    if value is None or str(value).strip() == "":
        return (TOOL_ROOT / default).resolve()
    path = Path(str(value))
    return path if path.is_absolute() else (base_dir / path).resolve()


def _explicit_path_setting(raw: dict[str, Any], key: str, *, base_dir: Path) -> Path | None:
    """Same resolution as :func:`_resolve_path_setting`, but ``None`` when the key is absent.

    The store loader has to tell "this config names a path" apart from "this config says
    nothing" — the first overrides ``data/store_config.json``, the second defers to it.
    """
    value = raw.get(key)
    if value is None or str(value).strip() == "":
        return None
    path = Path(str(value))
    return path if path.is_absolute() else (base_dir / path).resolve()


def _resolve_optional_path(value: Any, *, base_dir: Path) -> Path | None:
    """Resolve a config-relative file path, falling back to the tool root.

    ``base_dir`` is the config file's own folder, which is right for a config at the tool root
    but wrong for one that lives deeper: ``data/restore_config.json`` resolved the standard
    ``"data/telegram_config.json"`` to ``data/data/telegram_config.json``, found nothing, and
    silently fell back to empty telegram settings — which is why that file ended up restating
    the whole telegram block inline just to work around the path. These values are written
    relative to the tool root, so try there before giving up.
    """
    if value is None or str(value).strip() == "":
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    candidate = (base_dir / path).resolve()
    if candidate.exists():
        return candidate
    fallback = (TOOL_ROOT / path).resolve()
    # Neither exists: return the base_dir candidate so error messages still name the
    # location the config actually asked for.
    return fallback if fallback.exists() else candidate


def resolve_telegram_config_path(config_path: str | Path, raw: dict[str, Any] | None = None) -> Path:
    """Resolve the telegram settings file (``data/telegram_config.json`` by default)
    given the main config path. Used both to read settings and to persist
    ``update_offset`` back to the right file."""
    config_path = Path(config_path)
    base_dir = config_path.parent
    if raw is None:
        raw = {}
        if config_path.exists():
            with config_path.open("r", encoding="utf-8-sig") as file:
                loaded = json.load(file)
            if isinstance(loaded, dict):
                raw = loaded
    rel = str(raw.get("telegram_config_file") or DEFAULT_TELEGRAM_CONFIG_FILE)
    # Same tool-root fallback as _resolve_optional_path: reads and update_offset writes must
    # land on the same file, whatever folder the calling config lives in.
    return _resolve_optional_path(rel, base_dir=base_dir) or (base_dir / rel).resolve()


def _load_telegram_settings(raw: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    """Load the telegram settings block from the external telegram config file,
    letting an inline ``telegram`` block in the main config override it."""
    telegram_config_file = _resolve_optional_path(
        raw.get("telegram_config_file", DEFAULT_TELEGRAM_CONFIG_FILE),
        base_dir=base_dir,
    )
    file_settings = _load_json_object(telegram_config_file)
    inline = raw.get("telegram")
    if isinstance(inline, dict):
        return {**file_settings, **inline}
    return file_settings


def _load_json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise RuntimeError(f"Config file must be a JSON object: {path}")
    return data


def _load_telegram_bot_config(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}

    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(f"Telegram bot config must be a JSON object: {path}")
    return data


def _parse_optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def _load_telegram_groups_file(path: Path | None) -> dict[str, str]:
    """Build the ``level -> chat_id`` map used to route notifications.

    Two sources, in priority order:

    1. An explicit ``level_chat_map`` object (``logging``/``warning``/``critical``/
       ``error``/``test``/``private`` -> chat_id). This is the authoritative, per-level
       wiring — edit it to re-point a level at a different chat.
    2. The ``telegram_groups`` list, kept for the human metadata (title, allow_command,
       status). Any active group with a ``notify_level``/``source_notify_level`` fills a
       level that the explicit map left blank.
    """
    if path is None or not path.exists():
        return {}

    # Lazy, like the two secret_text imports above: db_ops.config is imported by every component
    # in the tree, and this branch is dormant unless `telegram.groups_file` is set. The records
    # come from the one reader of telegram_groups.json (2026-08-15) so this is not a fifth parse
    # of that file; `level_chat_map` is read here because it is config wiring, not a group record.
    from db_ops.common import data_sources

    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)

    groups: dict[str, str] = {}

    for item in data_sources.load_telegram_groups(path):
        if str(item.get("status", "")).lower() != "active":
            continue
        group_id = str(item.get("group_id", "")).strip()
        if not group_id:
            continue
        for level_key in ("notify_level", "source_notify_level"):
            level = str(item.get(level_key, "")).strip().lower()
            if level:
                groups.setdefault(level, group_id)

    # Explicit map wins over anything derived from the list above.
    for level, chat_id in (data.get("level_chat_map") or {}).items():
        chat_id = str(chat_id).strip()
        if chat_id:
            groups[str(level).strip().lower()] = chat_id

    return groups


def chat_id_for_level(groups: dict[str, str], level: str) -> str:
    """Resolve the Telegram chat_id for a notification ``level``.

    Canonical resolver shared by every app so routing stays identical everywhere.
    ``critical`` falls back to the ``error`` chat when no dedicated critical chat is
    configured (DB Ops has historically funnelled both to one Criticals group).
    Returns "" when the level is unmapped, which callers treat as "do not send".
    """
    level = str(level or "").strip().lower()
    if level == "critical":
        return str(groups.get("critical") or groups.get("error") or "").strip()
    return str(groups.get(level) or "").strip()
