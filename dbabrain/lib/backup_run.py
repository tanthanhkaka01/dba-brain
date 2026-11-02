"""The tag a backup run is started and stopped by (0.34.0, 1.238).

Both sides need it and neither may import the other: the backup app hands it to the script as
``DBABRAIN_RUN_TAG`` when the run starts, and ``backup-stop`` (in ``common``) finds the run on its host
by it - the pid file ``<temp>/dbabrain_runs/<tag>.pid`` and the sessions named
``host_name = dbabrain:<tag>``. Its characters are held because it reaches a file name and a SQL
literal on the host.
"""

from __future__ import annotations

import re

#: The env name the backup scripts read.
RUN_TAG_ENV = "DBABRAIN_RUN_TAG"
#: What a tag may hold, and how long it may be (a session's host_name is at most 128 characters).
RUN_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def run_tag(backup_id: str, job_run_id: int) -> str:
    """``<backup_id>_<job_runs id>``: unique per run, and rebuilt from the RUNNING row by a stop."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{backup_id}_{int(job_run_id)}")[-100:]
