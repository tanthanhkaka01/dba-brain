"""The Telegram app's ``route`` / ``groups`` commands answer from ``lib.telegram_route``.

Routing is a function of the configuration, and since 0.24.0 the configuration parser is ``lib``'s
(``lib.config``), so every app reads the route in-process from ``lib.telegram_route`` instead of
starting this app's CLI for it (rules R07, R42). This app keeps the two commands for a person at a
shell, answered by the very same functions - re-exported here, defined once there.
"""

from __future__ import annotations

from db_ops.lib.notify_route import NO_ROUTE  # noqa: F401 - one definition, see that module
from db_ops.lib.telegram_route import (  # noqa: F401 - re-exported for this app's CLI
    STANDARD_LEVELS, groups, route_for_level, telegram_settings)
