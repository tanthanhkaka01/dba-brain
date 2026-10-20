"""The defaults 0.29.0 renamed, and the records that had been running on them.

A default the code supplies is configuration nobody wrote down. A SQL Server backup job that names
no certificate has been encrypting with the certificate of the earlier default name since its first
run, and a restore of that backup imports that one: the name is that node's, as much as if its
file said so. 0.29.0 gives the default the tool's own name (the operator, 2026-10-12: *what belongs
to configuration is not changed; what belongs to the code is*) - so before it does, the value each
such record has been running on is **written into the record**. From then on the code's default
decides only for a record made after the upgrade, and no record that existed changes what it does.

Measured on the estate's own file the day this was written: 9 of 12 SQL Server backup entries and
all 3 script-driven SQL Server restores named no certificate. Renaming the default alone would
have had the next backup look for a certificate the server does not hold, create it, and encrypt
with a key no restore target has.

The file that marks a restore's staging folder is the second such default. The folder on the
target host carries the marker the first copy into it wrote, and retention prunes every staged
file and never the marker - so between two runs the folder holds the marker alone. Under a default
of another name the next copy met one unknown file and nothing that is at the source, and refused
the folder as somebody else's. Each entry that stages is told the name its folder carries.

``upgrade-config``'s step ``earlier-defaults`` runs these rules; they are here, pure, so the step
and its tests read one list. The earlier value is written in this file because stating it is the
whole job - the guard that holds the tree to one name allows the file for that, as it allows the
rename rules.
"""
from __future__ import annotations

from typing import Any

#: The certificate a SQL Server backup was encrypted with, and a restore imported, when a record
#: named none - until 0.29.0.
EARLIER_BACKUP_CERT_NAME = "db_ops_backup_cert"
#: Where a backup job and a script-driven restore state the certificate: their ``env``.
CERT_ENV = "BACKUP_CERT_NAME"
#: The file a copy left in a restore's staging folder to say the folder is this tool's - until
#: 0.29.0. The folders on the hosts carry it, and a folder retention has emptied down to its
#: marker holds nothing else the copy could know it by.
EARLIER_STAGING_MARKER = ".dbops-staging"
#: Where a lab database's instance folder and its backup mount were put on a host: the two folders
#: the code held as constants until 0.29.0, and holds no longer - a node states its own
#: (sre_config.json, sre.docker_db). Nothing reads the compose_path a lab's record carries: a move
#: and a forced re-create find the lab under the folder and its name.
EARLIER_CONTAINERS_DIR = "/opt/db_ops/containers"
EARLIER_BACKUP_MOUNT = "/opt/db_ops/backup"


def _sqlserver(record: dict[str, Any]) -> bool:
    return str(record.get("db_type") or "").strip().lower() == "sqlserver"


def _state_env(holder: dict[str, Any], where: str, changes: list[dict[str, str]]) -> None:
    """Write the earlier name into ``holder['env']`` when it names no certificate."""
    env = holder.get("env")
    if env is None:
        env = {}
    if not isinstance(env, dict):
        return                      # not an object: the loader's finding, by name - not this step's
    if str(env.get(CERT_ENV) or "").strip():
        return
    env[CERT_ENV] = EARLIER_BACKUP_CERT_NAME
    holder["env"] = env
    changes.append({"where": where, "field": f"env.{CERT_ENV}", "value": EARLIER_BACKUP_CERT_NAME})


def _state_source(holder: dict[str, Any], where: str, changes: list[dict[str, str]]) -> None:
    """Write the earlier name into ``holder['source']['backup_certificate']`` when it has none."""
    source = holder.get("source")
    certificate = source.get("backup_certificate") if isinstance(source, dict) else None
    if not isinstance(certificate, dict) or not certificate:
        return                      # no such block: the certificate comes another way
    if str(certificate.get("name") or "").strip():
        return
    source["backup_certificate"] = {"name": EARLIER_BACKUP_CERT_NAME,
                                    **{key: value for key, value in certificate.items() if key != "name"}}
    changes.append({"where": where, "field": "source.backup_certificate.name",
                    "value": EARLIER_BACKUP_CERT_NAME})


def state_backup_certificate(document: Any) -> list[dict[str, str]]:
    """State the earlier certificate name in every record of a ``restore_config.json`` document
    that has been leaving it to the default. The document is changed in place; the changes made
    are returned, one per record and field. A record that states a name keeps it, whatever it is.

    Three kinds of record took the default: every job of a SQL Server **backup** entry (its ``env``
    reaches the script, whose own default it was), a **script-driven** SQL Server restore (its
    ``env``), and a **share-driven** restore whose ``source.backup_certificate`` gave a passphrase
    and no name. An entry that is switched off is stated too: it ran on the default when it ran,
    and it may be switched on again.
    """
    block = document.get("backup_restore", document) if isinstance(document, dict) else None
    if not isinstance(block, dict):
        return []
    changes: list[dict[str, str]] = []
    for entry in block.get("backups") or []:
        if not isinstance(entry, dict) or not _sqlserver(entry):
            continue
        label = str(entry.get("backup_id") or "?")
        for job in entry.get("jobs") or []:
            if isinstance(job, dict):
                _state_env(job, f"backups[{label}].jobs[{job.get('job') or '?'}]", changes)
    _state_source(block, "backup_restore", changes)
    for entry in block.get("restores") or []:
        if not isinstance(entry, dict):
            continue
        label = f"restores[{entry.get('restore_id') or '?'}]"
        if entry.get("script"):
            if _sqlserver(entry):
                _state_env(entry, label, changes)
        else:
            _state_source(entry, label, changes)
    return changes


def state_staging_marker(document: Any) -> list[dict[str, str]]:
    """State the earlier marker name in every restore entry of a ``restore_config.json`` document
    that stages on its target (``target_backup_dir``) and names no marker. Changed in place; the
    changes made are returned.

    The staging folder on the target host was marked by the copy that first used it, and it is
    that file - not anything in this node's configuration - that the next copy and the next prune
    look for. An entry added after the upgrade states nothing and marks its own new folder with the
    new name. An entry that names a marker keeps it.
    """
    block = document.get("backup_restore", document) if isinstance(document, dict) else None
    if not isinstance(block, dict):
        return []
    changes: list[dict[str, str]] = []
    for entry in block.get("restores") or []:
        if not isinstance(entry, dict) or not entry.get("script"):
            continue
        if not str(entry.get("target_backup_dir") or "").strip():
            continue                # it stages nowhere: the engine reads the source's own folder
        if str(entry.get("staging_marker") or "").strip():
            continue
        entry["staging_marker"] = EARLIER_STAGING_MARKER
        changes.append({"where": f"restores[{entry.get('restore_id') or '?'}]",
                        "field": "staging_marker", "value": EARLIER_STAGING_MARKER})
    return changes


def state_lab_folders(sre_config: Any) -> list[dict[str, str]]:
    """State the two lab folders in a ``sre_config.json`` document - ``sre.docker_db`` - where it
    states none. Changed in place; the changes made are returned.

    Until 0.29.0 the folders were constants in the code, so every node ran on them whether or not
    it had built a lab yet. The code holds no folder any more (the operator, 2026-10-12: *a field,
    so the code hard-codes nothing and falls back to nothing*), and a lab command that finds none
    stated is refused - so the node is told the two it has been using. A folder it already states
    is kept, whatever it is.
    """
    if not isinstance(sre_config, dict):
        return []
    settings = sre_config.get("sre")
    if settings is None:
        settings = sre_config["sre"] = {}
    if not isinstance(settings, dict):
        return []
    block = settings.get("docker_db")
    if block is None:
        block = settings["docker_db"] = {}
    if not isinstance(block, dict):
        return []
    changes: list[dict[str, str]] = []
    for field, value in (("containers_dir", EARLIER_CONTAINERS_DIR), ("backup_mount", EARLIER_BACKUP_MOUNT)):
        if not str(block.get(field) or "").strip():
            block[field] = value
            changes.append({"where": "sre.docker_db", "field": field, "value": value})
    return changes


#: The fields of an ``app_commands.json`` record, in the order the shipped files write them. Every
#: one is required since 0.30.0 (the operator, 2026-10-16: *in app_commands every field is required*).
APP_COMMAND_FIELDS = (
    "node_role",
    "sort_order",
    "app_command_id",
    "app_code",
    "app_name",
    "display_name",
    "log_scope",
    "working_dir",
    "command_text",
    "active",
    "run_mode",
    "max_parallel",
    "time_window",
    "note",
)


def state_app_commands(document: Any) -> list[dict[str, str]]:
    """Write into each app command the value the daemon has been giving each field it left out.

    The same rule as the rest of this file: a field the loader (``jobs.daemon.load_app_commands``,
    ``lib.run_mode.parse``) filled in is written down with that value, so making the field required
    changes nothing the node does. ``sort_order`` the loader never read: the record's place in the
    file is the order it was listed in. A missing ``note`` is an empty one - the reason for a schedule
    is not something an upgrade can know.

    Three fields the loader never filled are left missing, and the configuration check names them:
    ``app_command_id`` and ``log_scope`` (it refuses a record without them) and ``time_window``,
    whose ``repeat_interval`` it has no value for.
    """
    records = document.get("app_commands") if isinstance(document, dict) else None
    if not isinstance(records, list):
        return []
    changes: list[dict[str, str]] = []
    for position, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        command_id = record.get("app_command_id")
        mode = str(record.get("run_mode") or "sync").strip().lower()
        loader_values = {
            "node_role": "all",
            "sort_order": position,
            "app_code": command_id,
            "app_name": record.get("app_code") or command_id,
            "display_name": record.get("command_name") or record.get("app_name") or command_id,
            "working_dir": "",
            "command_text": "",
            "active": True,
            "run_mode": "sync",
            "max_parallel": 4 if mode == "async" else 1,
            "note": "",
        }
        stated = False
        for field, value in loader_values.items():
            if field in record or value is None:
                continue
            record[field] = value
            stated = True
            changes.append({"where": f"app_commands[{command_id or position}]", "field": field,
                            "value": str(value)})
        if stated:
            # The order the shipped files use, so a node's file reads like the one it came from.
            ordered = {field: record[field] for field in APP_COMMAND_FIELDS if field in record}
            ordered.update({field: value for field, value in record.items() if field not in ordered})
            record.clear()
            record.update(ordered)
    return changes
