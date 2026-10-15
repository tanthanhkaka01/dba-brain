"""Where a node keeps its lab databases, when its configuration says so.

A lab database lives in two folders on its host: one holds each instance's compose file and
volumes, the other is the backup folder bind-mounted into the container at the same path. Until
0.29.0 both were constants (``/opt/<the earlier name>/containers`` and ``.../backup``), on the
reasoning that they are the tool's own layout and not an operator's choice.

They are a node's facts all the same. Nothing reads the ``compose_path`` a lab's record carries:
``move-db-docker`` and ``create-db-docker --force`` find an existing lab by putting the folder and
the lab's name together, so the folder is where that node's labs are - not something the code can
know. Since 0.29.0 the node states both in ``sre_config.json`` (``sre.docker_db``) and the code
holds neither (the operator, 2026-10-12: *a field, so the code hard-codes nothing and falls back
to nothing*). ``upgrade-config`` writes the two earlier constants into a node that states none
(``lib.earlier_defaults``), and the shipped ``sre_config.json`` states them for a new one.

This module is the one reader of that statement. It is for the apps: a ``common.cli`` command
reads no configuration (rules R09), so the app resolves the folder - a flag, else the node's own -
and puts it in the request; a request that states none is refused by name.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from dbabrain.lib.json_io import load_json_file

#: The file, and the block of it, a node states its lab folders in.
FILE_NAME = "sre_config.json"
BLOCK = "docker_db"


class LabFolders(NamedTuple):
    """What the node states; an empty string where it states nothing."""

    containers_dir: str = ""
    backup_mount: str = ""


def read(document: Any) -> LabFolders:
    """The lab folders a parsed ``sre_config.json`` states: ``sre.docker_db``."""
    settings = document.get("sre") if isinstance(document, dict) else None
    block = settings.get(BLOCK) if isinstance(settings, dict) else None
    if not isinstance(block, dict):
        return LabFolders()
    return LabFolders(containers_dir=str(block.get("containers_dir") or "").strip(),
                      backup_mount=str(block.get("backup_mount") or "").strip())


def load(data_dir: str | Path) -> LabFolders:
    """The lab folders the node under ``data_dir`` states. A node without the file states none;
    a file that cannot be read raises, because falling back to the default there would send the
    next move to a folder the node's labs are not in."""
    path = Path(data_dir) / FILE_NAME
    if not path.is_file():
        return LabFolders()
    return read(load_json_file(path))
