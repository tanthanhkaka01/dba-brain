"""One executor runs everything on a host: `common/remote_exec.py` (rules R44).

Until 0.25.0 `common` held five: `remote_exec`, `hostcmd` (its own paramiko client and a stdin-fed
`bash -s`), `sqlcmd_run` (a local `Popen`, its own SSH channel, a local PowerShell `Invoke-Command`),
`backup_copy` / `cli_backup_copy` (paramiko clients of their own) and `ssh_relay` (reaching through a
session to its client). Each had its own idea of timeouts, errors and how a script travels - and a
fix to one, the script that a `docker compose exec` inside it swallowed (1.65), would have been a fix
to one of five. The operator, 2026-09-28: *one way to run anything on a host; no module runs its own.*

So this holds, over the whole tree, that the SSH primitives appear in two files only: `common/ssh.py`
opens the connection and `common/remote_exec.py` - the executor behind `common.cli run-cmd` - uses it.
Everything else asks a `remote_exec` session: `run`, `run_script`, `open_stream`, `sftp`.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "db_ops"

#: The executor, and the one module it opens its connection through.
EXECUTOR = "db_ops/common/remote_exec.py"
CONNECTOR = "db_ops/common/ssh.py"

#: Calls that run something on a host, or open the way to it, by their attribute name.
PRIMITIVES = frozenset({"exec_command", "get_transport", "open_sftp", "invoke_shell"})


def _modules():
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path.relative_to(REPO).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _offences(relative: str, tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in PRIMITIVES and relative != EXECUTOR:
                found.append(f"{relative}:{node.lineno} calls .{name}()")
            if name == "SSHClient" and relative != CONNECTOR:
                found.append(f"{relative}:{node.lineno} builds a paramiko SSHClient")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            if (module == "paramiko" or "paramiko" in names or module.startswith("paramiko.")) \
                    and relative != CONNECTOR:
                found.append(f"{relative}:{node.lineno} imports paramiko")
            if "open_ssh_client" in names and relative != EXECUTOR:
                found.append(f"{relative}:{node.lineno} imports open_ssh_client")
    return found


def test_nothing_but_remote_exec_runs_anything_on_a_host():
    offences = [line for relative, tree in _modules() for line in _offences(relative, tree)]
    assert not offences, (
        "Only common/remote_exec.py runs anything on a host (R44). Ask a remote_exec session - "
        "run, run_script, open_stream, sftp - instead:\n  " + "\n  ".join(offences))


def test_the_guard_sees_what_it_is_for():
    """A scan that matches nothing passes every tree. It must catch each shape it names."""
    sample = ast.parse(
        "import paramiko\n"
        "from db_ops.common.ssh import open_ssh_client\n"
        "client.exec_command('ls')\n"
        "client.get_transport().open_session()\n"
        "client.open_sftp()\n"
        "paramiko.SSHClient()\n")

    offences = _offences("db_ops/common/elsewhere.py", sample)

    assert len(offences) == 6, offences


def test_the_executor_is_where_the_primitives_are():
    """If `remote_exec` stopped using them, the guard would be guarding a file that does not."""
    tree = ast.parse((REPO / EXECUTOR).read_text(encoding="utf-8"))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert {"exec_command", "open_sftp", "get_transport"} <= called
