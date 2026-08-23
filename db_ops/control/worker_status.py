"""Master-side worker health check (control plane).

SSHes to the worker, confirms the daemon container is up + which db_ops version it runs,
then invokes the in-container status report (``db_ops.jobs.status``) which lists every app
command on that node: active?, last run time/status, due now?, last error — plus metric
freshness. Read-only. Host/user/SSH-password come from ``config.json`` + the secret store
(the same ``--key``/``--key-base64`` the other control commands use); nothing is hard-coded.
"""

from __future__ import annotations

import base64
import sys

from db_ops.control._support import DEFAULT_CONTAINER, DEFAULT_REMOTE_DIR, ssh_capture, ssh_connect

# There used to be a fallback here for images predating ``db_ops.jobs.status``: an inline Python
# snippet that opened '/app/tools/db_ops/runtime/db_ops.sqlite' over SSH and printed its own version
# of the report. It is gone, deliberately.
#
# It hard-coded the store's location and assumed the store was SQLite, so on a node whose
# ``data/store_config.json`` declares PostgreSQL it would have read a stale file - or an absent one -
# and reported "no data" for a perfectly healthy worker. It was also a second implementation of a
# report that ``db_ops.jobs.status`` already produces, and the two had already drifted (it grouped
# metric freshness by ip only, while jobs.status groups by ip + server_id).
#
# An image that old cannot answer the question correctly by any route, so the honest response is to
# say so and let the operator deploy, rather than to print a second, quietly different answer.
_MISSING_STATUS_MODULE = (
    "The deployed image predates db_ops.jobs.status, so the worker cannot report its own status.\n"
    "Deploy the current build, then re-run:\n"
    "    python -m db_ops.control.cli deploy --key-base64 <K>"
)


def run_worker_status(*, host: str, user: str, password: str | None, port: int = 22,
                      container: str = DEFAULT_CONTAINER, remote_dir: str = DEFAULT_REMOTE_DIR,
                      as_json: bool = False, no_metrics: bool = False,
                      key_base64: str | None = None, key: str | None = None) -> int:
    client = ssh_connect(host, user, password, port)
    try:
        rc, out, _ = ssh_capture(
            client, f"docker ps -a --filter name={container} --format '{{{{.Names}}}} | {{{{.Status}}}}'")
        container_line = out.strip()
        print(f"# worker {user}@{host}")
        print(f"container: {container_line or '(not found)'}")
        if not container_line:
            print("Daemon container is not present — nothing is running on the worker.", file=sys.stderr)
            return 1

        rc, ver, _ = ssh_capture(
            client, f"docker exec {container} python -c 'import db_ops;print(db_ops.__version__)' 2>&1")
        print(f"version:   {ver.strip()}")

        flags = ""
        if as_json:
            flags += " --json"
        if no_metrics:
            flags += " --no-metrics"
        # A PostgreSQL store needs its password decrypted, so the in-container status call needs the
        # passphrase. `docker exec` does not inherit the environment the daemon set at runtime, so it
        # has to be passed through here — without it, worker-status works on a SQLite store and fails
        # on a PostgreSQL one. Harmless when the store is SQLite (no credential is looked up).
        if key_base64:
            flags += f" --key_base64 {key_base64}"
        elif key:
            flags += f" --key_base64 {base64.b64encode(key.encode()).decode()}"
        print()
        rc, out, err = ssh_capture(client, f"docker exec {container} python -m db_ops.jobs.status{flags}")
        if rc != 0 and "No module named" in (err or ""):
            print(_MISSING_STATUS_MODULE, file=sys.stderr)
            return 1
        print(out.rstrip() or f"(no status output, rc={rc})")
        if err.strip():
            print(err.strip(), file=sys.stderr)

        # Worker host disk usage (real filesystems only).
        _, disk, _ = ssh_capture(client, "df -hT -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null || df -h")
        print("\n===== worker disk usage =====")
        print(disk.rstrip())

        # db_ops reports live on the host under <remote_dir>/runtime/reports (bind-mounted into
        # the container at /app/tools/db_ops/runtime/reports), so they can be copied off directly.
        reports = f"{remote_dir}/runtime/reports"
        _, listing, _ = ssh_capture(client, f"ls -lht {reports} 2>/dev/null | head -25")
        print(f"\n===== db_ops reports — host path: {reports} =====")
        print(listing.rstrip() or "(empty or not found)")
        print(f"\ncopy a report out, e.g.:  scp {user}@{host}:{reports}/<file> .")
        return rc
    finally:
        client.close()
