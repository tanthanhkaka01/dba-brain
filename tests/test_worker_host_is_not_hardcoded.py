"""The worker's address belongs to the deployment, not to a command definition.

`spbot_create_db_docker` used to carry `--worker-host <address>` as a literal in `command_argv`,
and `spbot_report_inventory` built its result link on the same literal. Two things follow from
that, and only the second one is obvious:

- The shipped catalogue carried *somebody else's* address. A fresh install got a documentation-range
  one, which is nobody's worker, so the command was wrong on arrival rather than wrong later.
- Moving the worker meant editing every command that named it, and a command nobody edited kept
  pointing at the old host until somebody ran it.

`config.json` already declares the cluster, so `{worker_host}` resolves from there: the command says
*what* it wants and the deployment says where it is.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from db_ops.telegram.command_processor import _default_worker_host

ADDRESS = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

CATALOGUE = Path("db_ops/telegram/catalogue/telegram_support_commands.json")
EXAMPLE = Path("data/telegram_support_commands.example.json")


def _commands(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["telegram_support_commands"]


def _shipped_files() -> list[Path]:
    return [path for path in (CATALOGUE, EXAMPLE) if path.is_file()]


def test_no_shipped_command_runs_against_a_hardcoded_address():
    """An address inside `command_argv` is a machine the reader does not have."""
    offenders: list[str] = []
    for path in _shipped_files():
        for command in _commands(path):
            argv = (command.get("action_config") or {}).get("command_argv")
            if not isinstance(argv, list):
                continue
            for word in argv:
                if isinstance(word, str) and ADDRESS.search(word):
                    offenders.append(f"{path.name}:{command['command_text']} -> {word}")
    assert offenders == [], (
        "these shipped commands would run against an address written into the command itself; "
        "use {worker_host} (or a target argument) so the deployment decides:\n  "
        + "\n  ".join(offenders)
    )


def test_no_shipped_command_links_to_a_hardcoded_address():
    """A result link built on a literal address sends every reader to the author's machine."""
    offenders: list[str] = []
    for path in _shipped_files():
        for command in _commands(path):
            config = command.get("action_config") or {}
            for key in ("success_text", "start_text", "failure_text", "reply_text"):
                value = config.get(key)
                if isinstance(value, str) and re.search(r"http://\d{1,3}\.\d", value):
                    offenders.append(f"{path.name}:{command['command_text']}.{key}")
    assert offenders == [], (
        "these shipped commands build a URL on a literal address:\n  " + "\n  ".join(offenders)
    )


def test_the_worker_host_placeholder_is_the_one_that_is_used():
    """The replacement has to actually be `{worker_host}`, not a different spelling.

    A placeholder nothing resolves renders literally and the command runs against the string
    `{worker-host}`, which fails in a way that reads like a network problem.
    """
    used: set[str] = set()
    for path in _shipped_files():
        for command in _commands(path):
            blob = json.dumps(command.get("action_config") or {})
            used.update(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", blob))
    if "worker_host" in used:
        assert "worker-host" not in used


def test_a_missing_worker_returns_empty_rather_than_guessing(tmp_path):
    """No worker declared is a real state - a master-only install - and it must not invent one."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"master": [{"node_id": "m", "host": "localhost"}]}),
                      encoding="utf-8")
    assert _default_worker_host(config) == ""


def test_an_unreadable_config_does_not_take_the_command_down(tmp_path):
    """A command that never mentions the worker still has to run when config.json is broken."""
    config = tmp_path / "config.json"
    config.write_text("{ this is not json", encoding="utf-8")
    assert _default_worker_host(config) == ""


def test_the_declared_worker_is_what_is_returned(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"worker": [{"node_id": "w1", "host": "198.51.100.7"}]}),
        encoding="utf-8",
    )
    assert _default_worker_host(config) == "198.51.100.7"
