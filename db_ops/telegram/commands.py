from __future__ import annotations

from pathlib import Path

from db_ops.db import DbOpsStore


DEFAULT_COMMAND_PREFIX = "/spbot"


def can_run_command(*, allow_command: int, user_type: int, command_type: int) -> bool:
    """Level gate keyed on the command's required level ``command_type``.

    Convention:
    - ``command_type < 0``  -> disabled: never runs (use -1 to turn a command off).
    - ``command_type == 0`` -> public: runs for everyone, no clearance needed.
    - ``command_type >= 1`` -> the chat and the user must each be cleared at least to that level:
      ``allow_command >= command_type`` AND ``user_type >= command_type``.

    Level-based, not a fixed enumeration, so new levels (3, 4, ... 100) work with no code change:
    a level-5 command needs a level-5 (or higher) group and a level-5 (or higher) user. Disabling
    is ``-1`` (not 0), because 0 is the public tier."""
    if command_type < 0:
        return False
    if command_type == 0:
        return True
    return allow_command >= command_type and user_type >= command_type


def save_command_messages_from_messages(
    *,
    sqlite_path: str | Path,
    command_prefix: str = DEFAULT_COMMAND_PREFIX,
) -> dict[str, int | str]:
    store = DbOpsStore(sqlite_path)
    saved = store.sync_telegram_command_messages(command_prefix=command_prefix)
    return {
        "ok": True,
        "command_prefix": command_prefix,
        "saved": saved,
    }
