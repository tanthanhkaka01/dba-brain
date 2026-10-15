"""CLI facade for the jobs app (uniform ``<app>.cli`` entrypoint).

The jobs app has two operational entrypoints; this facade dispatches to both so the
app follows the same ``python -m dbabrain.<app>.cli`` convention as every other app:

* ``daemon`` (default) -> :func:`dbabrain.jobs.daemon.main` — run the app-command daemon
* ``status``           -> :func:`dbabrain.jobs.status.main` — one-shot node status JSON

``python -m dbabrain.jobs.daemon`` and ``python -m dbabrain.jobs.status`` keep working
unchanged — this module adds the conventional name, it does not move any logic.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "status":
        from dbabrain.jobs import status

        return status.main(argv[1:])
    if argv and argv[0] == "daemon":
        argv = argv[1:]
    from dbabrain.jobs import daemon

    return daemon.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
