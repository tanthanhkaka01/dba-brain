"""Every command the README, the docs and the example guides show would run on this version.

A guide is followed literally - by a person on their first day, and by an agent that treats an error
as something to fix. 0.24.0 moved `timezone` to `db.cli` and made every `common.cli` operation take
its login in the request (rules R09), and the guides went on saying the old thing: `db-ops common
timezone`, and `run-sql` / `db-status` / the host and instance commands sent with only a
`"target"` - which 0.24.0 refuses. Nothing checked them: `test_the_agent_guide_names_only_real_
commands.py` holds `AGENTS.md` and nothing else. This holds the rest, in two ways:

* every `db-ops <app> <command>` and `python -m db_ops.<app>.cli <command>` names a command that
  CLI has;
* a request sent to a `common.cli` command that takes a login (`request_fill.FILLS`) carries it -
  `"connection"` or `"access"` - or comes from an `@file`, which the reader writes.

The release notes and the changelog are history - they say what a version did, including commands
that later moved - so they are not read.
"""

from __future__ import annotations

import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from db_ops.lib.data_sources import request_fill

REPO = Path(__file__).resolve().parents[1]

GUIDES = sorted(
    [REPO / "README.md"]
    + list((REPO / "docs").glob("*.md"))
    + list((REPO / "examples").rglob("*.md"))
)

#: `db-ops <app> [--config X] <command>` and `python -m db_ops.<app>.cli <command>`.
_DB_OPS = re.compile(r"db-ops ([a-z][a-z0-9-]+)(?: --config \S+)? ([a-z][a-z0-9-]+)(?![a-z0-9-])")
_MODULE = re.compile(r"-m db_ops\.([a-z_]+)\.cli(?: --config \S+)? ([a-z][a-z0-9-]+)(?![a-z0-9-])")
_COMMON = re.compile(r"(?:db-ops common|-m db_ops\.common\.cli)(?: --config \S+)? ([a-z][a-z0-9-]+)(.*)$")

#: The `common.cli` commands a request must carry a login for.
TAKES_A_LOGIN = frozenset(command for command, needs in request_fill.FILLS.items()
                          if request_fill._SQL in needs or request_fill._HOST in needs)


def _code_blocks(text: str) -> list[list[str]]:
    blocks, current, inside = [], [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if inside:
                blocks.append(current)
            current, inside = [], not inside
            continue
        if inside:
            current.append(line)
    return blocks


def _invocations() -> list[tuple[str, int, str, str]]:
    """``(guide, line number, app, command)`` for every command a guide shows."""
    from db_ops import cli

    found = []
    for path in GUIDES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in _DB_OPS.finditer(line):
                app, command = match.groups()
                if app in cli.APPS and app != "daemon" and not command.endswith("-"):
                    found.append((path.relative_to(REPO).as_posix(), number, app, command))
            for match in _MODULE.finditer(line):
                module, command = match.groups()
                app = next((name for name, target in cli.APPS.items()
                            if target == f"db_ops.{module}.cli"), None)
                if app and not command.endswith("-"):
                    found.append((path.relative_to(REPO).as_posix(), number, app, command))
    return found


@lru_cache(maxsize=None)
def _help(app: str) -> str:
    """What `db-ops <app>` answers to - its --help, plus the tables some print bare. For `common`
    that is every command, the stdin-only ones (secret-set, smb-*, run-sqlcmd ...) included."""
    outputs = []
    for argv in ([app, "--help"], [app]):
        result = subprocess.run([sys.executable, "-m", "db_ops.cli", *argv], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=120)
        outputs.append(result.stdout + result.stderr)
    return "\n".join(outputs)


def _exists(app: str, command: str) -> bool:
    if app == "db":
        from db_ops.db import cli as db_cli
        if command in db_cli._JSON_COMMANDS:
            return True
    return bool(re.search(rf"(?<![a-z-]){re.escape(command)}(?![a-z-])", _help(app)))


def test_the_guides_show_commands_at_all():
    """A pattern that matches nothing passes every assertion below; that is not a check."""
    assert len(_invocations()) >= 50


def test_every_command_a_guide_shows_exists():
    missing = sorted({(guide, number, f"db-ops {app} {command}")
                      for guide, number, app, command in _invocations() if not _exists(app, command)})
    assert not missing, (
        "These guides show a command this version does not have - moved or removed. Point them at "
        "the one that does the job (lib/moved_commands.py names where each moved):\n"
        + "\n".join(f"  {guide}:{number}  {shown}" for guide, number, shown in missing))


def _statement_end(block: list[str], index: int) -> int:
    """The last line of the command on ``block[index]``: past `\\` continuations, and past a
    quoted request left open on its first line."""
    end = index
    while end + 1 < len(block):
        so_far = "\n".join(block[index:end + 1])
        if block[end].rstrip().endswith("\\") or so_far.count("'") % 2:
            end += 1
            continue
        break
    return end


def _login_free_requests() -> list[str]:
    offenders = []
    for path in GUIDES:
        for block in _code_blocks(path.read_text(encoding="utf-8")):
            start = 0
            for index, line in enumerate(block):
                if line.lstrip().startswith(("'{", "echo '{")):
                    start = index  # a request piped in: '{...}' | db-ops common <command> -
                match = _COMMON.search(line)
                if not match:
                    continue
                command, rest = match.groups()
                argument = rest.split("#")[0]
                # '<json>' is the command index's placeholder; an @file is the reader's own.
                if command not in TAKES_A_LOGIN or "@" in argument or "<json>" in argument:
                    start = index + 1
                    continue
                end = _statement_end(block, index)
                text = "\n".join(block[start:end + 1])
                if '"connection"' not in text and '"access"' not in text:
                    offenders.append(f"{path.relative_to(REPO).as_posix()}: {line.strip()[:110]}")
                start = end + 1
    return offenders


def test_a_request_that_needs_a_login_carries_one():
    """0.24.0: `common.cli` reads no configuration, so a `"target"` alone is refused. The bot and
    the SQL tasks fill the login in; a person at a shell states it (or writes it to an @file)."""
    offenders = _login_free_requests()
    assert not offenders, (
        "These guides send a login-taking command only a name, which this version refuses:\n  "
        + "\n  ".join(offenders))


@pytest.mark.parametrize("command", ["run-sql", "db-status", "run-cmd"])
def test_the_commands_that_broke_the_guides_are_among_those_held(command):
    assert command in TAKES_A_LOGIN
