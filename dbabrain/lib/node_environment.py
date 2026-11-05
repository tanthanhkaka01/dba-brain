"""How this node runs - its operating system, whether it is inside a container, how dbabrain is
installed and which shell a command line is written for (0.35.0, 1.267).

``self-status`` has always answered the first two for a person. The AI assistant needs all four for
a different reason: it proposes command lines, and a line written for the wrong shell is a line that
fails. Single-quoted JSON breaks in Windows PowerShell; ``export`` is bash; inside a container the
command is ``docker exec`` from the host; a source checkout has no ``dbabrain`` on PATH. The
operator, 2026-10-31: *say how and where each command runs, and for which environment - Windows,
Docker, Ubuntu*.

Values and rules only, so it lives in ``lib``: ``common.self_status`` and the assistant both import
it, and neither keeps a copy.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

__all__ = ["describe", "install_form", "operating_system", "runtime", "shell", "summary_line"]


def runtime() -> str:
    """Docker, Kubernetes, or straight on the operating system."""
    if Path("/.dockerenv").exists():
        return "docker"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return "host"
    for marker, label in (("docker", "docker"), ("containerd", "containerd"),
                          ("kubepods", "kubernetes"), ("lxc", "lxc")):
        if marker in cgroup:
            return label
    return "host"


def operating_system() -> str:
    """The distribution a person would name, not the kernel version.

    ``platform.platform()`` answers "Linux-6.8.0-generic" for both Ubuntu and RHEL, which does not
    help anybody deciding whether a package name applies.
    """
    if os.name == "nt":
        release, version, _sp, _kind = platform.win32_ver()
        return f"Windows {release} ({version})".strip()
    try:
        release = platform.freedesktop_os_release()
        pretty = release.get("PRETTY_NAME") or release.get("NAME")
        if pretty:
            return f"{pretty} ({platform.release()})"
    except (AttributeError, OSError):
        pass
    return f"{platform.system()} {platform.release()}"


def install_form() -> str:
    """``pip`` when the package is installed (a ``dbabrain`` command exists), ``source`` when the
    tree is run from a path (``python -m dbabrain...`` with that Python)."""
    from importlib import metadata

    try:
        metadata.distribution("dbabrain")
    except Exception:  # noqa: BLE001 - not installed: the tree is run from a path.
        return "source"
    return "pip"


def shell() -> str:
    """The shell a command line for this node is written in.

    Windows: ``powershell-5.1`` unless PowerShell 7 (``pwsh``) is on PATH - 5.1 has no ``&&`` and
    breaks single-quoted JSON, so the difference changes the line. Elsewhere: ``bash``.
    """
    if os.name == "nt":
        return "powershell-7" if shutil.which("pwsh") else "powershell-5.1"
    return "bash"


def describe(tool_root: str | Path | None = None) -> dict[str, Any]:
    """The four facts, plus where the tool root is and which Python runs dbabrain."""
    return {"os": operating_system(), "runtime": runtime(), "install": install_form(), "shell": shell(),
            "tool_root": str(Path(tool_root).resolve()) if tool_root else "", "python": sys.executable}


def summary_line(facts: dict[str, Any]) -> str:
    """One line for a prompt: what the assistant writes command lines for on this node."""
    if facts.get("runtime") not in (None, "", "host"):
        how = (f"inside a {facts['runtime']} container (tool root {facts.get('tool_root') or '?'}): from the "
               "host run `docker exec <container> dbabrain <app> ...`; inside it `dbabrain <app> ...`")
    elif facts.get("install") == "pip":
        how = "pip-installed: `dbabrain <app> ...` from the tool root"
    else:
        how = (f"a source tree: `{facts.get('python') or 'python'} -m dbabrain.<app>.cli ...` from the tool "
               "root - the venv's Python, never the one on PATH")
    return (f"OS {facts.get('os') or '?'}; shell {facts.get('shell') or '?'}; tool root "
            f"{facts.get('tool_root') or '?'}; {how}.")
