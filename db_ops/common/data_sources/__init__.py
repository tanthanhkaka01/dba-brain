"""The one reader of the ``data/`` folder — a package since 2026-08-15.

It became a package rather than growing to a thousand lines: ``metric_targets_config`` and
``target_resolve`` were separate modules doing the same job this one does — open a file under
``data/`` and answer a question about what is configured — and under the rule "an app does not
import ``common``" they had nowhere else to go. They are the same exemption, so they are the same
package; the alternative was three exemptions that each had to be argued again.

Submodules re-export through here, so ``from db_ops.common.data_sources import ...`` reaches
everything and no caller needs to know which file a lookup lives in.

Single entry point for loading db_ops connection inputs from the local data folder.

Historically these inputs lived outside the tool, at the repository root
(``architecture/*_users.json``, ``architecture/database-inventory.json`` and
``secrets/secret_text.json``), split across one file per db_type plus
``remote_users.json`` and ``monitor_users.json``. They now live next to the rest
of the runtime inputs under ``data/``, consolidated into a single
``users.json`` (``database_credentials`` + ``remote_credentials`` + ``monitor_users``),
so the tool is self-contained and the data folder can be bind-mounted at runtime.

App modules import the loaders here instead of reaching to the repo root:

    from db_ops.common import data_sources
    secrets = data_sources.load_secret_text()
    inventory = data_sources.load_inventory()
    credentials = data_sources.load_all_credentials()

The database inventory is now derived
from ``data/db_instances.json``: ``load_inventory()`` reshapes each db instance
into the ``servers -> databases`` structure the SQL/Telegram resolvers expect,
so no architecture YAML file is required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.lib.paths import DEFAULT_DATA_DIR, TOOL_ROOT  # noqa: F401 - one definition

from db_ops.lib import secret_text as _secret_text
from db_ops.lib.secret_text import ENCRYPTED_SECRET_TEXT_FILENAME
from db_ops.common.sql_execution import (
    load_credentials_file,
    load_json_file,
    load_remote_credentials_file,
)

# tools/db_ops -> data

DB_INSTANCES_FILENAME = "db_instances.json"
# All user/credential records live in a single consolidated file. Database
# credentials (every db_type), remote OS credentials, and monitor users were
# previously split across "<db_type>_users.json", "remote_users.json" and
# "monitor_users.json"; they are now sections of one "users.json".
USERS_FILENAME = "users.json"
METRIC_DEFINITIONS_FILENAME = "metric_definitions.json"
TELEGRAM_GROUPS_FILENAME = "telegram_groups.json"
TELEGRAM_USERS_FILENAME = "telegram_users.json"
BACKUP_POLICY_FILENAME = "backup_policy.json"
CAPACITY_POLICY_FILENAME = "capacity_policy.json"
SQLSERVER_INSTANCE_POLICY_FILENAME = "sqlserver_instance_policy.json"
#: The lab-DB registry the SRE provisioner writes and the deploy merge reads. Spelled here
#: with the other data/ filenames so `control` and `sre` cannot disagree about it — they
#: had a copy each, which briefly made `control` import `sre`.
REGISTRY_FILENAME = "docker_db_connections.json"

# db_types that may appear in the "database_credentials" section. "postgres" is
# an alias for "postgresql".
CREDENTIAL_DB_TYPES: tuple[str, ...] = ("sqlserver", "oracle", "mysql", "postgresql")


def _resolve_data_dir(data_dir: str | Path | None) -> Path:
    return Path(data_dir) if data_dir else DEFAULT_DATA_DIR


def secret_text_path(data_dir: str | Path | None = None) -> Path:
    """Path to the encrypted secret-text file (no plaintext fallback)."""
    return _resolve_data_dir(data_dir) / ENCRYPTED_SECRET_TEXT_FILENAME


def users_path(data_dir: str | Path | None = None) -> Path:
    """Path to the consolidated users file (database + remote + monitor credentials)."""
    return _resolve_data_dir(data_dir) / USERS_FILENAME


def db_instances_path(data_dir: str | Path | None = None) -> Path:
    return _resolve_data_dir(data_dir) / DB_INSTANCES_FILENAME


def metric_definitions_path(data_dir: str | Path | None = None) -> Path:
    return _resolve_data_dir(data_dir) / METRIC_DEFINITIONS_FILENAME


def load_metric_definition_records(
    path: str | Path | None = None, *, data_dir: str | Path | None = None
) -> list[dict[str, Any]]:
    """The ``metrics`` list from ``metric_definitions.json`` — the one read of that file.

    Eight call sites across three apps used to read it, each with its own default path and its
    own ``json.loads``: metrics validated it, reports opened it four times for ``report_policy``
    and ``time_window`` fields, telegram opened it once to list metric codes. Four parses of one
    file are four chances to disagree about what a metric is, and two of them silently swallowed
    a decode error and carried on with an empty policy — which reads exactly like a metric with
    no policy configured.

    Missing file returns ``[]``: a report that cannot find the definitions should render without
    per-metric policy, not fail. A **malformed** one raises, because a truncated write is not the
    same fact as an absent file and must not be reported as one.

    ``path`` wins over ``data_dir`` so a caller with an explicit file (tests, a one-off run
    against another checkout) still gets exactly that file.
    """
    source = Path(path) if path else metric_definitions_path(data_dir)
    if not source.exists():
        return []
    data = load_json_file(source)
    records = data.get("metrics")
    if not isinstance(records, list):
        raise RuntimeError(f"metric_definitions.json must contain a 'metrics' list: {source}")
    return [item for item in records if isinstance(item, dict)]


def telegram_groups_path(data_dir: str | Path | None = None) -> Path:
    return _resolve_data_dir(data_dir) / TELEGRAM_GROUPS_FILENAME


def telegram_users_path(data_dir: str | Path | None = None) -> Path:
    return _resolve_data_dir(data_dir) / TELEGRAM_USERS_FILENAME


def load_telegram_groups(
    path: str | Path | None = None, *, data_dir: str | Path | None = None
) -> list[dict[str, Any]]:
    """The ``telegram_groups`` records — the one read of ``telegram_groups.json``.

    The Telegram app owns this file and is the only thing that *writes* it. Reading it is a
    different question, and three components asked it: the app itself (permissions), and
    ``db.cli ops-status`` (which notify level goes to which chat).

    ``ops-status`` genuinely cannot ask the Telegram app: it exists to report that the other apps
    are failing, so shelling out to one of them to find out where to send that report is the
    dependency it was built without. Reading through this module keeps that property — an import
    is not a process — while leaving exactly one place that knows the file's shape. Before
    2026-08-15 the answer was a fourth copy of the parse living in ``common/cli.py``.

    Writes stay with the owner (``telegram/updates.py``): one reader, one writer, and they are
    not the same rule.
    """
    return _telegram_records(
        path or telegram_groups_path(data_dir), root_key="telegram_groups")


def load_telegram_users(
    path: str | Path | None = None, *, data_dir: str | Path | None = None
) -> list[dict[str, Any]]:
    """The ``telegram_users`` records — the one read of ``telegram_users.json``. See above."""
    return _telegram_records(
        path or telegram_users_path(data_dir), root_key="telegram_users")


def _telegram_records(path: str | Path, *, root_key: str) -> list[dict[str, Any]]:
    """Missing file -> ``[]``: an estate with no groups registered yet is not an error.

    Non-dict entries are dropped rather than raising. These files are edited by the bot at
    runtime, so a half-written entry must degrade to "that group is not configured" instead of
    taking down permission checks for every other group in the file.
    """
    source = Path(path)
    if not source.exists():
        return []
    records = load_json_file(source).get(root_key)
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


def load_backup_policy(
    path: str | Path | None = None, *, data_dir: str | Path | None = None
) -> dict[str, Any]:
    """``data/backup_policy.json`` — per-server backup expectations. The one read of that file.

    The judging that used to sit beside this read now lives in :mod:`db_ops.lib.backup_policy`,
    which takes the document as an argument. The split is the point: a rule about backup ages is
    a pure function and belongs where every component can call it, while finding the file is a
    question about the machine and belongs here.
    """
    return _policy_document(
        path or _resolve_data_dir(data_dir) / BACKUP_POLICY_FILENAME, root_key="backup_policy")


def load_capacity_policy(
    path: str | Path | None = None, *, data_dir: str | Path | None = None
) -> dict[str, Any]:
    """``data/capacity_policy.json`` — growth thresholds and reserves. See above."""
    return _policy_document(
        path or _resolve_data_dir(data_dir) / CAPACITY_POLICY_FILENAME, root_key="capacity_policy")


def load_sqlserver_instance_policy(
    path: str | Path | None = None, *, data_dir: str | Path | None = None
) -> dict[str, Any]:
    """``data/sqlserver_instance_policy.json`` — which server settings are portable.

    **A missing file raises**, unlike every other policy read here, and that difference is the
    point. The other policies grade something that exists whether or not a rule was written for
    it; this one *is* the list of what gets copied between instances, so defaulting it would mean
    a replay silently carrying a different set of logins and Agent jobs depending on whether
    anyone noticed the file was gone. There is no built-in answer on purpose.

    The document is not unwrapped under a root key: this file's whole content is the policy.
    """
    source = Path(path) if path else _resolve_data_dir(data_dir) / SQLSERVER_INSTANCE_POLICY_FILENAME
    if not source.exists():
        raise FileNotFoundError(
            f"{source} not found. It declares which server settings are portable between "
            "instances; db_ops has no built-in answer on purpose."
        )
    return load_json_file(source)


def _policy_document(path: str | Path, *, root_key: str) -> dict[str, Any]:
    """The policy object under ``root_key``, or ``{}``.

    Best-effort on purpose, and this is the behaviour the callers were written against: a report
    must still render when the policy file has not been deployed yet, and "no policy configured"
    is more useful than a traceback. The unwrapping matters as much as the read — the file wraps
    its content in a single named key, and a caller handed the outer document sees every rule as
    absent, which looks exactly like a policy that requires nothing.
    """
    source = Path(path)
    if not source.exists():
        return {}
    try:
        document = load_json_file(source)
    except (OSError, ValueError, RuntimeError):
        return {}
    policy = document.get(root_key)
    return policy if isinstance(policy, dict) else {}


def group_credentials_by_type(groups: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group ``database_credentials`` entries by their ``db_type`` field into a
    {db_type: groups} mapping (``postgres`` folded into ``postgresql``, then re-aliased)."""
    by_type: dict[str, list[dict[str, Any]]] = {db_type: [] for db_type in CREDENTIAL_DB_TYPES}
    for group in groups:
        db_type = str(group.get("db_type", "")).strip().lower()
        if db_type == "postgres":
            db_type = "postgresql"
        if db_type in by_type:
            by_type[db_type].append(group)
    by_type["postgres"] = by_type["postgresql"]
    return by_type


class CredentialNotFound(RuntimeError):
    """No credential could be resolved for a target — a refusal, never a guess."""


def find_database_credential(
    groups: list[dict[str, Any]],
    *,
    server_id: str,
    credential_name: str,
    db_type: str = "",
    service_name: str = "",
    instance_name: str = "",
) -> dict[str, Any]:
    """Resolve the one database credential a target runs as. **Single source of truth.**

    Every app used to answer this its own way, and they disagreed on the part that matters:
    what to do when the target names no credential. Metrics picked whichever entry had role
    DBA/SYSDBA, other paths took the first entry in file order — so a config omission silently
    connected as *something*, decided by role or by file order rather than by anyone. That is
    now refused: ``credential_name`` is **required**, and an unnamed or unknown credential
    raises :class:`CredentialNotFound` listing what the server does have.

    Matching narrows only on the keys the caller supplies: ``server_id`` always, then
    ``db_type``/``service_name``/``instance_name`` when given (case-insensitive; an empty value
    on either side is not a constraint, since inventories fill these unevenly).
    """
    wanted = str(credential_name or "").strip()
    if not wanted:
        raise CredentialNotFound(
            f"No credential configured for {server_id or '<unknown server>'}: set "
            "default_credential_name on the instance (db_instances.json) or credential_name "
            "on the target."
        )

    available: list[str] = []
    for group in groups:
        if str(group.get("server_id", "")).strip() != str(server_id).strip():
            continue
        if db_type and str(group.get("db_type", "")).strip().lower() != db_type.strip().lower():
            continue
        if service_name and str(group.get("service_name", "")).strip():
            if str(group.get("service_name", "")).strip().lower() != service_name.strip().lower():
                continue
        group_instance = str(group.get("instance_name") or group.get("sid") or "").strip()
        if instance_name and group_instance and group_instance.lower() != instance_name.strip().lower():
            continue
        for credential in group.get("credentials", []) or []:
            name = str(credential.get("credential_name", "")).strip()
            available.append(name)
            if name == wanted:
                return dict(credential)

    known = ", ".join(name for name in available if name) or "none configured"
    raise CredentialNotFound(
        f"Credential not found for server_id {server_id}: {wanted}. Available: {known}."
    )


def load_secret_text(data_dir: str | Path | None = None, *, key: str | None = None) -> dict[str, str]:
    """Load secrets (password_ref -> secret) by decrypting ``encrypted_secret_text.json``.
    Encrypted-only by design — there is NO plaintext fallback (see
    ``secret_text.load_secret_text``). The decryption key comes from ``key`` or the
    ``DB_OPS_SECRET_KEY`` environment variable. Missing file -> {}."""
    return _secret_text.load_secret_text(_resolve_data_dir(data_dir), key=key)


def load_credentials(db_type: str, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Load the credential groups for one db_type from ``users.json`` (filtered by ``db_type``)."""
    return group_credentials_by_type(load_credentials_file(users_path(data_dir))).get(db_type.lower(), [])


def load_all_credentials(data_dir: str | Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Load ``users.json`` ``database_credentials`` into a {db_type: groups} mapping."""
    return group_credentials_by_type(load_credentials_file(users_path(data_dir)))


def load_remote_credentials(data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Load remote (OS/cmd) credential groups from ``users.json`` ``remote_credentials``."""
    return load_remote_credentials_file(users_path(data_dir))


def load_db_instances(data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Load the raw db instance records from ``db_instances.json``."""
    path = db_instances_path(data_dir)
    if not path.exists():
        return []
    return list(load_json_file(path).get("db_instances", []))


def load_inventory(data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Build the ``servers`` inventory list from ``db_instances.json``.

    This replaces the old external inventory file. Each db instance becomes a
    server with a single database entry, exposing the same fields the SQL task
    and Telegram resolvers read (``server_id``, ``ip``, ``service_name``,
    ``instance_name``/``sid``, ``database_names``, ``port``, ...).
    """
    servers: list[dict[str, Any]] = []
    for item in load_db_instances(data_dir):
        if not isinstance(item, dict):
            continue
        database = dict(item)
        db_type = str(item.get("db_type", "")).lower()
        # Oracle resolvers match on "sid"; db_instances stores it as instance_name.
        if db_type == "oracle" and not database.get("sid") and item.get("instance_name"):
            database["sid"] = item.get("instance_name")
        server_id = str(item.get("server_id") or _server_id_from_instance(item))
        database["server_id"] = server_id
        servers.append(
            {
                "server_id": server_id,
                "company_code": item.get("site"),
                "ip": item.get("ip"),
                "databases": [database],
            }
        )
    return servers


def _server_id_from_instance(item: dict[str, Any]) -> str:
    ip = str(item.get("ip", "")).strip()
    if not ip:
        return ""
    return f"{str(item.get('site', '') or 'DB')}-{ip.replace('.', '-')}"


# Re-exported so the package presents one surface. Callers ask data_sources a question; which
# file answers it is this package's business, not theirs.
from db_ops.common.data_sources.metric_targets import (  # noqa: E402,F401
    load_config_metric_targets,
    resolve_config_metric_target,
)
from db_ops.common.data_sources.ssh_auth import (  # noqa: E402,F401
    SSH_KEYS_DIRNAME,
    resolve_ssh_key,
    resolve_ssh_password,
    ssh_keys_dir,
)
from db_ops.common.data_sources.target_resolve import (  # noqa: E402,F401
    TargetResolveError,
    format_target_list,
    list_target_instances,
    normalize_db_type,
    parse_target_spec,
    resolve_sql_target_fields,
    resolve_target_instance,
)


#: Empty on purpose. This was one estate's internal report host until 2026-08-21 — a real
#: hostname and port compiled in as the default, which did two wrong things at once: it published
#: a private address to anyone reading the source, and it handed every other operator a base URL
#: pointing at a machine they cannot reach. An unconfigured base URL is not a broken one; callers
#: that build a page link fall back to a relative href, and callers that need an absolute URL (the
#: Telegram messages) leave the link out. A link that 404s is worse than no link.
DEFAULT_REPORT_BASE_URL = ""


def report_base_url() -> str:
    """The published base URL, from ``data/reports_config.json``.

    Moved here from ``common/report_archive.py`` on 2026-08-15: it was the one part of that
    module that reads the data folder, and the rest — stamping and copying files — is pure and
    now lives in ``db_ops/lib/report_archive.py``.

    Lives in ``common`` because more than one app needs to link to a published page: the reports
    app builds its own cross-links with it, and the SLA app points Telegram at the SLA page
    instead of pasting 3,900 characters of detail into every message. A second copy would let the
    two disagree about where the reports are, and a link that 404s is worse than no link.
    """
    import json

    path = Path(_resolve_data_dir(None)) / "reports_config.json"
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return DEFAULT_REPORT_BASE_URL
    configured = str(data.get("report_base_url") or DEFAULT_REPORT_BASE_URL).strip()
    # An empty answer stays empty: appending "/" to nothing would turn "not configured" into a
    # root-relative URL, which is a different claim and a wrong one.
    return configured.rstrip("/") + "/" if configured else ""


def inventory_exclude_ip_prefixes(data_dir: str | Path | None = None) -> tuple[str, ...]:
    """Server ip prefixes the inventory pages leave out, from ``data/reports_config.json``.

    Empty unless the operator says otherwise. It was a constant in ``lib/inventory_render.py``
    until 2026-08-21 — one estate's management subnet, compiled into the rendering library, so
    every inventory page anyone rendered silently dropped those servers. Hiding a machine is a
    decision about *an* estate, which makes it configuration by definition.

    Lives here rather than in ``lib`` for the usual reason: reading the data folder is an
    operation, and ``lib`` is only ever a function of its arguments.
    """
    import json

    path = Path(_resolve_data_dir(data_dir)) / "reports_config.json"
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return ()
    raw = data.get("inventory_exclude_ip_prefixes") or ()
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(item).strip() for item in raw if str(item).strip())
