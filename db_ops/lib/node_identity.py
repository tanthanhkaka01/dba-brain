"""Which node a run belongs to - an identity that survives the host name changing under it.

A ``running`` row is a claim, and the claim names the host and pid that own it (``run_claim``).
The host name was taken to *be* the node, and on a container it is not: a worker whose compose
file pins no ``hostname`` gets a new one on every recreate. The new container then reads its
predecessor's open runs as "another host's", which it cannot check, and leaves them for their
timeout plus an hour of grace - and the open row is the claim, so the task does not run at all in
that time. On 2026-09-30 the upgrade to 0.25.0 held the production engine task's first target that
way until the row was closed by hand (0.26.0 §1.69).

So the tool root carries an identity of its own: a random id written once into ``runtime/``, the
folder a container keeps on the host across a recreate. Two machines never share a tool root, so
two nodes never share the id; a fresh root (every soak) gets a new one. What it lets a reaper say
is the one thing the host name could not: *this row was claimed by this node, under a host name it
no longer has* - so every process of that host is gone, and the row is free at once.

The daemon reads it at start and hands it to every child in :data:`ENV_VAR`, the same way it hands
down ``DB_OPS_NODE_ROLE``. A process started by hand has none, and its claims and reaps behave
exactly as before this module - an absent identity never reaps anything sooner.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

#: In ``runtime/``: generated, never configuration, and never carried in a config bundle - a copy
#: of it on a second machine would make two nodes one.
IDENTITY_FILENAME = "node_identity"

#: How the daemon passes the identity to the processes it starts.
ENV_VAR = "DB_OPS_NODE_IDENTITY"


def identity_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / IDENTITY_FILENAME


def ensure(runtime_dir: str | Path) -> str:
    """This root's identity, written on first use; ``""`` when it can be neither read nor written.

    ``""`` is safe by construction: a claim without an identity is judged by host and pid alone,
    which is the behaviour this module adds to, never replaces.
    """
    path = identity_path(runtime_dir)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    created = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(created + "\n", encoding="utf-8")
    except OSError:
        return ""
    return created


def current() -> str:
    """The identity this process was handed by its daemon, or ``""``."""
    return os.environ.get(ENV_VAR, "").strip()


__all__ = ["ENV_VAR", "IDENTITY_FILENAME", "current", "ensure", "identity_path"]
