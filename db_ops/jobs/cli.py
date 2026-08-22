"""CLI facade for the jobs app (uniform ``<app>.cli`` entrypoint).

The jobs app has two operational entrypoints; this facade dispatches to both so the
app follows the same ``python -m db_ops.<app>.cli`` convention as every other app:

* ``daemon`` (default) -> :func:`db_ops.jobs.daemon.main` — run the app-command daemon
* ``status``           -> :func:`db_ops.jobs.status.main` — one-shot node status JSON

``python -m db_ops.jobs.daemon`` and ``python -m db_ops.jobs.status`` keep working
unchanged — this module adds the conventional name, it does not move any logic.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "status":
        from db_ops.jobs import status

        return status.main(argv[1:])
    if argv and argv[0] == "daemon":
        argv = argv[1:]
    from db_ops.jobs import daemon

    return daemon.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
