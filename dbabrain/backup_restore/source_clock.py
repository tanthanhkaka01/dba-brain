"""The source database's clock, for an Oracle or PostgreSQL point in time (0.40.0, 1.385).

A point in time is read in the source's clock: Oracle's ``UNTIL TIME`` against the times its own
archived logs carry, PostgreSQL's ``recovery_target_time`` without an offset in the cluster's own
``timezone``. Every lab and drill ran on UTC, so the restore took the database's clock to be UTC
("every container target here runs on UTC") - from a source on +07 an Oracle restore would have stopped
seven hours off and said it was done. The operator, 2026-11-16: *build it in 0.40.0* - ask the source
database its offset, and read the moment by it.

The database is asked, read only (``run-sql``: Oracle ``SYSTIMESTAMP``, PostgreSQL ``now()``), with
the login the inventory names for it. An Oracle backup that records its RMAN chain records the offset
beside it (``record_chain``), so a restore from the repository still has it when the source no longer
answers. What could not be read is said: a moment that needs it is refused, never assumed UTC.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The engines whose point in time is read in the source's clock by this module (SQL Server reads
#: its offset off its own backup files: ``restore_by_id.source_offset_minutes``).
ENGINES = ("oracle", "postgresql")

_SQL = {
    "oracle": "SELECT TO_CHAR(SYSTIMESTAMP, 'TZH:TZM') AS utc_offset FROM dual",
    "postgresql": "SELECT to_char(now(), 'OF') AS utc_offset",
}
_OFFSET = re.compile(r"^\s*([+-])(\d{1,2})(?::?(\d{2}))?\s*$")


def parse_offset(text: Any) -> int | None:
    """``+07:00`` / ``+07`` / ``-03:30`` / ``+0530`` as minutes east of UTC; ``None`` when it is not one."""
    match = _OFFSET.match(str(text or ""))
    if not match:
        return None
    minutes = int(match.group(2)) * 60 + int(match.group(3) or 0)
    if minutes > 14 * 60:
        return None
    return minutes if match.group(1) == "+" else -minutes


def ask(server_id: str, *, db_type: str, data_dir: Any = None,
        secrets: dict[str, str] | None = None) -> tuple[int | None, str]:
    """``(offset in minutes, "")``, or ``(None, why)`` when the source's clock could not be read."""
    engine = str(db_type or "").strip().lower()
    if engine == "postgres":
        engine = "postgresql"
    if engine not in _SQL:
        return None, f"{engine or 'this engine'} is not read here"
    from dbabrain.lib.data_sources import request_fill
    from dbabrain.transport import common_cli

    try:
        connection = request_fill.sql_connection(server_id, data_dir=data_dir, secrets=secrets)
        answer = common_cli.run("run-sql", {"connection": connection, "sql_text": _SQL[engine], "max_rows": 1,
                                            "timeout_seconds": 30, "connect_timeout_seconds": 15},
                                timeout_seconds=120)
    except Exception as exc:  # noqa: BLE001 - said as the reason the clock is unknown
        return None, " ".join(f"{server_id}'s clock not read - {type(exc).__name__}: {exc}".split())[:300]
    rows = answer.get("rows") or ((answer.get("result_sets") or [{}])[0].get("rows") or [])
    value = rows[0][0] if rows and rows[0] else None
    minutes = parse_offset(value)
    if minutes is None:
        return None, f"{server_id} answered {value!r} for its clock's offset"
    return minutes, ""


def recorded(catalog: Any, server_id: str) -> int | None:
    """The offset an Oracle backup of ``server_id`` recorded beside its RMAN chain, newest first."""
    if catalog is None or not hasattr(catalog, "oracle_chain_rows"):
        return None
    try:
        runs, _files = catalog.oracle_chain_rows(server_id=server_id)
    except Exception:  # noqa: BLE001 - nothing recorded is the answer
        return None
    for run in reversed(runs):
        try:
            minutes = json.loads(str(run.get("rman_chain_json") or "{}")).get("clock_offset_minutes")
        except ValueError:
            continue
        if isinstance(minutes, int):
            return minutes
    return None
