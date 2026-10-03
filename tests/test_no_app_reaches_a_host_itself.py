"""An app does not reach a host itself - it asks `common` (R10).

Reaching a host - `ssh`, `scp`, `smbclient`, `sqlcmd`, `robocopy`, `cmdkey`, PowerShell remoting,
`docker` on another machine, `ansible` - is an operation, and operations are `common`'s: one
implementation of each, taking every fact in the request, reachable by any app through `transport`.
An app that spawns the tool itself keeps a second copy of how to reach that host, and the two drift -
the SQL Server restore's `sqlcmd` lived in an app until 0.23.0 (1.38) for exactly that reason.

It had no guard until 0.24.0, only a count by hand. This counts every process an app starts, by
file, because the argv is usually a variable and cannot be read statically. Each file's count is
fixed: either the starts are one of the kinds that are not a host reach (named below, with why), or
they are the debt this rule still carries, which may only shrink. A new process anywhere in an app -
including in a file allowed to start some - changes a count, and the change has to be argued here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


DB_OPS = Path(__file__).resolve().parents[1] / "db_ops"
APPS = frozenset({"backup_restore", "control", "jobs", "metrics", "reports", "sla", "sql_tasks",
                  "sre", "telegram", "webhost"})

#: Libraries that open a session to another machine. An app never imports one; `common` does.
HOST_LIBRARIES = frozenset({"paramiko", "pypsrp", "winrm", "smbprotocol", "smbclient", "fabric"})

#: Process starts that are not a host reach - file: (count, why). Exact, so a host reach added in one
#: of these files still fails.
NOT_A_HOST_REACH: dict[str, tuple[int, str]] = {
    "jobs/daemon.py": (1, "runs the command lines app_commands.json configures - the scheduler's job"),
    "telegram/command_cli.py": (1, "runs the command templates the bot is configured with"),
    "telegram/command_background.py": (1, "starts the bot's own detached runner for a long command"),
    "telegram/detached_exit.py": (1, "the bot's detached runner: runs the configured command it is given"),
    "sre/cli.py": (1, "sre re-launching its own CLI for a step"),
    "sql_tasks/python_source.py": (1, "a SQL task's input: the operator's own Python script"),
    "control/export_public.py": (2, "git over the maintainers' own tree, while exporting it"),
    "control/_support.py": (1, "the deploy tool building the image on this machine (docker build / save)"),
    "sre/service.py": (1, "the operator's VMware PowerShell scripts, run on this machine and streamed "
                          "live - the tool sre drives, not a host it reaches"),
    "sre/data_folder/deploy_sqlserver_ag.py": (1, "the lab orchestrator's one runner: ssh-keygen on this "
                                                  "machine, and the sre CLI re-launched for each step"),
}

#: The debt: app code that reaches a host itself. It may only shrink - moving a start into a
#: `common.cli` command means lowering the count here in the same change.
HOST_REACH_LEFT: dict[str, int] = {
    # Empty since 0.24.0 - rule R10 holds without exception. The last eleven were the SQL Server
    # SMB/Windows restore: its share went to `smb-list` / `smb-get` / `smb-delete` /
    # `smb-credential`, its certificate import on a Windows target to `run-cmd` over WinRM, and
    # its local `sqlcmd` and CHECKDB to `run-sqlcmd` (the operator, 2026-09-26: *an app does not
    # connect to a host directly; every app reaches it one way*).
}

#: The debt as measured on 2026-09-26, when the guard was written - a literal, so a count raised
#: in :data:`HOST_REACH_LEFT` has something to be compared with. Since then `sre` moved its ssh,
#: ansible and scp onto `run-cmd` and `push-file`, and `metrics` its scripts and SQL onto
#: `metric-batch` (0.24.0).
HOST_REACH_LEFT_AT_0_24_0: dict[str, int] = {
    "backup_restore/certificate.py": 1,
    "backup_restore/copy_backup.py": 4,
    "backup_restore/delete_backup.py": 3,
    "backup_restore/preflight.py": 1,
    "backup_restore/restore_database.py": 1,
    "backup_restore/verify_restore.py": 1,
    "metrics/collector.py": 2,
    "sre/service.py": 9,
    "sre/data_folder/deploy_sqlserver_ag.py": 1,
}

_STARTS = {("subprocess", name) for name in ("run", "Popen", "call", "check_call", "check_output")} \
    | {("os", name) for name in ("system", "popen", "spawnl", "spawnv", "execv", "execl")}


def _app_files() -> list[Path]:
    return sorted(p for p in DB_OPS.rglob("*.py") if "__pycache__" not in p.parts
                  and len(p.relative_to(DB_OPS).parts) > 1 and p.relative_to(DB_OPS).parts[0] in APPS)


def _relative(path: Path) -> str:
    return path.relative_to(DB_OPS).as_posix()


def _starts(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and (getattr(node.func.value, "id", ""), node.func.attr) in _STARTS)


@pytest.mark.parametrize("path", _app_files(), ids=_relative)
def test_an_app_starts_no_process_this_file_does_not_account_for(path):
    relative = _relative(path)
    expected = NOT_A_HOST_REACH.get(relative, (0, ""))[0] + HOST_REACH_LEFT.get(relative, 0)
    actual = _starts(path)
    assert actual == expected, (
        f"{relative} starts {actual} process(es), {expected} accounted for. Reaching a host is a "
        "common.cli command, called through transport (rules R10); a start that is not a host reach "
        "is named in NOT_A_HOST_REACH with why. A moved host reach lowers HOST_REACH_LEFT.")


@pytest.mark.parametrize("path", _app_files(), ids=_relative)
def test_an_app_imports_no_library_that_opens_a_session_to_a_host(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.level == 0}
    found = sorted(imported & HOST_LIBRARIES)
    assert not found, f"{_relative(path)} imports {found}; a session to a host is common's"


def test_the_host_reaches_left_only_shrink():
    grown = {name: count for name, count in HOST_REACH_LEFT.items()
             if count > HOST_REACH_LEFT_AT_0_24_0.get(name, 0)}
    assert not grown, f"the debt grew: {grown}"


def test_every_named_file_exists():
    missing = sorted(name for name in [*NOT_A_HOST_REACH, *HOST_REACH_LEFT] if not (DB_OPS / name).exists())
    assert not missing, f"named but not there: {missing}"
