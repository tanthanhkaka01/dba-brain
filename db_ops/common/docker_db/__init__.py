"""Lab database Docker instances: build one, or move one, on a host this is handed a login for.

The work behind ``common.cli create-db-docker`` and ``move-db-docker`` (0.23.0). It lived in the
``sre`` app until then, and read the secret store and wrote ``data/docker_db_connections.json``
itself, so only that app could run it. Now it takes a JSON request - the resolved password, the
resolved SSH logins, the spec - and answers JSON; ``sre`` does the looking up and the
registering. What an instance *is* (the spec, the engines) is :mod:`db_ops.lib.docker_db_spec`,
because both sides need it.
"""

from __future__ import annotations

import sys


def progress(text: str) -> None:
    """A line for the person watching - on stderr, because stdout is the JSON answer."""
    print(text, file=sys.stderr, flush=True)
