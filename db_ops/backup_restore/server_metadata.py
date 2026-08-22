"""The opt-in that lets a SQL Server backup or restore also carry its instance metadata.

A SQL Server backup covers user databases only — ``mssql_backup_database.sh`` selects
``WHERE database_id > 4``, so ``master``, ``msdb`` and ``model`` are excluded and every login,
server role, permission, credential, linked server, Agent job and ``sp_configure`` value is
absent after a restore. Oracle and PostgreSQL need no equivalent: RMAN ``DUPLICATE`` and
``pg_basebackup`` are physical, whole-instance, and carry that state inside the data itself.

:mod:`db_ops.common.sqlserver_instance` closes the gap. This module is only the **switch**, and
it is deliberately a small one:

* A backup or restore entry with **no** ``server_metadata`` block behaves exactly as it did
  before this existed. That is the whole design constraint — the scheduled MSSQL flows have been
  running for months and must not change because a capability was added next to them.
* ``"enabled": false`` is the same as absent.
* ``artifacts`` is an **array naming what to include**, so an operator turns on logins and Agent
  jobs without also taking ``sp_configure`` — the setting most likely to be unwanted on a
  different machine. Omitted or empty means every artifact the policy declares.

The block, on a backup entry::

    "server_metadata": {
        "enabled": true,
        "artifacts": ["logins", "server_roles", "permissions", "agent_jobs"]
    }

and on a restore entry::

    "server_metadata": {
        "enabled": true,
        "artifacts": ["logins", "server_roles", "permissions"],
        "phases": ["pre-database", "post-database"]
    }

**No target is stated.** The entry already says where it restores to - ``target_server_id`` names
the host and ``target_container`` names the container on it - and ``db_instances.json`` already
says which SQL Server instance that container is. :func:`resolve_replay_target` follows that
chain. A third field repeating the answer would be a second place for the same fact to live, and
the two would drift the first time a container was renamed. ``"target": "<server_id>"`` remains
available as an override for the one case the chain cannot answer: an instance that is not in the
inventory at all.

``phases`` exists because the two halves are not interchangeable: ``pre-database`` must run
before the databases are restored or their users are orphaned, and ``post-database`` after,
because Agent job steps name databases that have to exist. Listing only one is a legitimate
choice (restore the logins now, the jobs later); listing neither means both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db_ops.backup_restore import events
from db_ops.lib import instance_bundle
from db_ops.lib.paths import TOOL_ROOT

#: A restore that names no phases runs both, in this order.
DEFAULT_PHASES = (instance_bundle.PRE_DATABASE, instance_bundle.POST_DATABASE)

#: Where a bundle lives on the db_ops node, under the SOURCE instance's ``server_id``.
BUNDLE_ROOT = TOOL_ROOT / "runtime" / "instance_bundles"


def instance_bundle_dir(server_id: str) -> Path:
    """The bundle directory for one source instance, on the machine db_ops runs on.

    **Not** ``<backup_dir>/server``, which is what the config's ``note`` fields promised and what
    the first cut of this assumed. ``backup_dir`` is a path *inside the database container on the
    remote host* - ``/var/opt/mssql/backup/dbops`` on 203.0.113.188 - reachable only over SSH by
    the backup script. The export is not a shell step: it is a TDS connection made from the db_ops
    node, so :func:`sqlserver_instance.export_instance` writes with a local ``Path.write_text``
    and can only write where db_ops itself runs. Pointing it at the container path would have
    silently created ``/var/opt/mssql/backup/dbops/server`` **on the worker**, next to nothing.

    Keying on the source ``server_id`` is what lets the two halves meet without another config
    field: the backup entry exports under its ``server_id``, and the restore entry's ``server_id``
    *is* that same source instance, so the restore finds the bundle by asking the same question.
    ``runtime/`` is a bind mount on the worker, so a bundle outlives the container.
    """
    name = str(server_id or "").strip() or "unknown"
    return BUNDLE_ROOT / name


class ServerMetadataConfigError(ValueError):
    """The ``server_metadata`` block is present but cannot be honoured as written."""


@dataclass(frozen=True)
class ServerMetadataPlan:
    """What one backup or restore entry asked for. ``enabled=False`` means do nothing."""

    enabled: bool = False
    artifacts: tuple[str, ...] = ()
    target: str = ""
    phases: tuple[str, ...] = DEFAULT_PHASES
    on_unsupported: str = "skip"
    extras: dict[str, Any] = field(default_factory=dict)

    def request(self, **overrides: Any) -> dict[str, Any]:
        """The JSON object handed to ``db_ops.common.cli sqlserver-*-instance``.

        Built here rather than by each caller so the two sides cannot drift into passing
        different shapes for the same configured intent.
        """
        payload: dict[str, Any] = dict(self.extras)
        if self.artifacts:
            payload["include"] = list(self.artifacts)
        payload.update(overrides)
        return payload


def parse_server_metadata(
    raw: Any, *, label: str, for_restore: bool = False, db_type: str = "sqlserver"
) -> ServerMetadataPlan:
    """Read the block off a backup/restore entry. Absent or disabled -> an inert plan.

    Validation is strict about what it *does* accept, because the failure mode of a typo here is
    silence: a misspelled artifact name would simply not be exported, and nobody discovers that
    until the restore that needed it.
    """
    if raw in (None, {}, False):
        return ServerMetadataPlan()
    if not isinstance(raw, dict):
        raise ServerMetadataConfigError(
            f"{label}: server_metadata must be an object, got {type(raw).__name__}."
        )
    if not bool(raw.get("enabled", False)):
        return ServerMetadataPlan()

    if str(db_type or "").lower() != "sqlserver":
        raise ServerMetadataConfigError(
            f"{label}: server_metadata applies to SQL Server only (this entry is "
            f"db_type={db_type!r}). Oracle and PostgreSQL carry server-level state inside their "
            "physical backups already, so there is nothing for it to do."
        )

    # The policy is read through `data_sources` — the one reader of the data folder — and ordered
    # by `lib`. Neither half is an operation, so neither goes through the CLI: this runs while a
    # config entry is being validated, long before anything is connected to.
    from db_ops.common import data_sources

    known = set(instance_bundle.artifacts_in_order(
        data_sources.load_sqlserver_instance_policy()))
    artifacts = raw.get("artifacts") or []
    if isinstance(artifacts, str):
        raise ServerMetadataConfigError(
            f"{label}: server_metadata.artifacts must be an array, not a string."
        )
    artifacts = tuple(str(name).strip() for name in artifacts if str(name).strip())
    unknown = sorted(set(artifacts) - known)
    if unknown:
        raise ServerMetadataConfigError(
            f"{label}: unknown server_metadata artifact(s) {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(known))}."
        )

    phases = raw.get("phases") or []
    if isinstance(phases, str):
        phases = [phases]
    phases = tuple(str(item).strip().lower() for item in phases if str(item).strip())
    bad_phase = [item for item in phases if item not in DEFAULT_PHASES]
    if bad_phase:
        raise ServerMetadataConfigError(
            f"{label}: server_metadata.phases may only contain "
            f"{' / '.join(DEFAULT_PHASES)}; got {', '.join(bad_phase)}."
        )

    # Optional. The restore entry plus the inventory already answer this; see
    # resolve_replay_target. Stated only to override a chain that cannot resolve.
    target = str(raw.get("target") or "").strip()

    on_unsupported = str(raw.get("on_unsupported") or "skip").strip().lower()
    if on_unsupported not in {"skip", "fail"}:
        raise ServerMetadataConfigError(
            f"{label}: server_metadata.on_unsupported must be 'skip' or 'fail'."
        )

    extras = {
        key: value for key, value in raw.items()
        if key not in {"enabled", "artifacts", "target", "phases", "on_unsupported"}
    }
    return ServerMetadataPlan(
        enabled=True,
        artifacts=artifacts,
        target=target,
        phases=phases or DEFAULT_PHASES,
        on_unsupported=on_unsupported,
        extras=extras,
    )


def resolve_replay_target(
    plan: ServerMetadataPlan,
    *,
    target_server_id: str,
    target_container: str,
    source_server_id: str = "",
    target_host: str = "",
    data_dir: str | Path | None = None,
) -> str:
    """Which SQL Server instance a restore should replay onto.

    Four steps, in order, and each is answering a question the config has already been asked:

    1. ``server_metadata.target`` if the operator stated one - the override.
    2. ``target_server_id`` itself, when it already names a SQL Server instance rather than a
       host. Some entries restore into an instance directly.
    3. The host + container pair: find the inventory's SQL Server instance whose
       ``instance_name`` is ``target_container`` on that host's ip. This is the script path's
       normal route - ``CLOUD2-203-0-113-121-HOST`` + ``mssql_ha_cloud2-primary`` resolves to
       ``CLOUD2-203-0-113-121-MSSQL-1433``.
    4. ``target_host`` alone, an ip, when there is no container to name. The engine path restores
       through an SMB share and a ``sqlcmd`` connection rather than into a container, so its entry
       has no ``target_container`` to match on - what it does have is the target's ip. One SQL
       Server instance at that ip resolves; more than one is ambiguous and raises, exactly as a
       duplicate container name does.

    An ambiguous or absent match raises rather than guessing. Replaying logins onto the wrong
    instance is not an error anyone notices quickly.
    """
    if plan.target:
        return plan.target

    from db_ops.common.data_sources import load_config_metric_targets

    targets = (load_config_metric_targets(data_dir=data_dir) if data_dir is not None
               else load_config_metric_targets())
    by_id = {str(item.server_id): item for item in targets}

    host_id = str(target_server_id or source_server_id or "").strip()
    named = by_id.get(host_id)
    if named is not None and str(getattr(named, "db_type", "")).lower() == "sqlserver":
        return host_id

    host_ip = (str(getattr(named, "ip", "") or "") if named is not None else "") or str(target_host or "").strip()
    container = str(target_container or "").strip()
    if not container:
        # No container to match on. An ip still identifies the instance whenever the host runs
        # exactly one SQL Server, which is what the engine path's targets look like.
        by_ip = [item for item in targets
                 if str(getattr(item, "db_type", "")).lower() == "sqlserver"
                 and host_ip and str(getattr(item, "ip", "")) == host_ip]
        if len(by_ip) == 1:
            return str(by_ip[0].server_id)
        if len(by_ip) > 1:
            raise ServerMetadataConfigError(
                f"{host_ip} runs more than one SQL Server instance "
                f"({', '.join(sorted(str(m.server_id) for m in by_ip))}) and the entry names no "
                "container. Set server_metadata.target to say which one."
            )
        raise ServerMetadataConfigError(
            f"{host_id or host_ip or '<no target>'}: cannot tell which SQL Server instance to "
            "replay onto - the entry names no SQL Server target_server_id, no target_container, "
            "and no registered SQL Server instance sits at its ip. "
            'Set server_metadata.target to the instance\'s server_id.'
        )

    matches = [
        item for item in targets
        if str(getattr(item, "db_type", "")).lower() == "sqlserver"
        and str(getattr(item, "instance_name", "")) == container
        and (not host_ip or str(getattr(item, "ip", "")) == host_ip)
    ]
    if len(matches) == 1:
        return str(matches[0].server_id)
    if not matches:
        raise ServerMetadataConfigError(
            f"No SQL Server instance in db_instances.json matches container "
            f"{container!r}{f' on {host_ip}' if host_ip else ''}. Register it, or set "
            "server_metadata.target to its server_id."
        )
    raise ServerMetadataConfigError(
        f"Container {container!r} matches more than one instance "
        f"({', '.join(sorted(str(m.server_id) for m in matches))}). Set server_metadata.target "
        "to say which one."
    )


def summarize(result: dict[str, Any] | None) -> dict[str, Any]:
    """The few fields of an export/replay report worth keeping in ``job_runs.metadata_json``.

    The full report carries every gate and every fact it was decided from - hundreds of lines for
    an instance with a real login set. That belongs in the evidence file it already writes, not
    duplicated into a run row that an operator reads through Telegram.
    """
    if not result:
        return {}
    summary: dict[str, Any] = {
        "ok": bool(result.get("ok", False)),
        "status": str(result.get("status") or ""),
    }
    error = str(result.get("error") or "")
    if error:
        summary["error"] = error[:500]
    blockers = result.get("blockers") or []
    if blockers:
        summary["blockers"] = [str(name) for name in blockers][:10]
    evidence = str(result.get("evidence_file") or "")
    if evidence:
        summary["evidence_file"] = evidence
    return summary


# `_announce` is `db_ops.backup_restore.events.announce` since 2026-08-16 — it was four
# near-copies in this app, one of which had already drifted its parameter names.


def replay_phase(
    plan: ServerMetadataPlan,
    *,
    phase: str,
    label: str,
    source_server_id: str,
    target_server_id: str = "",
    target_container: str = "",
    target_host: str = "",
    data_dir: str | Path | None = None,
    announce: Any = None,
) -> tuple[str, dict[str, Any] | None]:
    """Replay one metadata phase around a restore. Returns (a ``PHASE=`` line, summary).

    Shared by **both** restore paths, which is the point of it living here. The script path
    (Oracle/PostgreSQL/container-to-container SQL Server) and the engine path (the SMB +
    ``sqlcmd`` production flow) run completely different machinery - different languages, even,
    since the script path's chain selection is bash - but the decision *around* the restore is
    identical: same bundle, same two phases, same ordering rule, same failure policy. Two copies
    of that would drift, and the direction it would drift in is silent: a phase quietly not
    running is indistinguishable from a phase that ran and found nothing to do.

    So the callers pass what they have rather than a shared job object, because they genuinely
    have different shapes: one names a container, the other an ip.

    **Never raises, and never changes the restore's verdict.** The user databases are the
    deliverable; logins and Agent jobs are what makes them usable, and losing them is worth a loud
    warning rather than a failed run that reports nothing was restored when in fact everything
    was. Every failure - a bundle that is not there, a target that cannot be resolved, a replay
    that errored - comes back as a ``PHASE=...`` line, which lands in the run row's
    ``stdout_tail`` and in the Telegram message, so it is visible without being fatal.

    The bundle is looked up under the **source** ``server_id``, which is where the backup entry's
    export wrote it (see :func:`instance_bundle_dir`). A missing one almost always means the same
    instance's backup entry has ``server_metadata`` off while the restore has it on - the one
    mismatch this can be configured into - so it is named as exactly that.
    """
    if not plan.enabled or phase not in plan.phases:
        return "", None

    bundle_dir = instance_bundle_dir(source_server_id)
    if not (bundle_dir / instance_bundle.SERVER_DIR).is_dir():
        line = (f"PHASE=metadata-{phase} SKIPPED no bundle at {bundle_dir} - the backup entry for "
                f"{source_server_id} needs server_metadata.enabled too\n")
        events.announce(announce, "METADATA_SKIP",
                  f"Restore {label}: {phase} instance metadata skipped - no bundle exported "
                  f"for {source_server_id}.")
        return line, {"ok": False, "status": "SKIPPED", "error": f"no bundle at {bundle_dir}"}

    try:
        target = resolve_replay_target(
            plan,
            target_server_id=target_server_id,
            target_container=target_container,
            source_server_id=source_server_id,
            target_host=target_host,
            data_dir=data_dir,
        )
    except Exception as exc:  # noqa: BLE001 - a config problem must not lose a good restore.
        line = f"PHASE=metadata-{phase} SKIPPED cannot resolve replay target: {exc}\n"
        events.announce(announce, "METADATA_SKIP",
                  f"Restore {label}: {phase} instance metadata skipped - {exc}")
        return line, {"ok": False, "status": "SKIPPED", "error": str(exc)[:500]}

    events.announce(announce, "METADATA_START",
              f"Restore {label}: replaying {phase} instance metadata onto {target}.")
    result = replay_for_restore(
        plan, phase=phase, bundle_dir=bundle_dir, target=target, data_dir=data_dir,
    )
    report = summarize(result)
    ok = bool((result or {}).get("ok", False))
    line = (f"PHASE=metadata-{phase} {'ok' if ok else 'FAILED'} target={target} "
            f"status={report.get('status', '')}\n")
    events.announce(announce, "METADATA_DONE" if ok else "METADATA_ERROR",
              f"Restore {label}: {phase} instance metadata "
              + (f"replayed onto {target}." if ok
                 else f"FAILED on {target} - the restore itself is unaffected."),
              {"phase": phase, "target": target, **report})
    return line, report


def export_for_backup(
    plan: ServerMetadataPlan,
    *,
    server_id: str,
    bundle_dir: str | Path,
    data_dir: str | Path | None = None,
    echo: Any = None,
) -> dict[str, Any] | None:
    """Export the instance's metadata next to the backup it belongs with. Read-only.

    Returns ``None`` when the plan is off, so a caller can log "not requested" rather than
    inventing a skipped result. A failure here is returned, not raised: the data backup is the
    thing that must not be lost to a metadata step that could not read ``sys.credentials``.
    """
    if not plan.enabled:
        return None
    from db_ops.backup_restore.instance_metadata import export_instance

    # Across the CLI, not by importing the function: the app supplies a finished request and
    # `common` performs it. `data_dir`/`echo` do not cross - a data directory is a lookup the
    # other side must not do, and there is nobody to echo to on the far side of a process.
    return export_instance(plan.request(target=server_id, output_dir=str(bundle_dir)))


def replay_for_restore(
    plan: ServerMetadataPlan,
    *,
    phase: str,
    bundle_dir: str | Path,
    target: str,
    data_dir: str | Path | None = None,
    echo: Any = None,
) -> dict[str, Any] | None:
    """Replay one phase onto the configured target instance.

    ``None`` when the plan is off or does not include this phase. Unlike the export, a failure
    here is returned as a failed result the caller decides about: replaying logins is often the
    point of the restore, and swallowing its failure would leave a database nobody can log into
    while the restore reports success.
    """
    if not plan.enabled or phase not in plan.phases:
        return None
    request = plan.request(
        target=target,
        bundle_dir=str(bundle_dir),
        phase=phase,
        on_unsupported=plan.on_unsupported,
        # A scheduled restore has nobody at a keyboard. Intent is declared by the config block
        # existing and saying enabled: true, which is the same authorization model the restore
        # itself runs under.
        confirm=True,
        assume_yes=True,
        reason=f"restore workflow: {phase} instance metadata from {bundle_dir}",
    )
    # `artifacts` selects what the EXPORT writes; replay applies whatever the bundle contains,
    # so passing the list again here would be a second place for the same decision to live.
    request.pop("include", None)
    from db_ops.backup_restore.instance_metadata import replay_instance

    return replay_instance(request)
