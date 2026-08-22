"""What a spec would do, decided before anything is touched.

Every check that can be made from the spec alone is made here, so a caller can ask "would this
work, and would it do what I meant" without a share being mounted or a database being dropped.
That is the whole value of a rehearsal: the expensive failures in a restore are the ones found
after the copy has moved gigabytes, or after the target's databases are already gone.

The plan also names the **method**, which the caller never states. It is derived from the spec -
how the source is reached and whether a container is named - so an operator does not have to know
which machinery their request implies, and cannot pick one that contradicts the rest of the spec.
"""

from __future__ import annotations

from typing import Any

from db_ops.lib.restore.pitr import assert_point_in_time_supported
from db_ops.lib.restore.spec import RestoreSpec, redacted

#: Drives the instance directly (sqlcmd/TDS) and selects the chain itself, so it can honour
#: STOPAT. Every target shape reaches it: a Windows VM, an Ubuntu VM, or a container.
ENGINE = "engine"
#: Ships a shell script that restores the newest chain. No STOPAT.
SCRIPT = "script"


def choose_method(spec: RestoreSpec) -> str:
    """Which machinery this spec implies.

    Derived rather than declared: a caller who had to name it could name one the rest of the spec
    contradicts, and the contradiction would only show up as a confusing failure at run time.
    A point-in-time request settles it outright - only the engine can honour one, and asking for a
    moment is an unambiguous statement of intent.
    """
    if spec.is_point_in_time:
        return ENGINE
    return SCRIPT if spec.extras.get("method") == SCRIPT else ENGINE


def plan_restore(spec: RestoreSpec) -> dict[str, Any]:
    """Everything decidable from the spec alone. Raises when the spec is self-contradictory."""
    method = choose_method(spec)
    # Checked here so a rehearsal refuses exactly what a real run would, rather than passing and
    # letting the run fail - a dry run that is more permissive than the thing it rehearses is
    # worse than no dry run at all.
    assert_point_in_time_supported(spec, method)

    # No "ok"/"operation" here: the response envelope owns those (common/response.py), and a
    # second copy inside `data` is a second thing to keep in step.
    return {
        "method": method,
        "restore_mode": "POINT_IN_TIME" if spec.is_point_in_time else "LATEST",
        "point_in_time": spec.point_in_time or None,
        "target": f"{spec.target.host}:{spec.target.port}"
                  + (f" ({spec.target.container})" if spec.target.container else ""),
        "source": f"{spec.source.access}:{spec.source.path}",
        "databases": list(spec.databases) or "all found in the backup set",
        "spec": redacted(spec),
    }
