"""Has a restore actually been proven lately? — the one question backups cannot answer.

"There is a backup" and "we can restore" are different claims, and only one of them can be tested.
The estate already tests it: :mod:`db_ops.backup_restore` runs drills and records every one in
``backup_restore_history`` (505 successes and 26 failures at the time of writing). Nothing turned
that into a health signal, so the reports carried static restore evidence from 2026-06-20 while the
table held a success from 2026-08-02 — and, more importantly, held ``APPDB_STG_DOCKER`` whose newest
success was 43 days old with nobody saying so.

This module is only the reading of that table against a policy. It does not run drills and does not
know how to — that is ``backup_restore``'s job, and the two never import each other; ``reports``
reaches this the same way it reaches :mod:`db_ops.lib.backup_policy`.

Two distinctions the verdict has to keep:

* **Never drilled is not the same as drilled and stale.** A database nobody has ever restored has
  no evidence at all; one drilled six weeks ago has evidence that has expired. Both need action,
  and they are different actions.
* **A newer failure outranks an older success.** A database whose last *attempt* failed is not
  covered by the success before it — that is precisely the case where somebody assumes they are
  protected.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from db_ops.lib.coerce import as_utc_datetime
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module

DEFAULT_POLICY_PATH = TOOL_ROOT / "data" / "restore_drill_policy.json"

#: One week. A restore drill proves the path still works — the backup format, the credentials, the
#: destination, the free space, the script. A week is short enough that a break is found before the
#: month-end close depends on it, and long enough not to thrash a large database nightly.
DEFAULT_MAX_AGE_HOURS = 24 * 7

_POLICY_CACHE: dict[str, dict] = {}


def load_restore_drill_policy(path: str | Path | None = None) -> dict:
    """The policy document, cached. A missing or unreadable file yields the built-in defaults."""
    source = Path(path or DEFAULT_POLICY_PATH)
    key = str(source)
    if path is None and key in _POLICY_CACHE:
        return _POLICY_CACHE[key]
    try:
        document = json.loads(source.read_bytes().decode("utf-8-sig"))
        policy = document.get("restore_drill_policy") if isinstance(document, dict) else None
    except (OSError, ValueError, AttributeError):
        policy = None
    policy = policy if isinstance(policy, dict) else {}
    if path is None:
        _POLICY_CACHE[key] = policy
    return policy


def max_age_hours(policy: dict | None = None, *, database: str = "",
                  override: float | None = None) -> float:
    """How old a successful drill may be before it stops counting as evidence.

    ``override`` wins over everything, so a caller (a CLI request object, a report) can ask a
    different question without editing config.
    """
    if override is not None:
        return float(override)
    policy = policy if policy is not None else load_restore_drill_policy()
    hours = float((policy.get("defaults") or {}).get("max_age_hours", DEFAULT_MAX_AGE_HOURS)
                  or DEFAULT_MAX_AGE_HOURS)
    for entry in policy.get("overrides") or []:
        wanted = str(entry.get("database") or "").strip().lower()
        if wanted and wanted == str(database or "").strip().lower():
            hours = float(entry.get("max_age_hours", hours) or hours)
            break
    return hours


# `_epoch` is `db_ops.lib.coerce.as_utc_datetime` since 2026-08-16; see there for why an
# ISO string with no timezone is read as UTC and not as local time.


def evaluate(rows: list[dict[str, Any]], *, policy: dict | None = None,
             override_hours: float | None = None, now: datetime | None = None) -> list[dict]:
    """One verdict per database from ``backup_restore_history`` rows.

    ``rows`` need ``database_name``, ``status`` and ``restore_start``. Returns one entry per
    database, worst first.
    """
    policy = policy if policy is not None else load_restore_drill_policy()
    now = now or datetime.now(timezone.utc)

    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        database = str(row.get("database_name") or "").strip()
        started = as_utc_datetime(row.get("restore_start"))
        if not database or started is None:
            continue
        status = str(row.get("status") or "").strip().upper()
        entry = latest.setdefault(database, {"success": None, "attempt": None, "attempt_status": ""})
        if entry["attempt"] is None or started > entry["attempt"]:
            entry["attempt"] = started
            entry["attempt_status"] = status
        if status == "SUCCESS" and (entry["success"] is None or started > entry["success"]):
            entry["success"] = started

    out: list[dict] = []
    for database, entry in latest.items():
        limit = max_age_hours(policy, database=database, override=override_hours)
        success, attempt = entry["success"], entry["attempt"]
        age_hours = round((now - success).total_seconds() / 3600.0, 1) if success else None
        if success is None:
            status, reason = "CRITICAL", "no successful restore drill has ever been recorded"
        elif age_hours > limit:
            status = "WARNING" if age_hours <= limit * 2 else "CRITICAL"
            reason = (f"newest successful drill is {age_hours}h old, over the "
                      f"{limit:.0f}h policy")
        else:
            status, reason = "OK", f"proven {age_hours}h ago, within the {limit:.0f}h policy"
        # A newer failure is not covered by an older success: that is exactly when somebody
        # believes they are protected and is not.
        if attempt is not None and success is not None and attempt > success \
                and entry["attempt_status"] == "FAILED":
            status = "CRITICAL"
            reason = (f"the most recent attempt FAILED ({attempt:%Y-%m-%d %H:%M} UTC); the last "
                      f"success before it was {age_hours}h ago")
        out.append({
            "database": database,
            "status": status,
            "ageHours": age_hours,
            "maxAgeHours": limit,
            "lastSuccess": success.strftime("%Y-%m-%dT%H:%M:%SZ") if success else None,
            "lastAttempt": attempt.strftime("%Y-%m-%dT%H:%M:%SZ") if attempt else None,
            "lastAttemptStatus": entry["attempt_status"],
            "reason": reason,
        })
    rank = {"CRITICAL": 0, "WARNING": 1, "OK": 2}
    out.sort(key=lambda r: (rank.get(r["status"], 3), -(r["ageHours"] or 0)))
    return out


def summarize(results: list[dict]) -> dict[str, Any]:
    """Fleet-shaped counts, for a report header or a CLI reply."""
    counts = {"CRITICAL": 0, "WARNING": 0, "OK": 0}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "databases": len(results),
        "compliant": counts["OK"],
        "warning": counts["WARNING"],
        "critical": counts["CRITICAL"],
        "status": "CRITICAL" if counts["CRITICAL"] else ("WARNING" if counts["WARNING"] else "OK"),
    }
