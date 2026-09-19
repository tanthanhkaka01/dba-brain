"""What a scheduled SQL task's **input** may say — the reserved placeholder names.

The mirror of :mod:`db_ops.lib.task_output`, which names what a task does with its result set. This
names the one part of its *input* that neither the task nor the operator chooses: the placeholders
the runner fills from the ``sql_targets`` entry it is running for.

**Why it is here and not in either caller.** Two places need it and neither may import the other:
``common.sql_task_admin`` validates ``input.args`` while registering a command, and
``sql_tasks.python_source`` substitutes the values while running one. ``common`` is the API layer and
does not import an app (ORD 13); an app does not import ``common`` either — it invokes its CLI. So
the constant was spelled out twice, with a comment in ``common`` explaining that it had to be, and
``tests/test_no_duplicate_definitions.py`` found the two copies on 2026-09-19. The layer rule was
right and the conclusion was wrong: a value both layers need is exactly what ``lib`` is for, and
both of them may import it.

The cost of two copies is not theoretical. Adding a third placeholder to the runner without adding
it here would make ``sql-command-add`` refuse an ``input.args`` the runner can fill perfectly well,
and the refusal would name the argument rather than the list it was checked against.
"""

from __future__ import annotations

#: The names ``input.args`` may use **without declaring them as parameters**: the runner adds them to
#: the substitution values from the target it is running for — its ``server_id`` and
#: ``database_name``, as configured. A script that must reach the target itself takes them from here,
#: which is what lets one command run on every tier it has a target for.
TARGET_PLACEHOLDERS: tuple[str, ...] = ("target_server_id", "target_database")

__all__ = ["TARGET_PLACEHOLDERS"]
