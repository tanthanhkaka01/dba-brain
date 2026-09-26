"""Cross-platform shell helpers shared by db_ops apps.

The backup/restore and metrics apps drive Windows targets through PowerShell.
Historically they hard-coded ``powershell.exe``, which only exists on a Windows
host. To let the same code run from a Linux/Ubuntu host (e.g. inside a
container), resolve the PowerShell executable at runtime and prefer the
cross-platform PowerShell 7 binary (``pwsh``) when present.
"""

from __future__ import annotations

import os
import shutil

__all__ = [
    "POWERSHELL_NOT_FOUND_HINT",
    "SHELL_BASH",
    "SHELL_CMD",
    "SHELL_POWERSHELL",
    "docker_cli",
    "is_powershell_executable",
    "powershell_executable",
]

#: The shells a remote script is written for. `common.remote_exec` runs a script in one and
#: `metrics` declares which one a collector's script needs; one spelling for both sides, because a
#: shell the two spell differently is a script sent to the wrong interpreter.
SHELL_BASH = "bash"
SHELL_POWERSHELL = "powershell"
SHELL_CMD = "cmd"

# Preference order: cross-platform PowerShell 7 first so a Linux host with
# pwsh installed can still reach Windows targets via WinRM/Invoke-Command,
# then Windows PowerShell names for native Windows hosts.
_POWERSHELL_CANDIDATES = ("pwsh", "powershell.exe", "powershell")

# Set DB_OPS_POWERSHELL to force a specific executable (name or absolute path).
POWERSHELL_ENV_VAR = "DB_OPS_POWERSHELL"

POWERSHELL_NOT_FOUND_HINT = (
    "PowerShell was not found on PATH. Install PowerShell 7 ('pwsh') on "
    "Linux/macOS, or run on a Windows host with 'powershell.exe'. You can also "
    f"set {POWERSHELL_ENV_VAR} to an explicit executable path."
)


def powershell_executable() -> str:
    """Return the PowerShell executable available on this host.

    Resolution order:

    1. ``DB_OPS_POWERSHELL`` environment override (explicit name or path).
    2. First of ``pwsh`` / ``powershell.exe`` / ``powershell`` found on PATH.
    3. Platform default name (so the original ``FileNotFoundError`` path still
       fires with a clear message when nothing is installed).
    """
    override = os.environ.get(POWERSHELL_ENV_VAR, "").strip()
    if override:
        return override
    for candidate in _POWERSHELL_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    return "powershell.exe" if os.name == "nt" else "pwsh"


def is_powershell_executable(name: str) -> bool:
    """True when ``name`` (a command name or path) refers to PowerShell.

    Used by target-context guards that previously matched the literal prefix
    ``"powershell"``; they must also accept the resolved ``pwsh`` binary.
    """
    base = os.path.basename(str(name)).strip().lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base in {"pwsh", "powershell"}


#: Plain `docker` when this user may run it, `sudo docker` otherwise - decided on the host, at the
#: moment of use, as the backup scripts have always decided it.
_DOCKER_OR_SUDO = '$(docker info >/dev/null 2>&1 && echo docker || echo "sudo docker")'


def docker_cli(sudo: bool) -> str:
    """How to call docker on a remote host where ``sudo`` was asked for.

    ``sudo docker`` outright fails on a host whose sudo wants a password - there is no terminal to
    type it into - although the SSH user is usually in the docker group and needs no sudo at all.
    Every PostgreSQL and Oracle restore onto such a machine failed that way (the lab drill,
    2026-09-24); the cloud hosts, whose sudo asks for nothing, never showed it. So: plain docker
    first, sudo only when docker itself refuses. The result is typed into a POSIX shell.
    """
    return _DOCKER_OR_SUDO if sudo else "docker"
