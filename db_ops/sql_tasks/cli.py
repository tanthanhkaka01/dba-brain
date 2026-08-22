"""CLI facade for the sql_tasks app (uniform ``<app>.cli`` entrypoint).

Delegates verbatim to :func:`db_ops.sql_tasks.runner.main` — the documented
``python -m db_ops.sql_tasks.runner`` entrypoint keeps working unchanged.

**Registering** a task is not here and not in this app: it is
``python -m db_ops.common.cli add-sql '<json>'``. There used to be a
``db_ops.sql_tasks.config_admin`` shim re-exporting the shared engine, deleted on 2026-08-15 —
a second name for one command is a second thing to keep documented, and an app that imports the
engine is not calling the API, it is reaching through it.
"""

from __future__ import annotations

import sys

from db_ops.sql_tasks import runner


def main(argv: list[str] | None = None) -> int:
    return runner.main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
