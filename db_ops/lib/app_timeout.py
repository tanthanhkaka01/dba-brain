"""How long the daemon gives the app command this process is running for.

The daemon kills an app command at its ``time_window.timeout`` (``terminate`` then ``kill``), and
until 0.25.0 nothing told the app what that number was. An app that needs to stop *before* it is
killed - the Telegram send pass, which must not be cut off halfway through a row - had to carry a
number of its own, and the two drifted: 180 s of budget against a timeout an operator could change
in ``app_commands.json``.

So the daemon states it: every child it starts carries ``DB_OPS_APP_TIMEOUT_SECONDS`` when the
command has a timeout, and an app reads its budget from here. A process started by hand, or by a
scheduler that is not the daemon, has no such variable - nothing will kill it, so it has no budget.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Set by the daemon on each child it starts, to that command's timeout in seconds. Absent for a
#: command with no timeout (``timeout: 0``, a long-running service) and outside the daemon.
APP_TIMEOUT_ENV_VAR = "DB_OPS_APP_TIMEOUT_SECONDS"


def timeout_seconds(environ: Mapping[str, str] | None = None) -> int | None:
    """The timeout the daemon will enforce on this process, or ``None`` when there is none."""
    raw = str((os.environ if environ is None else environ).get(APP_TIMEOUT_ENV_VAR) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def budget_seconds(ratio: float, environ: Mapping[str, str] | None = None) -> float | None:
    """``ratio`` of the enforced timeout, or ``None`` when the process has no timeout."""
    timeout = timeout_seconds(environ)
    return None if timeout is None else timeout * float(ratio)
