"""`sre ssh --stdin` keeps a command that carries a password out of every argument list.

The lab AG tool runs commands on the SQL nodes that carry their password (``SSHPASS=...``). Batch 3
quoted them; they still went to ``python -m db_ops.sre.cli ssh`` as arguments, so the password sat in
the process table here and on the bastion for as long as the command ran (review 0.25.0, B8.1, the
residual). ``sre ssh --stdin`` reads the command from stdin and sends it as a ``run-cmd`` script,
stdin all the way to the node, and the tool uses it.
"""

from __future__ import annotations

import importlib
import io
import logging
import sys
from pathlib import Path

from db_ops.sre import cli as sre_cli
from db_ops.sre.config import SreOperationalConfig
from db_ops.sre.service import run_ssh_command

SECRET = "N0de-Pa55word"
COMMAND = f"SSHPASS='{SECRET}' sshpass -e ssh node1 hostname"


def _config() -> SreOperationalConfig:
    return SreOperationalConfig(
        root_dir=Path("/tmp/sre"),
        inventory={"groups": {"shared": [{"name": "bastion-01", "role": "bastion", "ip": "10.0.0.1"}]}},
        credentials={"guest_user": "tuser"},
        database_defaults={}, vmware={}, automation={})


def test_a_command_from_stdin_is_sent_as_a_script():
    request = run_ssh_command(_config(), host="10.0.0.1", command_args=[], script_text=COMMAND,
                              dry_run=True)

    assert request["script"] == COMMAND
    assert SECRET not in str(request.get("command") or "")


def test_the_cli_reads_the_command_from_stdin(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(sre_cli, "run_ssh_command",
                        lambda cfg, **kw: seen.update(kw) or {"script": kw["script_text"]})
    monkeypatch.setattr(sys, "stdin", io.StringIO(COMMAND))
    args = sre_cli.build_parser().parse_args(["ssh", "--stdin", "--dry-run", "10.0.0.1"])

    sre_cli._handle_ssh(args, logging.getLogger("sre-test"), sre_config=_config())

    assert seen["script_text"] == COMMAND and seen["command_args"] == []


def test_the_lab_ag_tool_hands_its_command_over_stdin(monkeypatch):
    tool = importlib.import_module("db_ops.sre.data_folder.deploy_sqlserver_ag")
    calls: list[tuple[list, str | None]] = []
    monkeypatch.setattr(tool, "_run", lambda cmd, timeout=0, cwd=None, stdin_text=None:
                        calls.append((cmd, stdin_text)) or (0, "", ""))

    tool.remote("10.0.0.1", COMMAND, timeout=5)

    [(argv, stdin_text)] = calls
    # Before the host: anything after it is the remote command, so "--stdin" there would be run.
    assert stdin_text == COMMAND and argv.index("--stdin") < argv.index("10.0.0.1")
    assert not any(SECRET in str(part) for part in argv)
