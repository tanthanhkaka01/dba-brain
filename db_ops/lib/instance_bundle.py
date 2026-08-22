"""What a SQL Server instance-metadata bundle *is* — its layout, its two phases, its order.

The work of exporting and replaying one is an operation and stays in
``db_ops.common.sqlserver_instance``, behind ``common.cli sqlserver-export-instance`` and its
siblings. These are the facts a caller needs **before** any of that runs: where the artifacts sit
inside a bundle, which of the two phases it is talking about, and what order artifacts go in.

``backup_restore`` needs all three while it is still reading config — deciding whether an entry's
``server_metadata`` block is valid, and whether a bundle exists at all — long before there is
anything to connect to. It cannot import ``common`` to ask, and a subprocess cannot answer a
question about a string constant, so the values live here.

**The order is not cosmetic.** Agent jobs replayed before the user databases exist create job
steps against missing databases, which fail on their first schedule, quietly, at 02:00. That is
the whole reason ``artifacts_in_order`` sorts rather than returning the policy's key order.
"""

from __future__ import annotations

from typing import Any


#: The two halves of a replay, named because they are not interchangeable: ``pre-database`` must
#: run before the databases are restored or their users are orphaned, ``post-database`` after,
#: because Agent job steps name databases that have to exist.
PRE_DATABASE = "pre-database"
POST_DATABASE = "post-database"

#: Artifacts live under this subdirectory of the bundle, so a bundle can later gain siblings (the
#: user-database backups it belongs with) without the two mixing.
SERVER_DIR = "server"

#: Written next to the artifacts. Replay reads it to know what it is being handed *before* it
#: executes anything — source build, edition, collation, and every secret reference emitted.
MANIFEST_NAME = "manifest.json"

__all__ = ["MANIFEST_NAME", "POST_DATABASE", "PRE_DATABASE", "SERVER_DIR", "artifacts_in_order"]


def artifacts_in_order(policy: dict[str, Any], *, phase: str = "") -> list[str]:
    """Artifact names in dependency order, optionally limited to one phase.

    Takes the policy document as an argument rather than reading it: the read is
    ``data_sources.load_sqlserver_instance_policy`` and belongs to the one reader of the data
    folder, while ordering a document somebody already has is arithmetic.
    """
    entries = policy.get("artifacts") or {}
    named = [
        (str(name), value)
        for name, value in entries.items()
        if isinstance(value, dict) and "order" in value
    ]
    if phase:
        named = [item for item in named if str(item[1].get("phase") or "") == phase]
    return [name for name, _ in sorted(named, key=lambda item: int(item[1]["order"]))]
