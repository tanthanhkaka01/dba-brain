"""Which console-editable config decides what runs, or who may run it.

A console account at `min_level_edit` (default 50) could rewrite `app_commands.command_text`, and
the daemon ran it with `shell=True` within a second, as the worker - which holds the passphrase to
every credential in the estate (review 0.25.0, F9.1). Combined with an XSS or a look-alike login,
that was code execution for anyone able to name a database on a monitored server.

Two kinds of change are therefore set apart, and the console asks more of them (admin level and
the user's password, re-entered): a change to a field that names something to execute or a host
to execute it on, and any change to a file that grants permissions.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["EXEC_FIELDS", "PRIVILEGE_FILES", "sensitive_changes"]

#: Per file: the top-level record fields that name code, a script, a host login or SQL to run.
EXEC_FIELDS: dict[str, frozenset[str]] = {
    "app_commands.json": frozenset({"command_text", "working_dir", "env", "node_role"}),
    "db_instances.json": frozenset({"cmd_access"}),
    "sql_commands.json": frozenset({"script_path", "script_paths", "script_files",
                                    "final_script_files", "input", "input_type"}),
    "telegram_support_commands.json": frozenset({"action_config", "action_type", "command_argv"}),
    "metric_definitions.json": frozenset({"variants", "collector_type"}),
    "restore_config.json": frozenset({"script", "env", "jobs", "source", "target"}),
}

#: Files where every change is a change of who may do what: levels, logins, the confirmation
#: ladder, the console's own settings, where the store lives.
PRIVILEGE_FILES: frozenset[str] = frozenset({
    "telegram_users.json", "users.json", "emergency_operations.json", "webhost_config.json",
    "store_config.json", "data_files.json",
})


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def sensitive_changes(source_file: str, before: dict[str, Any] | None,
                      after: dict[str, Any] | None) -> list[str]:
    """What in this change needs the stronger check - empty when nothing does.

    ``before`` is the stored record (``None`` for a new one), ``after`` the submitted one (``None``
    for a retirement). Retiring a record never needs it: it removes something that would run, it
    cannot add one.
    """
    name = str(source_file or "").rsplit("/", 1)[-1]
    if after is None:
        return []
    if name in PRIVILEGE_FILES:
        return [f"{name} (permissions)"]
    fields = EXEC_FIELDS.get(name, frozenset())
    old = before or {}
    return sorted(field for field in fields
                  if (field in old or field in after) and not _same(old.get(field), after.get(field)))
