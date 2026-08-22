"""Restore as a library: a JSON object in, a result out. No config, no data, no app.

`db_ops/common/` is meant to be built and shipped on its own. Everything a restore needs arrives
in the request, so a packaged copy works in any directory with nothing beside it. The apps are
front ends: they read `restore_config.json`, look up the host, decrypt the password, and then call
this - see `db_ops/backup_restore/spec_builder.py`.

Four small pieces, each replaceable on its own:

* :mod:`db_ops.lib.restore` - what a restore is, as data: parsing, the point-in-time refusal,
  and the plan. Moved to ``lib`` on 2026-08-15 because ``backup_restore/spec_builder.py`` needs
  to build and validate a spec in-process, long before there is anything to restore to.
* :mod:`.sqlserver` - the SQL Server restore itself. Stayed here: it is the operation.
"""

# Re-exported from db_ops.lib.restore, where the spec vocabulary now lives. Kept so that
# `from db_ops.common.restore import parse_restore_spec` still reads naturally inside `common`
# itself — the CLI and the SQL Server runner both build a spec before performing one.
from db_ops.lib.restore.pitr import assert_point_in_time_supported  # noqa: F401
from db_ops.lib.restore.plan import ENGINE, SCRIPT, plan_restore  # noqa: F401
from db_ops.lib.restore.spec import (  # noqa: F401
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
    "run_restore",
]


def run_restore(spec: RestoreSpec, plan: dict) -> dict:
    """Perform the restore the spec describes.

    A plain `if` on the engine, not a plugin registry: there is one engine today, `common` owns it,
    and a registry would be indirection whose only reader is this function. A second engine adds a
    second branch here - and that is the moment to reconsider, not before.
    """
    if spec.db_type == "sqlserver":
        from db_ops.common.restore.sqlserver.runner import execute_restore_spec

        return execute_restore_spec(spec, plan)
    raise RestoreSpecError(f"No restore implementation for db_type {spec.db_type!r}.")
