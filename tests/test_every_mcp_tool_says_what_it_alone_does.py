"""Every MCP tool opens with a sentence of its own.

A tool's description opens with its reference entry's ``one_line``, and an input entry several commands share has
one ``one_line`` for all of them: ``table-info``, ``object-source``, ``db-activity`` and ``query-plan`` all opened with
*One database object or the server's activity read*, the four ``smb-*`` tools with one sentence, ``key-request`` and
``key-renew`` with another. An agent picks a tool by that sentence, so it could not tell those apart; Glama, scoring
the server on 2026-11-26, rated its disambiguation 1 of 5 for exactly that. Such an entry now carries
``command_lines``, one sentence per command, and these tests keep every new shared entry to it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from dbabrain.lib import mcp_tools as M
from dbabrain.lib import shared_objects


@pytest.fixture(scope="module")
def entries():
    # The packaged copy - a public checkout ships no data/shared_config_objects.json.
    return M.command_entries(shared_objects.load())


def _shared(entries):
    by_entry: dict[str, list[str]] = {}
    for command, entry in entries.items():
        by_entry.setdefault(str(entry.get("object")), []).append(command)
    return {name: commands for name, commands in by_entry.items() if len(commands) > 1}


def test_every_command_of_a_shared_entry_has_a_sentence_of_its_own(entries):
    missing = []
    for name, commands in _shared(entries).items():
        lines = entries[commands[0]].get("command_lines") or {}
        missing += [f"{name}: {command}" for command in commands if not str(lines.get(command) or "").strip()]
    assert not missing, ("These commands share an input entry and have no command_lines sentence, so their tools "
                         "open alike:\n" + "\n".join(missing))


def test_a_sentence_belongs_to_a_command_of_its_entry(entries):
    """A line for a command the entry does not serve is a line no tool will ever open with."""
    stray = []
    for entry in {id(e): e for e in entries.values()}.values():
        commands = set(entry.get("commands") or [])
        stray += [f"{entry.get('object')}: {name}" for name in (entry.get("command_lines") or {}) if name not in commands]
    assert not stray, "\n".join(stray)


def test_no_two_tools_open_with_the_same_sentence(entries):
    first = {command: M.describe(command, entry).split("\n", 1)[0]
                      .split(":", 1)[1].strip() for command, entry in entries.items()}
    repeated = [line for line, count in Counter(first.values()).items() if count > 1]
    assert not repeated, "\n".join(f"{line!r}: {sorted(c for c, f in first.items() if f == line)}" for line in repeated)


def test_no_two_offered_tools_open_alike_run_sql_write_included(tmp_path):
    """``run-sql-write`` is a tool and not a command: it opened with ``run-sql``'s sentence, which the check over
    commands could not see. The end-to-end listing found it (2026-11-26)."""
    from dbabrain.mcp import server as S

    root = Path(__file__).resolve().parents[1]
    catalogue = json.loads((root / "dbabrain/common/catalogue/emergency_operations.json").read_text(encoding="utf-8"))
    node = S.Node(data_dir=tmp_path, config_path=None, settings=M.parse_mcp_tools(dict(M.DEFAULTS)),
                  reference=shared_objects.load(), operations=M.operation_commands(catalogue))
    first = Counter(tool["description"].split("\n", 1)[0].split(":", 1)[-1].strip() for tool in S.tool_list(node))
    assert [line for line, count in first.items() if count > 1] == []


def test_a_tool_opens_with_its_own_sentence(entries):
    entry = entries["query-plan"]
    text = M.describe("query-plan", entry)
    assert text.startswith(f"dbabrain common.cli `query-plan`: {entry['command_lines']['query-plan']}")
    assert entry["one_line"] not in text.split("\n", 1)[0]


def _offered(tmp_path):
    from dbabrain.mcp import server as S

    root = Path(__file__).resolve().parents[1]
    catalogue = json.loads((root / "dbabrain/common/catalogue/emergency_operations.json").read_text(encoding="utf-8"))
    node = S.Node(data_dir=tmp_path, config_path=None, settings=M.parse_mcp_tools(dict(M.DEFAULTS)),
                  reference=shared_objects.load(), operations=M.operation_commands(catalogue))
    return S, S.tool_list(node)


def test_what_every_tool_shares_is_said_once_in_the_instructions(tmp_path):
    """The note that a server is named by ``server_id`` with its login completed by the node, and the one that an
    emergency operation takes ``confirm`` and a ``reason``, were two of a tool's three sentences: ``disable-job`` and
    ``start-job`` read 97% alike over their whole description, ``pull-file`` and ``push-file`` 92%. The server's
    instructions say both once; a tool says only what is its own."""
    S, tools = _offered(tmp_path)
    assert "server_id" in S.INSTRUCTIONS and "inventory and encrypted secret store" in S.INSTRUCTIONS
    assert "`confirm`" in S.INSTRUCTIONS and "`reason`" in S.INSTRUCTIONS
    shared = ("encrypted secret store", "emergency-operations list", "Changes or removes something")
    carrying = [tool["name"] for tool in tools if any(text in tool["description"] for text in shared)]
    assert carrying == []


def test_no_two_offered_tools_have_the_same_description(tmp_path):
    """With the shared sentences gone a tool's description is its own sentence, so two alike are two the reference
    describes alike - what Glama read as indistinguishable."""
    _, tools = _offered(tmp_path)
    seen = Counter(tool["description"] for tool in tools)
    assert [text for text, count in seen.items() if count > 1] == []
