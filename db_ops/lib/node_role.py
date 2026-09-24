"""Whether a record declaring a ``node_role`` runs on a node with a given role.

Three readers asked this question with three copies of the answer: the daemon (which app commands
run here), the Telegram command processor (which support commands this node answers) and, from
0.22.0, ``self-status`` (which app commands this node would run). The copies had already drifted in
the one place that matters — what an EMPTY role means — so the rule lives here once and each caller
states its own default rather than re-deriving the rest.
"""

from __future__ import annotations

#: Spellings meaning "every node". ``both`` and ``any`` are legacy and still in configs.
EVERY_NODE = frozenset({"all", "both", "any"})

#: What a node with no declared role is. A bare install is a single node doing everything, and the
#: daemon has always treated an unset ``DB_OPS_NODE_ROLE`` as the master.
DEFAULT_NODE_ROLE = "master"


def runs_on(command_role: str | None, node_role: str | None, *, default: str = "all") -> bool:
    """True when a record with ``command_role`` runs on a node whose role is ``node_role``.

    ``default`` is what an empty ``command_role`` means, and it differs per caller on purpose: an
    app command with no role runs everywhere (``all``), a Telegram support command with none stays
    on the worker, where ``APP-TELEGRAM`` has always run it.
    """
    role = str(command_role or "").strip().lower() or str(default).strip().lower()
    if role in EVERY_NODE:
        return True
    return role == (str(node_role or "").strip().lower() or DEFAULT_NODE_ROLE)
