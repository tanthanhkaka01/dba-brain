"""What a restore *is*, as data — parsing, validation, and the plan, with nothing performed.

Split from ``db_ops/common/restore/`` on 2026-08-15, when apps stopped importing ``common``.
The division is the one the original package docstring already drew, made structural:

* here — :mod:`.spec` (what a restore is), :mod:`.pitr` (a point-in-time request is refused,
  never quietly downgraded to "the newest chain"), :mod:`.plan` (what a spec would do, decided
  without touching anything);
* in ``common`` — the SQL Server restore itself, reached through the ``restore-database`` CLI.

``backup_restore/spec_builder.py`` is the reason it had to move: it reads ``restore_config.json``
and the encrypted store, then *builds a spec* — parsing and validation it needs in-process, long
before there is anything to restore to. That is a rule about values, so it belongs where every
component may import it.
"""

from db_ops.lib.restore.pitr import assert_point_in_time_supported
from db_ops.lib.restore.plan import ENGINE, SCRIPT, plan_restore
from db_ops.lib.restore.spec import (
    RestoreSpec,
    RestoreSpecError,
    SourceSpec,
    TargetSpec,
    parse_restore_spec,
    redacted,
)

__all__ = [
    "ENGINE", "SCRIPT", "RestoreSpec", "RestoreSpecError", "SourceSpec", "TargetSpec",
    "parse_restore_spec", "redacted", "assert_point_in_time_supported", "plan_restore",
]
