"""The scheduler watching its dbabrain key: the daily online check, and the warnings (1.0.0, plan K5).

Backups that stop because a key ran out are the most dangerous thing a DBA tool can do - a schedule
that silently stops is a restore that will not exist. So the end of a key is said well before it,
and loudly after it (D1, the operator 2026-11-21: *warn before, and 3 days of grace*):

* **14, 7, 3 and 1 day(s) before the end** - once each, WARNING, the last CRITICAL;
* **every day of the grace** - CRITICAL: the gated work still runs, and stops when it ends;
* **every day after it, or after a revocation** - CRITICAL: new reports and backups have stopped;
* **no key at all** is not repeated here: a node that only collects metrics is a correct install
  (the scheduler's gate says it once per process - ``key_gate``).

**Once a day the key is checked online** (D5): ``common.cli key-status``, which keeps dbabrain.dev's
answer beside ``data/``. One try a day - a check that fails is not repeated every scan, and is not
a revocation. What was said is kept in ``runtime/dbabrain_key_said.json``, so a restart does not say
a step twice.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dbabrain.jobs.key_gate import KeyGate
from dbabrain.lib import dbabrain_key as dk

SAID_FILENAME = "dbabrain_key_said.json"


def said_file(data_dir: str | Path) -> Path:
    return dk.status_file(data_dir).with_name(SAID_FILENAME)


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(path: Path, said: dict[str, Any]) -> None:
    from dbabrain.lib.json_io import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(said, indent=2, sort_keys=True) + "\n")


def watch(
    data_dir: str | Path,
    gate: KeyGate,
    *,
    now: datetime,
    push: Callable[[str, str], None],
    check_online: Callable[[], None] | None = None,
    regate: Callable[[], KeyGate] | None = None,
) -> KeyGate:
    """One scan's watch: maybe the day's online check, then whatever is due to be said.

    Returns the gate to use - a new one when the online check changed what the node knows.
    """
    path = said_file(data_dir)
    said = _read(path)
    today = now.date().isoformat()
    if gate.standing.key is not None and check_online is not None and said.get("checked_on") != today:
        said["checked_on"] = today
        _write(path, said)  # first: one try a day, whatever the check does
        try:
            check_online()
        except Exception:  # noqa: BLE001 - a failed check is not a revocation; the offline answer stands
            pass
        if regate is not None:
            gate = regate()
    sentence = _due(gate.standing, said, today)
    if sentence is not None:
        level, text, mark = sentence
        said.update(mark)
        _write(path, said)
        push(level, text)
    return gate


def _due(standing: dk.KeyStanding, said: dict[str, Any], today: str) -> tuple[str, str, dict[str, Any]] | None:
    key = standing.key
    if key is None:
        return None
    renew = "Run `dbabrain key-renew` (an email sign-in on dbabrain.dev)."
    if standing.state == "active":
        step = standing.warning_due
        if step is None or said.get("warned", {}).get(key.kid) == step:
            return None
        days = max(0, int(standing.days_left or 0))
        level = "critical" if step == 1 else "warning"
        text = (f"The dbabrain key of {key.email} ends in {days} day(s), {_day(key.expires_at)} - then 3 days of "
                f"grace, then new reports and backups stop. {renew}")
        return level, text, {"warned": {**said.get("warned", {}), key.kid: step}}
    if said.get("loud_on", {}).get(key.kid) == today:
        return None
    mark = {"loud_on": {**said.get("loud_on", {}), key.kid: today}}
    if standing.state == "grace":
        return "critical", (f"The dbabrain key of {key.email} ended {_day(key.expires_at)}. Reports and backups still "
                            f"run until {_day(key.grace_ends_at)}, then stop. {renew}"), mark
    if standing.state in ("ended", "revoked"):
        return "critical", (f"New reports and backups have STOPPED: the dbabrain key of {key.email} - "
                            f"{standing.reason}. A restore you ask for still runs. {renew}"), mark
    return None


def _day(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")
