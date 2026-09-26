"""No two commands - in any CLI - do the same job (rules R43).

R11 holds the *code* to one place: one function name, one body. It cannot see two **commands** that
reach the same code from different doors - `control.cli inventory-summary` and `common.cli
inventory-summary` both render the inventory with `lib.inventory_render`, so R11 is satisfied and a
person still has two commands to learn, document and keep in step. The operator, 2026-09-26: *no
two commands, in any CLI, perform the same function.* One job, one command; everything else that
needs the job calls that command.

A test cannot read what a command *does*, so it holds what can be counted - a command's **name**
across every CLI - and makes every other case a written decision:

* a name in two CLIs is either an app's **front door** to the `common.cli` command of that name - it
  finishes the request from the app's own `data/` and hands it on, which the test checks in the
  code - or **two different jobs** that share a word, each named with what it is;
* anything else is a **duplicate**, listed with the question the operator has to answer, in a list
  that may only shrink. Duplicates under different names (two commands that restore a SQL Server
  database) are listed there too, because only a person can find them.
"""

from __future__ import annotations

import ast
import collections
import importlib.util
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "db_ops"
_KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def _common_commands() -> set[str]:
    spec = importlib.util.spec_from_file_location("contract", REPO / "tests" / "test_common_cli_json_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.ALL_COMMANDS) | set(module.STDIN_ONLY_COMMANDS)


def _commands_by_cli() -> dict[str, set[str]]:
    """``{cli: {command}}`` for every CLI in the package - an argparse sub-command, a JSON command a
    CLI dispatches from a table before argparse (``db.cli``), and ``common.cli``'s own list."""
    found: dict[str, set[str]] = collections.defaultdict(set)
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts or len(path.relative_to(PACKAGE).parts) < 2:
            continue
        cli = path.relative_to(PACKAGE).parts[0]
        if cli == "common":
            continue  # its list is the contract test's, which already holds every handler to it
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # `for name in ("restore-full", "restore-diff", "restore-log"): add_parser(name, ...)` - the
        # form three hidden `backup_restore` commands were registered in, unseen by a scan that read
        # only literal arguments, until 0.24.0.
        looped: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                    and isinstance(node.iter, (ast.Tuple, ast.List))
                    and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.iter.elts)):
                looped[node.target.id] = [e.value for e in node.iter.elts]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_parser" and node.args):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found[cli].add(first.value)
            elif isinstance(first, ast.Name) and first.id in looped:
                found[cli].update(looped[first.id])
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                    and any(isinstance(t, ast.Name) and t.id == "_JSON_COMMANDS" for t in node.targets)):
                found[cli] |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    found["common"] = _common_commands()
    return {cli: {name for name in names if _KEBAB.match(name)} for cli, names in found.items()}


COMMANDS = _commands_by_cli()

#: An app command named like a `common.cli` command, which only finishes the request from the app's
#: own `data/` (and records what came back) - the job itself is `common.cli`'s. Checked in the code:
#: the app must hand the request to the command of the same name.
FRONT_DOORS: dict[str, str] = {
    "create-db-docker": "sre",   # resolves the lab spec from sre's config, registers what was built
    "move-db-docker": "sre",     # resolves both hosts from sre's config, updates the registry
}

#: One word, two jobs - each side named, so the next reader does not have to open both to find out.
DIFFERENT_JOBS: dict[str, str] = {
    "init": "common.cli creates a tool root (config, data/, a store); db.cli creates or upgrades the "
            "store's schema on the backend a root already names",
    "user-level": "telegram.cli sets a chat user's level in the bot's user list; webhost.cli sets a "
                  "web console account's level in the console's own accounts",
}

#: Commands that do one job twice - the debt of R43, each with the decision it waits on. A duplicate
#: leaves by one side becoming the other's front door, or being removed; it never grows.
DUPLICATES: dict[str, tuple[frozenset[str], str]] = {
    # Resolved 2026-09-26 (the operator): the inventory summary is common's, the inventory workflow
    # reports' - control's two copies went; this node's clock is db's timezone - common's went;
    # self-status is common's, the bot stating the last runs - db's went; verify-restore is
    # common's - backup_restore's standalone command went (its workflow already asked common's).
    # Resolved the same day (the operator: *merge it fully, now*): the SQL Server RESTORE is written
    # once, in common/restorestep/sqlserver.py. `restore-latest` asks common.cli restore-full/-diff/
    # -log for every step - the nightly's text byte for byte (tests/test_one_sqlserver_restore_
    # statement.py) - and the extra routes went: backup_restore's hidden restore-full/-diff/-log,
    # restore-by-id's adapter for an SMB entry, and common.cli restore-database. One route per
    # entry: restore-workflow runs an SMB entry through restore-latest, a script entry through
    # restore-by-id, and both through the same common steps.
}

#: The duplicates as written down on 2026-09-26 - a literal, so one added to the list above has
#: something to be compared with.
DUPLICATES_AT_0_24_0 = frozenset({
    "inventory summary", "inventory workflow", "verify a restore", "this node's clock",
    "this node's status", "restore a SQL Server backup",
})


def _named_in_two_clis() -> dict[str, set[str]]:
    owners: dict[str, set[str]] = collections.defaultdict(set)
    for cli, names in COMMANDS.items():
        for name in names:
            owners[name].add(cli)
    return {name: clis for name, clis in owners.items() if len(clis) > 1}


def _duplicate_names() -> set[str]:
    return {member.split(":", 1)[1] for members, _why in DUPLICATES.values() for member in members
            if len({m.split(":", 1)[1] for m in members}) == 1}


def test_every_cli_was_found():
    """A collector that silently found nothing would pass every test below."""
    assert {"common", "db", "control", "telegram", "reports", "sre", "backup_restore"} <= set(COMMANDS)
    assert len(COMMANDS["common"]) > 50 and len(COMMANDS["db"]) > 10


def test_a_command_name_in_two_clis_is_a_front_door_two_jobs_or_a_listed_duplicate():
    unaccounted = {name: sorted(clis) for name, clis in _named_in_two_clis().items()
                   if name not in FRONT_DOORS and name not in DIFFERENT_JOBS
                   and name not in _duplicate_names()}
    assert not unaccounted, (
        f"these commands exist in two CLIs and nothing here says why: {unaccounted}. One job is one "
        "command (rules R43): make one the other's front door, give them different names if they are "
        "different jobs, or - only if neither can be done now - list the pair in DUPLICATES.")


@pytest.mark.parametrize("name", sorted(FRONT_DOORS))
def test_a_front_door_hands_its_request_to_the_common_command_of_its_name(name):
    app = FRONT_DOORS[name]
    assert name in COMMANDS["common"] and name in COMMANDS.get(app, set()), (name, app)
    handed_on = re.compile(r"common_cli\.run(_allowing_failure)?\(\s*" + re.escape(f'"{name}"'))
    sources = [p.read_text(encoding="utf-8") for p in (PACKAGE / app).rglob("*.py")]
    assert any(handed_on.search(text) for text in sources), (
        f"{app}'s {name} is listed as a front door, but nothing in {app} hands a request to "
        f"common.cli {name} - a front door that does the job itself is a duplicate.")


def test_two_jobs_sharing_a_word_still_share_it():
    stale = sorted(name for name in DIFFERENT_JOBS if name not in _named_in_two_clis())
    assert not stale, f"no longer in two CLIs - take them off DIFFERENT_JOBS: {stale}"


def test_every_listed_duplicate_still_exists():
    """A pair resolved without its entry going would leave the page counting debt that is gone."""
    missing = {job: sorted(member for member in members
                           if member.split(":", 1)[1] not in COMMANDS.get(member.split(":", 1)[0], set()))
               for job, (members, _why) in DUPLICATES.items()}
    missing = {job: members for job, members in missing.items() if members}
    assert not missing, f"these no longer exist - the duplicate is resolved, delete its entry: {missing}"


def test_the_duplicates_only_shrink():
    assert set(DUPLICATES) <= DUPLICATES_AT_0_24_0, (
        f"a new duplicate: {sorted(set(DUPLICATES) - DUPLICATES_AT_0_24_0)}. One job is one command "
        "(rules R43) - the second one calls the first.")
