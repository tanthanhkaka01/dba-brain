"""`AGENTS.md` is how an AI agent learns to drive an install - so every command it names must exist.

0.22.0 turned the first-run note into the whole operating guide for an agent (Codex, Claude, ...)
standing in a tool root after `pip install dbabrain`. An agent follows a document literally: a
subcommand that does not exist is not a typo it steps around, it is an error it will try to "fix".
`test_the_shipped_guide_is_true.py` already holds the first word of every `db-ops <app>`; this holds
the second - `db-ops common describe-object`, `db-ops db ops-status` - against each app's own
`--help`, so a renamed or removed command fails here instead of in an agent's session.

It also holds the two properties the operator asked for (2026-09-23): the guide is short enough to
be read whole (300-400 lines), and it is in the tool root and current - refreshed by `init` after an
upgrade, but never over a copy a person has edited.
"""

from __future__ import annotations

import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from db_ops import scaffold

GUIDE = scaffold.AGENTS_GUIDE

#: Words after `db-ops <app>` that are options or values, not subcommands.
_NOT_A_COMMAND = re.compile(r"^(-|@|'|\"|<|\{)")


@lru_cache(maxsize=None)
def _help(app: str) -> str:
    """What `db-ops <app>` answers to - its --help, plus the JSON-command tables some print bare."""
    outputs = []
    for argv in ([app, "--help"], [app]):
        result = subprocess.run([sys.executable, "-m", "db_ops.cli", *argv],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=120)
        outputs.append(result.stdout + result.stderr)
    return "\n".join(outputs)


def _commands_named() -> set[tuple[str, str]]:
    pairs = set()
    for line in GUIDE.splitlines():
        for match in re.finditer(r"db-ops ([a-z][a-z0-9-]+)((?: --config \S+)?) ([a-z][a-z0-9-]+)", line):
            app, _, sub = match.groups()
            # `restore-*` names a family; the regex stops at the star and would test "restore-".
            if not _NOT_A_COMMAND.match(sub) and not sub.endswith("-"):
                pairs.add((app, sub))
    return pairs


def test_the_guide_names_subcommands_at_all():
    """A regex that matches nothing passes every assertion below; that is not a check."""
    assert len(_commands_named()) >= 20


@pytest.mark.parametrize("app,sub", sorted(_commands_named()))
def test_every_subcommand_the_guide_names_exists(app, sub):
    from db_ops import cli

    if app not in cli.APPS:
        pytest.skip(f"{app} is a top-level verb, held by test_the_shipped_guide_is_true.py")
    # ops-status and self-status are JSON commands db.cli matches before argparse, so they are in
    # its dispatch table rather than in the argparse listing.
    if app == "db":
        from db_ops.db import cli as db_cli
        if sub in db_cli._JSON_COMMANDS:
            return
    assert re.search(rf"(?<![a-z-]){re.escape(sub)}(?![a-z-])", _help(app)), (
        f"AGENTS.md tells an agent to run `db-ops {app} {sub}`, and `db-ops {app} --help` does not "
        "list it.")


def test_the_guide_is_short_enough_to_be_read_whole():
    lines = len(GUIDE.splitlines())
    assert 250 <= lines <= 400, f"AGENTS.md is {lines} lines; the operator asked for 300-400"


def test_the_guide_never_teaches_a_secret_on_the_command_line():
    """secret-set reads stdin only; a guide showing it inline teaches the leak it exists to stop."""
    for line in GUIDE.splitlines():
        if "secret-set" in line and "db-ops common secret-set" in line:
            assert line.rstrip().endswith("secret-set -"), line


# --------------------------------------------------------------------------- #
# In the tool root, and current
# --------------------------------------------------------------------------- #
def test_init_puts_the_guide_in_the_tool_root(tmp_path):
    scaffold.initialise(tmp_path)
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("<!-- written by dbabrain init")
    assert text.endswith(GUIDE)


def test_an_untouched_guide_is_refreshed_after_an_upgrade(tmp_path):
    """`init` used to skip an existing AGENTS.md, so every upgrade left the old guide in place."""
    scaffold.initialise(tmp_path)
    path = tmp_path / "AGENTS.md"
    old_body = "# the guide an older build wrote\n"
    import hashlib
    stamp = hashlib.sha256(old_body.encode("utf-8")).hexdigest()[:16]
    path.write_text(f"<!-- written by dbabrain init 0.1.0; sha256 {stamp} - edit it -->\n{old_body}",
                    encoding="utf-8")

    result = scaffold.initialise(tmp_path)

    assert "AGENTS.md" in result.written
    assert path.read_text(encoding="utf-8").endswith(GUIDE)


def test_a_guide_a_person_edited_is_saved_and_then_replaced(tmp_path):
    """The operator, 2026-09-24: AGENTS.md must follow the installed version - 0.22.0 and 0.23.0
    changed too much to leave an old guide in place, and a kept one told an agent the previous
    version's field names. What the person wrote is saved first, and init says where."""
    scaffold.initialise(tmp_path)
    path = tmp_path / "AGENTS.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nOur notes: never restore on Fridays.\n",
                    encoding="utf-8")

    result = scaffold.initialise(tmp_path)

    assert "AGENTS.md" in result.written
    assert path.read_text(encoding="utf-8") == scaffold.rendered_guide()
    saved = result.guide_saved_copy
    assert saved is not None and saved.parent == tmp_path / scaffold.GUIDE_BACKUP_DIR
    assert "never restore on Fridays" in saved.read_text(encoding="utf-8")


def test_a_guide_from_before_the_stamp_is_saved_and_then_replaced(tmp_path):
    """0.21.0 and earlier wrote no stamp, so nothing can prove such a file untouched: it is treated
    as edited."""
    path = tmp_path / "AGENTS.md"
    path.write_text("# Running this toolkit for the first time\nold text\n", encoding="utf-8")

    outcome, saved = scaffold.write_guide(tmp_path)

    assert outcome == "replaced"
    assert saved.read_text(encoding="utf-8").endswith("old text\n")
    assert path.read_text(encoding="utf-8") == scaffold.rendered_guide()


def test_this_builds_guide_is_left_alone_and_nothing_is_saved(tmp_path):
    scaffold.write_guide(tmp_path)

    assert scaffold.write_guide(tmp_path) == ("unchanged", None)
    assert not (tmp_path / scaffold.GUIDE_BACKUP_DIR).exists()


def test_guide_write_puts_it_here_before_init(tmp_path, monkeypatch, capsys):
    """Before `init` there is no root; an agent opened in this directory still needs the file."""
    from db_ops import cli

    monkeypatch.chdir(tmp_path)
    assert cli.main(["guide", "--write"]) == 0
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").endswith(GUIDE)
    assert cli.main(["guide", "--write"]) == 0
    assert "already this build's guide" in capsys.readouterr().out


#: Old names that still name something current elsewhere, so a guide may write them as keys:
#: `enabled` on a feature block, a backup job's `env`, the `notes` every data file documents itself
#: with, a parameter's `name`, `groups` / `operator` / `engine` in their own files, `password_env`
#: for an environment variable. Everything else `field_names` retired means nothing any more.
STILL_CURRENT_ELSEWHERE = {"enabled", "env", "notes", "name", "groups", "operator", "engine",
                           "password_env", "database"}


def test_the_guide_writes_no_retired_field_name():
    """0.22.0 renamed the configuration and 0.23.0 kept going; a guide that shows an agent
    `"sql_name":` or `"ord":` teaches it the name every check now reports as deprecated. The
    guide may NAME an old spelling - its own table says what replaced what - but never write one."""
    from db_ops.lib import field_names

    retired = ({old for renames in field_names.RENAMES.values() for old in renames}
               | {old for moved in field_names.MOVED_ONLY.values() for old in moved})
    written = sorted(old for old in retired - STILL_CURRENT_ELSEWHERE if f'"{old}":' in GUIDE)
    assert written == []
