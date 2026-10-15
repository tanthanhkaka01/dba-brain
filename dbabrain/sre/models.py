from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SreCommandResult:
    command: list[str]
    cwd: Path
    returncode: int
    stdout: str = ""
    stderr: str = ""
    dry_run: bool = False
