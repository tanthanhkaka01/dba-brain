"""What a node's configuration says about the code's earlier name, and what it says after 0.29.0.

The package was renamed in 0.29.0 with nothing kept for the earlier name (the operator, 2026-10-12):
no second package, no second command, no second set of environment variables. A node's ``data/``
still names the code in four ways, and each would stop working the morning after the upgrade:

- a **module path** - ``python -m <old>.sql_tasks.runner``, ``["{python}", "-m", "<old>.common.cli",
  ...]`` in an app command or a bot command;
- a **path into the package** - ``../<old>/sre/automation``, a collector's SQL file;
- the **tool-root alias** ``tools/<old>`` an app command's ``working_dir`` carries, and the
  container's ``/app/tools/<old>`` - both of these with either slash, as a Windows node writes
  them;
- the **name of an environment variable** the code reads, where a node hands a value to a script
  (an instance's ``env``, a bot command's answer passed on as ``<OLD>_NEW_DB_PASSWORD``).

Everything else in a node's configuration that carries the old name is a value that lives outside
the code and keeps it: the store's database and schema and its tables (``<old>.job_runs`` is a
table in that schema), folders on a host (``/opt/<old>/backup``), a record's id, a secret's ref.
So the rules below are narrow on purpose - a module or package path only when what follows it is
one of the package's own components, and an environment variable only when this version reads it.

The old spelling is written here, and in the step that runs these rules, because finding it is
the whole job; the guard that holds the tree to the new name allows these two files for it.
"""
from __future__ import annotations

import re

#: The earlier name, in the two spellings a node's configuration can carry.
OLD_PACKAGE = "db_ops"
OLD_ENV_PREFIX = "DB_OPS_"
NEW_PACKAGE = "dbabrain"
NEW_ENV_PREFIX = "DBABRAIN_"

#: The package's components, and the modules at its root a command line can name.
COMPONENTS = ("backup_restore", "common", "control", "db", "jobs", "lib", "logging_ops", "metrics",
              "reports", "sla", "sql_tasks", "sre", "telegram", "transport", "webhost", "cli")

_COMPONENT = "|".join(COMPONENTS)
#: A path separator as a node may have written it. The daemon reads `tools\<old>` as the same
#: alias as `tools/<old>`, so a rule that knew one slash left a Windows node's `working_dir`
#: naming a folder nothing resolves any more - and `tools\<old>\<old>\metrics\x.sql` half renamed.
#: One or more, because a value read as raw text out of a JSON file carries its backslash doubled.
_SEP = r"(?:/|\\+)"
#: `<old>.common.cli`, `-m <old>.cli` - a dotted module path into one of the components.
_MODULE = re.compile(rf"(?<![\w/.\-]){OLD_PACKAGE}(?=\.(?:{_COMPONENT})\b)")
#: `../<old>/sre/automation`, `tools/<old>/<old>/metrics/...` - a file path into a component, to
#: the component's own folder (`../<old>/sre`) or beyond it.
_PATH = re.compile(rf"(?<![\w.\-]){OLD_PACKAGE}(?={_SEP}(?:{_COMPONENT})(?:[/\\]|\.py\b|(?![\w.\-])))")
#: What can hold the package, as the path part just before it: a relative step, the tool root
#: under either name (the alias is renamed first, so `tools/<old>/<old>/metrics` arrives here as
#: `tools/<new>/<old>/metrics`), a checkout folder named like the package, an interpreter's
#: package folder. Any other part before it - `/opt/<old>/reports/a.html` - is a folder on a host
#: that is only named like a component: configuration's, and it keeps its name (the operator,
#: 2026-10-12: what belongs to configuration is not changed, what belongs to the code is).
_HOLDS_THE_PACKAGE = frozenset({"..", ".", OLD_PACKAGE, NEW_PACKAGE, "site-packages", "dist-packages"})
#: The path part that ends where a match begins; nothing when the path begins at the match.
_PART_BEFORE = re.compile(r"([^/\\\s\"'=,;:(\[{]*)(?:/|\\+)$")
#: `python -m <old>` - the package itself, run as a module.
_BARE = re.compile(rf"(?<=-m ){OLD_PACKAGE}(?![\w.])")
#: `tools/<old>` - the tool-root alias, and the container's `/app/tools/<old>`.
_TOOLS = re.compile(rf"(?<![\w.\-])tools({_SEP}){OLD_PACKAGE}(?![\w\-])")
#: `<OLD>_TARGET_HOST` - a whole environment-variable name.
_ENV = re.compile(rf"(?<![A-Za-z0-9_]){OLD_ENV_PREFIX}([A-Z0-9_]+)(?![A-Za-z0-9_])")


def _package_path(match: re.Match[str]) -> str:
    """The new name where the old one is the package's folder; the match as it is where the part
    before it says it is a folder on a host."""
    before = _PART_BEFORE.search(match.string, 0, match.start())
    if before is None or before.group(1) in _HOLDS_THE_PACKAGE:
        return NEW_PACKAGE
    return match.group(0)


def renamed(text: str, env_names: frozenset[str] | set[str] = frozenset()) -> str:
    """*text* with every reference to the code under its earlier name pointed at the new one.

    *env_names* are the environment variables this version reads (``DBABRAIN_...``); an old name is
    renamed only when its new spelling is one of them, so an id or a secret ref that merely starts
    with the old prefix is left as it is.
    """
    text = _TOOLS.sub(lambda match: f"tools{match.group(1)}{NEW_PACKAGE}", text)
    text = _MODULE.sub(NEW_PACKAGE, text)
    text = _BARE.sub(NEW_PACKAGE, text)
    text = _PATH.sub(_package_path, text)
    if env_names:
        text = _ENV.sub(lambda match: (NEW_ENV_PREFIX + match.group(1)
                                       if NEW_ENV_PREFIX + match.group(1) in env_names
                                       else match.group(0)), text)
    return text


#: Every environment-variable name a piece of source text reads or sets.
_NEW_ENV_NAME = re.compile(rf"(?<![A-Za-z0-9_]){NEW_ENV_PREFIX}[A-Z0-9_]*[A-Z0-9]")


def env_names_in(text: str) -> set[str]:
    """The ``DBABRAIN_...`` names *text* mentions - what :func:`renamed` may rename to."""
    return set(_NEW_ENV_NAME.findall(text))
