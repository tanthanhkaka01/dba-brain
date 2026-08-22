"""Whether a spec can be restored to a moment, and refusing when it cannot.

Its own module because it is the one rule in this API that must never be relaxed quietly, and a
rule with its own file is a rule somebody has to delete on purpose.

``STOPAT`` needs the restore to choose its log chain against a timestamp, which the engine path
does in Python and has no platform branch for - a Windows VM, an Ubuntu VM and a container target
all reach it identically. A restore driven by a shell script that picks "the newest chain" cannot
do it at all.

The refusal matters more than the capability. A caller told they recovered to 14:00 while actually
holding whatever the last log contained has been handed a wrong answer shaped exactly like a right
one, and will only discover it by reading data that should not exist yet. Refusing costs a message;
downgrading costs the recovery.
"""

from __future__ import annotations

from db_ops.lib.restore.spec import RestoreSpec, RestoreSpecError

#: Restore methods that can honour a point in time.
PITR_CAPABLE = frozenset({"engine"})


def assert_point_in_time_supported(spec: RestoreSpec, method: str) -> None:
    """Raise unless ``method`` can honour ``spec.point_in_time``. No-op when none was asked for."""
    if not spec.is_point_in_time:
        return
    if method in PITR_CAPABLE:
        return
    raise RestoreSpecError(
        f"{spec.label or spec.db_type}: restore method {method!r} restores the newest chain and "
        "has no STOPAT - it cannot restore to a point in time. Point-in-time recovery needs the "
        f"engine method ({', '.join(sorted(PITR_CAPABLE))})."
    )
