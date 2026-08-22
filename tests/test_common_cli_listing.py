"""Every command the CLI dispatches must appear in the listing it prints with no arguments.

`run-cmd` shipped working but unlisted: the dispatch knew it, `--help` did not, and the only way
to discover it was to read the source. A command nobody can find is one nobody uses, which is the
same outcome as not having written it.

`data/telegram_support_commands.md` already has this test for the bot's commands — its docstring
notes the listing drifted twice before anyone noticed. This is the same guard for the shared CLI,
which has no separate doc to drift *from*: the usage text IS the documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

from db_ops.common import cli

SOURCE = Path(cli.__file__).read_text(encoding="utf-8")

#: Handled by `config_admin.main`, which the dispatcher falls through to, and listed by that
#: parser's own --help rather than by this one.
DELEGATED = {"add-sql", "metric-toggle"}


def _dispatched() -> set[str]:
    """Command names the dispatcher compares argv[0] against.

    Leading-dash tokens are dropped: `-h` / `--help` are compared against argv[0] too, but they
    are flags every command accepts rather than commands in their own right.
    """
    names = set(re.findall(r'argv\[0\] == "([a-z0-9-]+)"', SOURCE))
    for group in re.findall(r"argv\[0\] in \{([^}]*)\}", SOURCE):
        names.update(re.findall(r'"([a-z0-9-]+)"', group))
    return {name for name in names if not name.startswith("-")}


def _listed() -> set[str]:
    return set(re.findall(r"^\s{2}([a-z0-9-]+)\s{2,}\S", cli.USAGE, flags=re.MULTILINE))


def test_every_dispatched_command_is_listed_in_the_usage_text():
    missing = _dispatched() - _listed() - DELEGATED
    assert not missing, (
        f"dispatched but not listed by `python -m db_ops.common.cli`: {sorted(missing)}. "
        "A command that only the source mentions is one nobody can find."
    )


def test_the_listing_does_not_advertise_a_command_that_is_not_dispatched():
    """The other direction: a renamed or removed command left in the listing sends an operator
    to a command that answers with the usage text and exit 2."""
    phantom = _listed() - _dispatched() - DELEGATED
    assert not phantom, f"listed but not dispatched: {sorted(phantom)}"


def test_the_commands_added_for_file_and_command_work_are_all_reachable():
    """Named explicitly so a refactor that drops one fails here rather than in production."""
    dispatched = _dispatched()
    for command in ("run-cmd", "run-sql", "fetch-file", "send-file", "pack-files"):
        assert command in dispatched, command
        assert command in _listed(), command
