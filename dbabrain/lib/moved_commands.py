"""Commands that moved from one CLI to another - the table ``upgrade-config`` rewrites command lines by.

A command line written before a move still runs: the old CLI either lacks the command or answers
less. Nothing but ``upgrade-config``'s ``moved-commands`` step rewrites it - which is why a worker
moved to 0.23.0 kept /spbot_self_status on ``common.cli``, and reported every app's last run as
unknown (2026-09-26). A value, so it lives here and ``common`` imports it (rules R12): ``common``
may name no CLI module in its own code (R05).
"""

from __future__ import annotations

__all__ = ["MOVED_COMMANDS"]

#: (old module, command) -> the module that has the command now.
MOVED_COMMANDS: dict[tuple[str, str], str] = {
    # 0.24.0 (rules R43, the operator's choice): one self-status, and it is common's - it takes the
    # last-run column in its request. 0.23.0 had moved it the other way, for that column.
    ("dbabrain.db.cli", "self-status"): "dbabrain.common.cli",
    # 2026-08-15: they open the runtime store, which ORD 01 owns.
    ("dbabrain.common.cli", "ops-status"): "dbabrain.db.cli",
    ("dbabrain.common.cli", "queue-telegram-message"): "dbabrain.db.cli",
    ("dbabrain.common.cli", "restore-drill-status"): "dbabrain.db.cli",
    # 0.24.0 (rules R43, the operator's choice): one command for this node's clock, and it is db's -
    # it also takes the JSON request the common one did.
    ("dbabrain.common.cli", "timezone"): "dbabrain.db.cli",
}
