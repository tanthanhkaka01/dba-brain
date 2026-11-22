"""Master-side worker health check (control plane).

SSHes to the worker, confirms the daemon container is up + which dbabrain version it runs,
then invokes the in-container status report (``dbabrain.jobs.status``) which lists every app
command on that node: active?, last run time/status, due now?, last error — plus metric
freshness. Read-only. Host/user/SSH-password come from ``config.json`` + the secret store
(the same ``--key``/``--key-base64`` the other control commands use); nothing is hard-coded.

It also reads the host's container network subnets and checks them against
``data/network_reservations.json`` (see :mod:`dbabrain.lib.network_policy`). That check is here, in
the one command an operator runs to ask "is this worker healthy?", because the failure it catches
does not look like a network problem anywhere else: a Docker bridge that claims a routed range
makes a database vanish from this host alone, and every other symptom is an ordinary connect
timeout. Three outages were diagnosed by hand before it existed.
"""

from __future__ import annotations

import base64
import shlex
import sys
from pathlib import Path

from dbabrain.control import worker_unlock
from dbabrain.control._support import DEFAULT_CONTAINER, DEFAULT_REMOTE_DIR, ssh_capture, ssh_connect
from dbabrain.lib import network_policy
from dbabrain.lib.json_io import load_json_file
from dbabrain.lib.paths import DEFAULT_DATA_DIR

#: One line per network: name, then the subnets its IPAM declares. `host` and `none` allocate
#: nothing and come back with an empty second field, which parse_host_networks drops.
_DOCKER_SUBNETS_CMD = (
    "for n in $(docker network ls --format '{{.Name}}'); do "
    "printf '%s\\t%s\\n' \"$n\" "
    "\"$(docker network inspect \"$n\" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null)\"; "
    "done"
)

# There used to be a fallback here for images predating ``dbabrain.jobs.status``: an inline Python
# snippet that opened '/app/tools/dbabrain/runtime/dbabrain.sqlite' over SSH and printed its own version
# of the report. It is gone, deliberately.
#
# It hard-coded the store's location and assumed the store was SQLite, so on a node whose
# ``data/store_config.json`` declares PostgreSQL it would have read a stale file - or an absent one -
# and reported "no data" for a perfectly healthy worker. It was also a second implementation of a
# report that ``dbabrain.jobs.status`` already produces, and the two had already drifted (it grouped
# metric freshness by ip only, while jobs.status groups by ip + server_id).
#
# An image that old cannot answer the question correctly by any route, so the honest response is to
# say so and let the operator deploy, rather than to print a second, quietly different answer.
_MISSING_STATUS_MODULE = (
    "The deployed image predates `db.cli node-status`, so the worker cannot report its own status.\n"
    "Deploy the current build, then re-run:\n"
    "    python -m dbabrain.control.cli deploy --key-base64 <K>"
)


def run_worker_status(*, host: str, user: str, password: str | None, port: int = 22,
                      container: str = DEFAULT_CONTAINER, remote_dir: str = DEFAULT_REMOTE_DIR,
                      as_json: bool = False, no_metrics: bool = False,
                      key_base64: str | None = None, key: str | None = None) -> int:
    client = ssh_connect(host, user, password, port)
    try:
        rc, out, _ = ssh_capture(
            client, f"docker ps -a --filter {shlex.quote('name=' + container)} "
                    f"--format '{{{{.Names}}}} | {{{{.Status}}}}'")
        container_line = out.strip()
        print(f"# worker {user}@{host}")
        print(f"container: {container_line or '(not found)'}")
        if not container_line:
            print("Daemon container is not present — nothing is running on the worker.", file=sys.stderr)
            return 1

        rc, ver, _ = ssh_capture(
            client, f"docker exec {shlex.quote(container)} python -c 'import dbabrain;print(dbabrain.__version__)' 2>&1")
        print(f"version:   {ver.strip()}")

        # Said before anything that needs the store: a locked daemon (1.409) runs nothing, and
        # cannot say so on Telegram - the token is in the store it cannot open.
        lock = worker_unlock.read_state(client, container)
        print(f"lock:      {worker_unlock.describe(lock)}")
        if lock.get("state") in {"locked", "stopped"}:
            print("           -> python -m dbabrain.control.cli unlock-worker --key-base64 <key>")

        flags: list[str] = []
        if as_json:
            flags.append("--json")
        if no_metrics:
            flags.append("--no-metrics")
        # A PostgreSQL store needs its password decrypted, so the in-container status call needs the
        # passphrase. It used to go as `--key_base64 <value>` on `docker exec`'s argv - the host's
        # process table, which every login reads - and the container's configuration no longer holds
        # it (1.409): it goes on stdin, into that one command's environment.
        secret_key = (base64.b64decode(key_base64).decode("utf-8") if key_base64 else key) or ""
        # The shared db layer's report (1.0.0) - not the jobs app's CLI, which control may not start (R42).
        command = ["python", "-m", "dbabrain.db.cli", "node-status", *flags]
        print()
        if secret_key:
            result = client.run_script(worker_unlock.keyed_exec_script(container, secret_key, command,
                                                                       header="dbabrain: worker-status"))
            rc, out, err = result.exit_code, result.stdout or "", result.stderr or ""
        else:
            rc, out, err = ssh_capture(client, f"docker exec {shlex.quote(container)} "
                                               + " ".join(shlex.quote(token) for token in command))
        if rc != 0 and ("No module named" in (err or "") or "invalid choice: 'node-status'" in (err or "")):
            print(_MISSING_STATUS_MODULE, file=sys.stderr)
            return 1
        print(out.rstrip() or f"(no status output, rc={rc})")
        if err.strip():
            print(err.strip(), file=sys.stderr)

        # Worker host disk usage (real filesystems only).
        _, disk, _ = ssh_capture(client, "df -hT -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null || df -h")
        print("\n===== worker disk usage =====")
        print(disk.rstrip())

        # dbabrain reports live on the host under <remote_dir>/runtime/reports (bind-mounted into
        # the container at /app/tools/dbabrain/runtime/reports), so they can be copied off directly.
        reports = f"{remote_dir}/runtime/reports"
        _, listing, _ = ssh_capture(client, f"ls -lht {reports} 2>/dev/null | head -25")
        print(f"\n===== dbabrain reports — host path: {reports} =====")
        print(listing.rstrip() or "(empty or not found)")
        print(f"\ncopy a report out, e.g.:  scp {user}@{host}:{reports}/<file> .")

        _, networks, _ = ssh_capture(client, _DOCKER_SUBNETS_CMD)
        print("\n===== container network reservations =====")
        for line in report_network_reservations(networks):
            print(line)

        _, hygiene, _ = ssh_capture(client, secret_hygiene_script(remote_dir, container))
        print("\n===== secret hygiene =====")
        for line in report_secret_hygiene(hygiene):
            print(line)
        return rc
    finally:
        client.close()


#: What has to stay closed on the worker once 0.42.0's plan is in (audits/20261120_plan_worker_secret_exposure.md,
#: S6): each check prints `name|ok|detail` or `name|BAD|detail`, names and modes only - never a value.
#: The passphrase is looked for by its NAME in the container's configuration (F1/F2), the keys by mode
#: (F4) and by whether they open with no passphrase (1.410), what deploys leave behind by its folders.
_HYGIENE_CHECKS = r"""
r={root}; c={container}; me=$(id -un)
names=$(docker inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' "$c" 2>/dev/null | cut -d= -f1)
if printf '%s\n' "$names" | grep -qx 'DBABRAIN_SECRET_KEY'; then echo "passphrase in docker inspect|BAD|DBABRAIN_SECRET_KEY is in the container's configuration (on disk in config.v2.json)"
else echo "passphrase in docker inspect|ok|not there"; fi
k="$r/data/ssh_keys"
if [ -r "$k" ] && [ -x "$k" ]; then
  wide=$(find "$k" -type f -name '*.key' -perm /077 | wc -l)
  [ "$wide" = 0 ] && echo "key files wider than 600|ok|none" || echo "key files wider than 600|BAD|$wide"
  open=0; unread=0
  for f in "$k"/*.key; do [ -e "$f" ] || continue
    if [ ! -r "$f" ]; then unread=$((unread+1)); continue; fi
    ssh-keygen -y -P '' -f "$f" >/dev/null 2>&1 && open=$((open+1)); done
  if [ "$unread" != 0 ]; then echo "keys that open with no passphrase|BAD|$unread key(s) not readable by $me - not checked"
  elif [ "$open" = 0 ]; then echo "keys that open with no passphrase|ok|none"
  else echo "keys that open with no passphrase|BAD|$open (seal-ssh-key)"; fi
else echo "key files wider than 600|BAD|$k not readable by $me - not checked"; fi
mode=$(stat -c %a "$r/data" 2>/dev/null)
case "$mode" in "") echo "data/ readable by any login|BAD|cannot stat $r/data";;
  *[1-7]) echo "data/ readable by any login|BAD|$mode";; *) echo "data/ readable by any login|ok|$mode";; esac
left=$(ls -d "$r"/runtime/pre_deploy_* "$r"/runtime/pre_soak_* 2>/dev/null | wc -l)
[ "$left" = 0 ] && echo "pre-deploy copies of data/|ok|none" || echo "pre-deploy copies of data/|BAD|$left (worker-prune)"
imgs=$(docker images --format '{{{{.Repository}}}}' 2>/dev/null | grep -cx dbabrain)
[ "$imgs" -le 1 ] && echo "dbabrain images|ok|$imgs" || echo "dbabrain images|BAD|$imgs (worker-prune)"
pw=$(cat /etc/ssh/sshd_config.d/*.conf /etc/ssh/sshd_config 2>/dev/null | grep -iE '^[[:space:]]*PasswordAuthentication[[:space:]]' | head -1 | awk '{{print tolower($2)}}')
exc=$(cat /etc/ssh/sshd_config.d/*.conf /etc/ssh/sshd_config 2>/dev/null | awk 'tolower($1)=="match" {{m=$0; next}} m!="" && tolower($1)=="passwordauthentication" && tolower($2)=="yes" {{print m; exit}}')
if [ "$pw" = no ] && [ -z "$exc" ]; then echo "sshd password logins|ok|off"
elif [ "$pw" = no ]; then echo "sshd password logins|BAD|off, except:$(printf ' %s' $exc)"
else echo "sshd password logins|BAD|${{pw:-yes (the default)}}"; fi
"""


def secret_hygiene_script(remote_dir: str, container: str) -> str:
    return _HYGIENE_CHECKS.format(root=shlex.quote(remote_dir), container=shlex.quote(container)).strip()


def report_secret_hygiene(listing: str) -> list[str]:
    """The checks' lines, aligned, with a closing verdict; a check that printed nothing is not passed."""
    rows = [line.split("|", 2) for line in listing.splitlines() if line.count("|") >= 2]
    if not rows:
        return ["(the checks printed nothing - the host could not be read)"]
    width = max(len(name) for name, _, _ in rows)
    lines = [f"{'OK ' if state == 'ok' else 'BAD'}  {name:<{width}}  {detail}" for name, state, detail in rows]
    bad = sum(1 for _, state, _ in rows if state != "ok")
    lines.append(f"{bad} of {len(rows)} need attention" if bad else f"all {len(rows)} closed")
    return lines


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
    lines.append("Pin the host's allocation: dbabrain/sre/host_config/README.md")
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
