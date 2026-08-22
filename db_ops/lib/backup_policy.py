"""Is each database actually protected? — evaluated per database, per backup type.

The reports used to answer "is this server backed up" by taking the newest backup evidence found
anywhere on the instance. That is not an answer: one healthy database hides every stale one behind
it. On 192.0.2.115 the fleet page showed FULL and DIFF present and healthy while the newest LOG
backup on the instance was 124 days old and three databases sat at 99%+ log usage with
``LOG_REUSE_WAIT = LOG_BACKUP``. On 192.0.2.250 the page read "Full+Diff+Log" although the DIFF
evidence covered one database out of six.

So the unit of judgement here is ``(database, backup type)``:

* what the policy in ``data/backup_policy.json`` requires for that database, given its recovery
  model — a DIFF that policy does not require is reported **not required**, never "missing";
* how old the newest backup of that type is, or that there has never been one;
* the worst required state per database, and ``compliant / eligible`` counts per type.

**What this deliberately does not claim.** A stale LOG backup proves an RPO violation. It does not
prove the log chain is broken: that needs backup LSNs, recovery-fork ids and recovery-model
history, none of which the collectors gather. The wording stays "LOG RPO violated" until that
evidence exists, because "chain broken" is an instruction to take a fresh FULL and a DBA who acts
on it when the chain was in fact intact has destroyed a restore path for no reason.
"""

from __future__ import annotations
from db_ops.lib.coerce import as_float

import datetime
import json
from typing import Any


#: msdb records the type as a single letter; the collectors already expand it, but both spellings
#: reach this module depending on which metric a row came from.
BACKUP_TYPES = ("FULL", "DIFF", "LOG")

#: Every metric that emits the ``BACKUP_LAST_RESULT`` message contract (``database=``,
#: ``recovery_model=``, ``backup_type=``, ``backup_finish_date=``), whatever engine it read.
#: SQL Server and Oracle share the code because both are SQL collectors and a metric's variants
#: may differ by engine; PostgreSQL needs its own because ``collector_type`` is per metric and a
#: base backup is a directory on disk, not a catalog row. Listing them here rather than at each
#: reader is what keeps "which codes count as backup evidence" a single fact — the report layer
#: previously hard-coded the one code, which is why Oracle and PostgreSQL servers showed
#: "No metrics" in the Backup column while their backups ran nightly.
BACKUP_LAST_RESULT_CODES = ("BACKUP_LAST_RESULT", "POSTGRES_BACKUP_LAST_RESULT")
TYPE_ALIASES = {
    "D": "FULL", "DATABASE": "FULL", "FULL": "FULL",
    "I": "DIFF", "DIFF": "DIFF", "DIFFERENTIAL": "DIFF",
    "L": "LOG", "LOG": "LOG",
}

_SEVERITY_RANK = {"CRITICAL": 3, "WARNING": 2, "OK": 1, "NOT_REQUIRED": 0, "UNKNOWN": 0}




def _type_rule(policy: dict, *, server_id: str, database: str, backup_type: str) -> dict:
    """The rule for one ``(database, type)``: the first matching override, else the default."""
    rule: dict[str, Any] = dict(((policy.get("defaults") or {}).get("types") or {}).get(backup_type) or {})
    for override in policy.get("overrides") or []:
        wanted_server = str(override.get("server_id") or "").strip().lower()
        wanted_db = str(override.get("database") or "").strip().lower()
        if wanted_server and wanted_server != str(server_id or "").strip().lower():
            continue
        if wanted_db and wanted_db != str(database or "").strip().lower():
            continue
        rule.update(((override.get("types") or {}).get(backup_type) or {}))
        break
    return rule


def _is_required(rule: dict, recovery_model: str) -> bool:
    models = rule.get("required_for_recovery_models")
    if models is not None:
        return str(recovery_model or "").strip().upper() in {str(m).upper() for m in models}
    return bool(rule.get("required"))




def _parse_kv(message: str) -> dict[str, str]:
    out = {}
    for part in str(message or "").split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _age_hours(finish: str, *, now: datetime.datetime) -> float | None:
    """Hours since a ``backup_finish_date``, which SQL Server writes in *its own* local time.

    Deliberately naive arithmetic against a naive ``now``: the report has no reliable offset
    between the engine's clock and the collector's, and inventing one would move every backup age
    by a whole timezone. The consequence is stated on the page rather than papered over here.
    """
    if not finish or str(finish).upper() in ("NULL", "NONE"):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(finish).strip().replace("Z", ""))
    except ValueError:
        return None
    return round((now - parsed).total_seconds() / 3600.0, 1)


def collect_evidence(rows: list[dict], *, now: datetime.datetime | None = None) -> dict[str, dict]:
    """``{database: {recovery_model, types: {FULL: {...}}, as_of}}`` from the backup metric rows.

    Any code in :data:`BACKUP_LAST_RESULT_CODES` carries one row per ``(database, type)`` with the
    finish date and the age in its ``metric_value``; ``BACKUP_AGE`` is the FULL-or-DIFF fallback
    for a database that has no such row at all. Rows from any other metric are ignored, so a caller
    may hand over its whole snapshot.
    """
    now = now or datetime.datetime.now()
    evidence: dict[str, dict] = {}

    def entry(database: str) -> dict:
        return evidence.setdefault(database, {
            "database": database, "recovery_model": "",
            "types": {backup_type: {"latest_finish": "", "age_hours": None, "seen": False}
                      for backup_type in BACKUP_TYPES},
            "as_of": "",
        })

    for row in rows:
        code = str(row.get("metric_code") or "")
        item = str(row.get("metric_item") or "")
        if code in BACKUP_LAST_RESULT_CODES:
            fields = _parse_kv(row.get("message"))
            database = fields.get("database") or item.split(" / ")[0].strip()
            if not database:
                continue
            record = entry(database)
            record["recovery_model"] = fields.get("recovery_model") or record["recovery_model"]
            record["as_of"] = max(record["as_of"], str(row.get("collected_at") or ""))
            backup_type = TYPE_ALIASES.get(str(fields.get("backup_type") or "").upper())
            if not backup_type:
                continue
            slot = record["types"][backup_type]
            slot["seen"] = True
            finish = fields.get("backup_finish_date") or ""
            if finish.upper() in ("NULL", "NONE"):
                finish = ""
            # The collector's own DATEDIFF is preferred: it is computed on the engine, against the
            # engine's clock, so it is not affected by the report host's timezone.
            age = as_float(row.get("metric_value"))
            age = age if (age is not None and age >= 0) else _age_hours(finish, now=now)
            if finish and (not slot["latest_finish"] or finish > slot["latest_finish"]):
                slot["latest_finish"] = finish
                slot["age_hours"] = age
            elif slot["age_hours"] is None:
                slot["age_hours"] = age
        elif code == "BACKUP_AGE" and item:
            record = entry(item)
            record["as_of"] = max(record["as_of"], str(row.get("collected_at") or ""))
            record["fallback_age_hours"] = as_float(row.get("metric_value"))
            record["fallback_status"] = str(row.get("status") or "")

    return evidence


def evaluate_backup_policy(rows: list[dict], *, server_id: str = "",
                           policy: dict | None = None,
                           now: datetime.datetime | None = None) -> dict:
    """Per-database compliance plus the server summary the Backup tile and fleet card render."""
    policy = policy or {}
    evidence = collect_evidence(rows, now=now)
    excluded = {str(name).lower() for name in (policy.get("exclude_databases") or [])}

    databases: list[dict] = []
    for database in sorted(evidence):
        if database.lower() in excluded:
            continue
        record = evidence[database]
        recovery = record["recovery_model"]
        types: dict[str, dict] = {}
        worst = "OK"
        reasons: list[str] = []
        for backup_type in BACKUP_TYPES:
            rule = _type_rule(policy, server_id=server_id, database=database, backup_type=backup_type)
            slot = record["types"][backup_type]
            age = slot["age_hours"]
            if age is None and backup_type == "FULL":
                age = record.get("fallback_age_hours")
            required = _is_required(rule, recovery)
            if not required:
                state = "NOT_REQUIRED"
            elif age is None:
                state = "CRITICAL"
                reasons.append(f"no {backup_type} backup on record")
            elif age >= float(rule.get("critical_hours", 48)):
                state = "CRITICAL"
                reasons.append(f"{backup_type} {_age_text(age)} old")
            elif age >= float(rule.get("warn_hours", 24)):
                state = "WARNING"
                reasons.append(f"{backup_type} {_age_text(age)} old")
            else:
                state = "OK"
            if _SEVERITY_RANK[state] > _SEVERITY_RANK[worst]:
                worst = state
            types[backup_type] = {
                "state": state, "required": required, "age_hours": age,
                "latest_finish": slot["latest_finish"],
                "warn_hours": rule.get("warn_hours"), "critical_hours": rule.get("critical_hours"),
            }
        databases.append({
            "database": database,
            "recovery_model": recovery,
            "status": worst,
            # Never "chain broken": see the module docstring. This names the violated objective.
            "reason": "; ".join(reasons),
            "types": types,
            "as_of": record["as_of"],
        })

    return {"databases": databases, "summary": _summarize(databases)}


def _age_text(hours: float) -> str:
    if hours < 48:
        return f"{round(hours)}h"
    return f"{round(hours / 24)}d"


def _summarize(databases: list[dict]) -> dict:
    if not databases:
        return {"status": "UNKNOWN", "compliant": 0, "eligible": 0, "reason": "",
                "byType": {}, "collectedAt": None, "worstDatabases": []}
    eligible = len(databases)
    compliant = sum(1 for record in databases if record["status"] == "OK")
    worst = "OK"
    for record in databases:
        if _SEVERITY_RANK[record["status"]] > _SEVERITY_RANK[worst]:
            worst = record["status"]

    by_type: dict[str, dict] = {}
    for backup_type in BACKUP_TYPES:
        required = [record for record in databases if record["types"][backup_type]["required"]]
        ok = [record for record in required if record["types"][backup_type]["state"] == "OK"]
        ages = [record["types"][backup_type]["age_hours"] for record in required
                if record["types"][backup_type]["age_hours"] is not None]
        by_type[backup_type] = {
            "required": len(required),
            "compliant": len(ok),
            # 0 required is not 100% healthy and not a failure: the type is simply not part of
            # this server's plan, and the page must say so rather than imply either.
            "state": "NOT_REQUIRED" if not required else ("OK" if len(ok) == len(required) else "VIOLATED"),
            "worstAgeHours": max(ages) if ages else None,
        }

    failing = sorted((record for record in databases if record["status"] in ("CRITICAL", "WARNING")),
                     key=lambda record: (-_SEVERITY_RANK[record["status"]], record["database"]))
    reason = ""
    if failing:
        reason = (f"{len(failing)} of {eligible} database(s) outside policy — "
                  f"{failing[0]['database']}: {failing[0]['reason']}")
    return {
        "status": worst,
        "compliant": compliant,
        "eligible": eligible,
        "reason": reason,
        "byType": by_type,
        "collectedAt": max((record["as_of"] for record in databases), default=""),
        "worstDatabases": [record["database"] for record in failing[:8]],
    }


def coverage_text(summary: dict) -> str:
    """"Full+Log · DIFF not required" — what the server's plan actually is.

    The old string was derived from "does any evidence of this type exist anywhere on the
    instance", which reported Full+Diff+Log for a server whose DIFF covered one database of six.
    """
    by_type = summary.get("byType") or {}
    present = [backup_type for backup_type in BACKUP_TYPES
               if (by_type.get(backup_type) or {}).get("required")]
    if not present:
        return "No policy match"
    text = "+".join(backup_type.title() for backup_type in present)
    optional = [backup_type for backup_type in BACKUP_TYPES
                if (by_type.get(backup_type) or {}).get("state") == "NOT_REQUIRED"]
    if optional:
        text += f" · {', '.join(optional)} not required"
    return text
