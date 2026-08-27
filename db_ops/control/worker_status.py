"""Master-side worker health check (control plane).

SSHes to the worker, confirms the daemon container is up + which db_ops version it runs,
then invokes the in-container status report (``db_ops.jobs.status``) which lists every app
command on that node: active?, last run time/status, due now?, last error — plus metric
freshness. Read-only. Host/user/SSH-password come from ``config.json`` + the secret store
(the same ``--key``/``--key-base64`` the other control commands use); nothing is hard-coded.

It also reads the host's container network subnets and checks them against
``data/network_reservations.json`` (see :mod:`db_ops.lib.network_policy`). That check is here, in
the one command an operator runs to ask "is this worker healthy?", because the failure it catches
does not look like a network problem anywhere else: a Docker bridge that claims a routed range
makes a database vanish from this host alone, and every other symptom is an ordinary connect
timeout. Three outages were diagnosed by hand before it existed.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

from db_ops.control._support import DEFAULT_CONTAINER, DEFAULT_REMOTE_DIR, ssh_capture, ssh_connect
from db_ops.lib import network_policy
from db_ops.lib.json_io import load_json_file
from db_ops.lib.paths import DEFAULT_DATA_DIR

#: One line per network: name, then the subnets its IPAM declares. `host` and `none` allocate
#: nothing and come back with an empty second field, which parse_host_networks drops.
_DOCKER_SUBNETS_CMD = (
    "for n in $(docker network ls --format '{{.Name}}'); do "
    "printf '%s\\t%s\\n' \"$n\" "
    "\"$(docker network inspect \"$n\" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null)\"; "
    "done"
)

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

        _, networks, _ = ssh_capture(client, _DOCKER_SUBNETS_CMD)
        print("\n===== container network reservations =====")
        for line in report_network_reservations(networks):
            print(line)
        return rc
    finally:
        client.close()


def report_network_reservations(listing: str, *, data_dir: Path | None = None) -> list[str]:
    """What this host's container networks mean for the addresses it has to reach.

    Split out from the SSH above so it can be tested against a captured listing: the whole value of
    this check is the sentence it prints, and a check nobody can assert on is how the last one went
    three years without being noticed missing.
    """
    root = data_dir if data_dir is not None else DEFAULT_DATA_DIR
    reservations = network_policy.load_reservations(_read_json(root / "network_reservations.json"))
    if not reservations.routed_ranges and not reservations.container_ranges:
        return ["(data/network_reservations.json declares no ranges — nothing to check against)"]

    inventory = _read_json(root / "db_instances.json").get("db_instances") or ()
    monitored = network_policy.parse_monitored_addresses(inventory)
    host_networks = network_policy.parse_host_networks(_parse_subnet_listing(listing))
    if not host_networks:
        return ["(no container networks reported)"]

    findings = network_policy.evaluate_host(
        host_networks=host_networks, monitored=monitored, reservations=reservations)
    if not findings:
        return [f"OK — {len(host_networks)} container network(s), none overlapping "
                f"{len(monitored)} monitored address(es)."]
    lines = [finding.summary() for finding in findings]
    lines.append("Pin the host's allocation: db_ops/sre/host_config/README.md")
    return lines


def _parse_subnet_listing(listing: str) -> list[tuple[str, str]]:
    """``name<TAB>subnet [subnet ...]`` into one pair per subnet.

    A network may declare several (IPv4 and IPv6, or a split pool), and each one allocates
    independently, so each is checked on its own rather than only the first.
    """
    pairs: list[tuple[str, str]] = []
    for line in (listing or "").splitlines():
        name, _, subnets = line.partition("\t")
        for subnet in subnets.split():
            pairs.append((name.strip(), subnet))
    return pairs


def _read_json(path: Path) -> dict:
    """The file, or an empty mapping when it is absent or unreadable.

    worker-status is a health report. A missing or broken config file here must cost the network
    section, not the run-status section the operator actually came for.
    """
    try:
        return load_json_file(path)
    except (OSError, ValueError, RuntimeError):
        return {}
