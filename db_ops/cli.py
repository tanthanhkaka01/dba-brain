"""The ``db-ops`` / ``dbabrain`` command: an entry point that dispatches, and nothing of its own.

``db-ops <component> ...`` runs that component's CLI; ``db-ops init`` and the other words in
:data:`ALIASES` run the ``common.cli`` command of the same name. Routing only: every component owns
its own parser and its own ``main``, and this stays a lookup - the moment it interprets a
component's arguments there are two parsers for one command, and they disagree the first time one
of them changes.

The root package ``db_ops`` is not an app and holds nothing of its own (rules R41). Until 0.24.0 this
file answered six commands itself and imported two apps, ``common``, ``db`` and ``logging_ops``, and
its docstring said it sat outside the import rules *by construction* - a module outside every rule
is where the rules stop being true. ``init``, ``guide``, ``encrypt-secret``, ``export-data`` and
``import-data`` are ``common.cli`` commands now (``common/cli_tool_root.py``), and the logging/store
smoke test (``--message`` / ``--recent`` / ``--export-sqlite-schema``) is gone - ``db.cli
export-sqlite-schema`` was already the store's own command. ``check-credentials`` was the last: it
asked the metrics target loader and a Telegram resolver, both ``lib.data_sources``' since, and it is
``common/cli_check_credentials.py`` now. ``tests/test_the_root_package_only_dispatches.py`` holds
the root to having none left.
"""

from __future__ import annotations

import sys
from pathlib import Path




#: Every app, by the name someone types. The value is the module holding that app's ``main`` —
#: imported only when it is asked for, which is what lets ``--help`` and ``--version`` work on an
#: install with no database driver at all. Importing all twelve up front would make the help text
#: depend on having pyodbc.
APPS: dict[str, str] = {
    "metrics": "db_ops.metrics.cli",
    "reports": "db_ops.reports.cli",
    "telegram": "db_ops.telegram.cli",
    "sla": "db_ops.sla.cli",
    "backup-restore": "db_ops.backup_restore.cli",
    "sql-tasks": "db_ops.sql_tasks.cli",
    "sre": "db_ops.sre.cli",
    "control": "db_ops.control.cli",
    "webhost": "db_ops.webhost.cli",
    "db": "db_ops.db.cli",
    "common": "db_ops.common.cli",
    "daemon": "db_ops.jobs.daemon",
}

#: Words a person types that run a ``common.cli`` command: the word, and the request used when none
#: is given. ``init`` and ``guide`` default to ``format: "txt"`` - they are what a first run types,
#: and a first run should read sentences, not an envelope. An option is a JSON key: ``db-ops init
#: '{"force": true}'`` (the flags these took until 0.24.0 are refused by name).
ALIASES: dict[str, str] = {
    "init": '{"format": "txt"}',
    "guide": '{"format": "txt"}',
    "encrypt-secret": "",
    "export-data": "",
    "import-data": "",
    # 0.24.0: the last command the root answered itself (rules R41). Bare, it checks this node.
    "check-credentials": "",
}


def installed_apps() -> dict[str, str]:
    """The entries of :data:`APPS` this installation actually has.

    `v0.1.0` of the public distribution ships seven of the fourteen components
    (:mod:`db_ops.lib.distribution`), so on an installed copy this table would otherwise advertise
    seven commands that raise `ModuleNotFoundError` the moment somebody runs the line the help text
    just offered them. Measured, not predicted: the first thin wheel built on 2026-08-22 listed all
    twelve apps and could run five.

    Detected rather than hardcoded, so one code path is right in both trees — the full checkout
    lists twelve and the thin wheel lists five, and neither needs to know which it is.

    `find_spec` **locates** a module without executing it, which is what keeps the dispatch lazy:
    importing twelve apps to print a help message would make `--help` depend on having an ODBC
    driver, so a slim install would crash while explaining how to use the tool.
    """
    import importlib.util

    present: dict[str, str] = {}
    for name, module in APPS.items():
        try:
            if importlib.util.find_spec(module) is not None:
                present[name] = module
        except (ImportError, ValueError):
            # A parent package that is not installed raises rather than returning None.
            continue
    return present


def _usage() -> str:
    from db_ops.lib.version import __version__

    apps = installed_apps() or APPS
    width = max(len(name) for name in apps)
    lines = [
        # ASCII only: this prints to whatever console the operator has, and the Windows one is
        # cp1252. An em dash there arrives as a question mark at best.
        f"db_ops {__version__} - database operations toolkit",
        "",
        "usage: db-ops <app> [args...]",
        "",
        "apps:",
    ]
    lines += [f"  {name.ljust(width)}  python -m {module}" for name, module in sorted(apps.items())]
    lines += [
        "",
        "  init                   create a tool root here: config, a SQLite store, empty inventory",
        "  guide                  print the getting-started guide (writes nothing)",
        "  encrypt-secret         encrypt secrets/secret_text.json into the store the tool reads",
        "  check-credentials      does every configured target resolve to a real login",
        "  export-data            write this machine's whole configuration to one JSON file",
        "  import-data            apply such a file, so this machine runs the same estate",
        "",
        "Each app takes its own arguments; ask it directly, e.g. `db-ops metrics --help`.",
        "Every app is also runnable as `python -m <module>`, which is what the daemon does.",
    ]
    return "\n".join(lines)


def _tool_root_exists() -> bool:
    """Is there a tool root here yet?

    `config.json` beside `data/` is what every app resolves against. Asking the filesystem rather
    than loading the config on purpose: this runs before anything is configured, and the answer
    has to be available when loading would fail.
    """
    here = Path.cwd()
    return (here / "config.json").is_file() and (here / "data").is_dir()


def first_run_banner() -> str:
    """What to print when there is nothing here yet.

    A list of twelve apps is the right answer to "what can this do" and the wrong one to "I have
    just installed this". None of those apps can run before a tool root exists, so leading with
    them sends the reader to twelve dead ends. Measured on a clean install: the directory holds
    `.venv` and nothing else, and `init` was thirteenth in the list.
    """
    from db_ops.lib.version import __version__

    return "\n".join([
        f"dbabrain {__version__} - database operations toolkit",
        "",
        "There is no tool root in this directory yet, so there is nothing to run against.",
        "",
        "  dbabrain init      create one here: config.json, data/, and a guide to fill it in",
        "  dbabrain guide     read that guide now, without creating anything",
        "",
        "`init` writes AGENTS.md beside the configuration - the shortest path from here to a",
        "first collection, written to be followed by a person or by an AI agent.",
        "",
        "Full documentation: https://github.com/tanthanhkaka01/dba-brain",
        "",
        "Already have a tool root? Run this from that directory, or `dbabrain --help`.",
    ])


def main(argv: list[str] | None = None) -> int:
    """Dispatch to a component, or to the ``common.cli`` command an alias names.

    A console script is called with no arguments, so ``argv`` defaults to the real ones; the tests
    pass a list instead, which is why it is a parameter at all.
    """
    import importlib

    argv = list(sys.argv[1:] if argv is None else argv)

    # Bare, in a directory with no tool root, is somebody who has just installed this. Twelve app
    # names is the right answer to "what can it do" and the wrong one to "what do I type now".
    if not argv and not _tool_root_exists():
        print(first_run_banner())
        return 0
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    if argv[0] in ("-V", "--version"):
        from db_ops.lib.version import __version__

        print(__version__)
        return 0

    word = argv[0].replace("_", "-")
    if word in ALIASES:
        rest = argv[1:]
        if not rest and ALIASES[word]:
            rest = [ALIASES[word]]
        return int(importlib.import_module("db_ops.common.cli").main([word, *rest]) or 0)

    if word in APPS:
        # A name this build does not ship gets a sentence, not a traceback: `ModuleNotFoundError:
        # db_ops.sre.cli` tells a reader their install is broken, and it is not. Asked through
        # `installed_apps()` rather than a bare `find_spec`, which raises when the parent package is
        # the missing one.
        present = installed_apps()
        if word not in present:
            print(
                f"db-ops: '{word}' is not in this build.\n"
                f"This distribution ships: {', '.join(sorted(present))}.\n"
                "The rest of the toolkit arrives in a later release.",
                file=sys.stderr,
            )
            return 2
        return int(importlib.import_module(present[word]).main(argv[1:]) or 0)

    print(f"db-ops: unknown app or command {argv[0]!r}\n", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
