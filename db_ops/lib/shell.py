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
    "is_powershell_executable",
    "powershell_executable",
]

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
