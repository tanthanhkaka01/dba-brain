"""Provision (or dry-run) a lab database Docker instance, here or on a host over SSH.

``provision`` is the work behind ``common.cli create-db-docker``. It takes a spec and the
**resolved** password, builds the side-effect-free plan, performs the runtime guards (port in use,
image tag, folder exists), then either returns the plan (a dry run) or writes the files, brings
the stack up with ``docker compose``, waits for health, and returns what it made.

**It reads no configuration and writes none** (0.23.0). It used to decrypt the password from the
secret store and write the connection into ``data/docker_db_connections.json`` itself, which made
it an app's code that only an app could run. Both are ``sre``'s now: it resolves the password and
the SSH login, hands them over in the request, and registers the connection record this returns.

Progress goes to **stderr**: stdout is the JSON answer, and a line of progress there is a
response nobody can parse.
"""

from __future__ import annotations

from db_ops.lib import errors
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from db_ops.common.docker_db import compose as compose_mod
from db_ops.common.docker_db import healthcheck, templates
from db_ops.common.docker_db import progress as _say
from db_ops.common.docker_db.compose import MASKED_PASSWORD, ProvisionPlan
from db_ops.lib.docker_db_spec import DEFAULT_CONTAINERS_DIR, DockerDbSpec


class ProvisionError(errors.OperationFailed):
    """A provisioning guard failed (port in use, folder exists, missing password)."""


class LocalFs:
    """Default filesystem face: the instance dir lives on this machine.

    ``provision`` only touches files through this small interface so a remote
    provisioning run (``remote`` in the request) can substitute an SFTP-backed twin
    (:class:`db_ops.common.docker_db.remote_host.RemoteUbuntuHost`) without the provisioner
    changing.
    """

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def mkdirs(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def write_text(self, path: str, content: str, *, mode: int | None = None) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        if mode is not None:
            try:
                os.chmod(target, mode)
            except OSError:
                pass

    def rmtree(self, path: str) -> None:
        shutil.rmtree(path, ignore_errors=True)


def validate_password(spec: DockerDbSpec, password: str | None) -> None:
    """Reject a password the engine's first-start scripts would corrupt rather than reject.

    Oracle is the case this exists for: the image sets the password through SQL*Plus, which
    expands ``&name`` as a substitution variable. A password containing ``&`` is replaced by
    whatever text follows in the script, so the database comes up **healthy** with a password
    nobody knows — every later login fails ORA-01017, and on an ha-lab the Data Guard step
    dies on ``connect target`` after the whole database has already been copied.

    The failure is silent by nature, so it has to be caught here, before the password reaches
    the ``.env`` file and before a single container is created.
    """
    forbidden = spec.meta.forbidden_password_chars
    if not password or not forbidden:
        return
    present = sorted({char for char in forbidden if char in password})
    if not present:
        return
    shown = " ".join(repr(char) for char in present)
    raise ProvisionError(
        f"The password contains {shown}, which {spec.engine} cannot carry through its own "
        f"first-start scripts: the value is silently altered there, so the database would "
        f"come up healthy with a password nobody knows and every login would fail "
        f"(ORA-01017 for oracle). Choose a password without {' '.join(repr(c) for c in forbidden)} "
        f"— letters, digits and punctuation such as _ - . # ! % are safe."
    )


# --------------------------------------------------------------------------- #
# Runtime guards.
# --------------------------------------------------------------------------- #
def _published_host_ports(runner) -> set[int]:
    """Host ports currently published by any docker container."""
    try:
        result = runner(["docker", "ps", "--format", "{{.Ports}}"],
                        capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return set()
    ports: set[int] = set()
    for match in re.finditer(r":(\d+)->", result.stdout or ""):
        ports.add(int(match.group(1)))
    return ports


def check_ports_free(ports: list[int], runner) -> None:
    in_use = _published_host_ports(runner)
    clashing = sorted(p for p in ports if p in in_use)
    if clashing:
        raise ProvisionError(
            f"Host port(s) already in use by another container: {', '.join(map(str, clashing))}."
        )


def _instance_ports(spec: DockerDbSpec) -> list[int]:
    count = len(spec.node_names())
    return [spec.host_port + i for i in range(count)]


# What a wrong --version costs without this check: `docker compose up` pulls, fails on the
# first node, interrupts the others, and reports it as a wall of compose output — minutes later,
# with the instance folder already written. The registry is asked instead, up front.
_TAG_HINTS: dict[str, str] = {
    # SQL Server has no bare-year tag: `2025` does not exist, `2025-latest` does.
    "mssql": "2022-latest, 2025-latest, or a full tag such as 2025-CU6-ubuntu-24.04",
    "postgres": "18, 17, 16 (or 18-alpine)",
    "mysql": "8.4, 8.0",
    # gvenzl/oracle-free: Oracle AI Database 26ai kept the 23.26.x version numbers, so the
    # 26ai tags are 23.26.2 / latest (there is NO '26' or '26ai' tag).
    "oracle": "23.26.2 or latest (= 26ai; version numbers stay 23.26.x), 23, 23-slim",
    # gvenzl/oracle-xe. `11` is 11.2.0.2 — the only 11g R2 there is as an image, and x86-64 only.
    # 18/21 are the later XE releases and serve XEPDB1 rather than XE, so they are not
    # interchangeable with 11 in a connection string.
    "oracle-xe": "11 (= 11.2.0.2 R2), 11-slim, 18, 21, 21-slim",
}


#: What docker prints when the tag really is not in the registry. Anything else - a rate limit
#: (`toomanyrequests`), a timeout, a network or auth error - is the registry not answering, not a
#: wrong --version.
_TAG_MISSING_MARKERS = ("manifest unknown", "no such manifest", "not found")


def images_in(plan: ProvisionPlan) -> list[str]:
    """Every image the plan's compose file will pull, in order - ha-lab templates run more than
    the engine's own image (MySQL's replication image, the Data Guard lab's `docker:cli` shipper)."""
    images: list[str] = []
    for pf in plan.files:
        if pf.relpath != "docker-compose.yml":
            continue
        for line in pf.content.splitlines():
            text = line.strip()
            if text.startswith("image:"):
                image = text[len("image:"):].strip().strip("'\"")
                if image and image not in images:
                    images.append(image)
    return images


def check_image_exists(spec: DockerDbSpec, runner, *, images: list[str] | None = None) -> None:
    """Fail before anything is created when an image the build needs is not in the registry.

    Every image the compose file names, not only the engine's own: the MySQL ha-lab's image had
    left Docker Hub, and this check - asking only about `mysql:8.4` - passed, so `compose up` failed
    minutes later instead (the create-db-docker pass, 2026-09-25).
    """
    own = f"{spec.meta.image_repo}:{spec.version}"
    for image in images or [own]:
        _check_one_image(spec, image, runner, own=image == own)


def _check_one_image(spec: DockerDbSpec, image: str, runner, *, own: bool) -> None:
    """Fail before anything is created when ``--version`` is not a real image tag.

    *Not found* only when docker says so. Every other failure of ``docker manifest inspect`` was
    reported as a missing tag too, with docker's own words thrown away: on 2026-09-24 a transient
    registry error on a lab host read *"Image not found: postgres:18 … Valid postgres tags include:
    18, 17, 16"* - refusing 18 and recommending it in the next line - and the retry a minute later
    worked. That is reported as what it is, with docker's message, so nobody goes hunting for a typo.
    """
    try:
        result = runner(["docker", "manifest", "inspect", image],
                        capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return  # no docker CLI here; `compose up` will report it
    returncode = getattr(result, "returncode", 1)
    if returncode == 0:
        return
    said = " ".join(str(getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").split())
    if not any(marker in said.lower() for marker in _TAG_MISSING_MARKERS):
        raise ProvisionError(
            f"Could not ask the registry whether {image} exists "
            f"({said[:300] or f'docker manifest inspect exited {returncode}'}). This is not a wrong "
            "--version: the registry did not answer - retry, or check the host's network, its DNS and "
            "Docker Hub's pull limits (toomanyrequests).")
    if not own:
        raise ProvisionError(
            f"Image not found: {image}, which the {spec.mode} {spec.engine} template runs beside the "
            "engine's own image. This is not a --version problem: that image is no longer in the "
            "registry, and the template has to name another.")
    lines = [
        f"Image not found: {image}. --version must be a tag that exists in the registry.",
        f"Valid {spec.engine} tags include: {_TAG_HINTS.get(spec.engine, 'see the image registry')}.",
    ]
    if spec.engine == "mssql":
        lines.append("Full list: https://mcr.microsoft.com/v2/mssql/server/tags/list")
    raise ProvisionError("\n".join(lines))


# --------------------------------------------------------------------------- #
# Printing.
# --------------------------------------------------------------------------- #
def plan_text(plan: ProvisionPlan) -> str:
    """What a dry run shows: every file it would write (the password masked), and the command."""
    lines = [f"# instance directory: {plan.instance_dir}"]
    for pf in plan.files:
        lines.append(f"\n# ---- {plan.instance_dir}/{pf.relpath} ----")
        lines.append(pf.content[:-1] if pf.content.endswith("\n") else pf.content)
    lines.append(f"\n# data directories: {', '.join(plan.data_dirs)}")
    lines.append(f"# command (run in {plan.instance_dir}):")
    lines.append("  " + " ".join(plan.up_command))
    return "\n".join(lines)


def format_summary(plan: ProvisionPlan, statuses: dict[str, str] | None, *, status_label: str) -> str:
    spec = plan.spec
    conn = plan.connection
    lines = [
        f"Instance: {spec.name}",
        f"Engine: {spec.engine}",
        f"Version: {spec.version}",
        f"Mode: {spec.mode}",
        f"Host: {conn['host']}",
        f"Port: {conn['port']}",
        f"Username: {conn['username']}",
        f"Password source: {conn['password_source']}",
        f"Status: {status_label}",
        f"Compose path: {plan.compose_path}",
    ]
    if spec.is_ha and statuses:
        lines.append("Containers:")
        for svc in plan.services:
            lines.append(f"  - {svc}: {statuses.get(svc, 'unknown')}")
    lines.append("Connection:")
    lines.append(f"  {conn['connect']}")
    folder = templates.backup_folder(spec)
    if folder:
        # What a backup entry needs to be told, said where the operator is already looking.
        lines.append(f"Backup folder: {folder}")
        lines.append("  use it as the backup entry's backup_dir and a restore's source_backup_host_dir"
                     + (" - WAL is archived into its wal/" if spec.engine == "postgres" else "")
                     + (" - ARCHIVELOG on first start" if spec.engine in ("oracle", "oracle-xe") else ""))
    if spec.is_ha:
        lines.append("")
        lines.append("WARNING: ha-lab mode is only HA simulation because all containers run on the "
                     "same worker.")
        lines.append("Worker failure will stop the whole cluster.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Provision.
# --------------------------------------------------------------------------- #
def provision(
    spec: DockerDbSpec,
    *,
    password: str | None,
    containers_dir: str = DEFAULT_CONTAINERS_DIR,
    worker_host: str = "",
    dry_run: bool = False,
    force: bool = False,
    health_timeout: int | None = None,   # None = the engine's own ceiling
    runner=subprocess.run,
    fs=None,
) -> dict:
    """Provision ``spec`` and return what was made - or, on a dry run, what would be.

    ``password`` is the resolved value; a dry run may pass ``None`` and sees it masked.
    ``runner``/``fs`` default to this machine; pass a
    :class:`db_ops.common.docker_db.remote_host.RemoteUbuntuHost` (as both) to provision the
    instance on a remote Ubuntu host over SSH instead.
    """
    fs = fs or LocalFs()
    instance_dir = f"{containers_dir.rstrip('/')}/{spec.name}"

    # Rule 7: folder must not already exist unless --force.
    if not dry_run and fs.exists(instance_dir) and not force:
        raise ProvisionError(
            f"Instance '{spec.name}' already exists ({instance_dir}). "
            f"To rebuild it from scratch — which DESTROYS its containers and their data volumes — "
            f"pass --force (from Telegram: answer 'yes' to the recreate question). "
            f"To keep it, choose another --name."
        )
    if not dry_run and not password:
        raise ProvisionError(
            f"No password for {spec.name}: the request must carry the resolved value of "
            f"'{spec.password_env}'.")

    # Before anything is created, and before the password is written anywhere: a character
    # the engine's own init script cannot carry produces a database with an unknown password
    # rather than an error, and the cost is only discovered ~30 minutes later at the first
    # real login. Same fail-fast principle as the image-tag check below.
    validate_password(spec, password)

    plan = compose_mod.build_plan(
        spec, containers_dir=containers_dir, password=password,
        worker_host=worker_host, dry_run=dry_run,
    )
    result = {
        "name": spec.name, "engine": spec.engine, "version": spec.version, "mode": spec.mode,
        "instance_dir": plan.instance_dir, "compose_path": plan.compose_path,
        "backup_folder": templates.backup_folder(spec) or None, "dry_run": bool(dry_run),
    }

    if dry_run:
        return {**result, "status": "dry-run (not created)", "healthy": None, "statuses": {},
                "plan_text": plan_text(plan),
                "summary": format_summary(plan, None, status_label="dry-run (not created)")}

    # --force on an existing instance = clean recreate: tear down the old project
    # AND its named volumes first. Without this, reusing the name with a different
    # engine version leaves stale data in the volumes and the containers fail to
    # start ("database files are incompatible with server"). It also frees the old
    # published ports before the port check below.
    if force and fs.exists(f"{instance_dir}/docker-compose.yml"):
        _say(f"--force: tearing down existing '{spec.name}' (removes its containers + volumes) ...")
        runner(["docker", "compose", "down", "-v", "--remove-orphans"],
               cwd=instance_dir, check=False)

    # Rule 6: host port(s) must be free.
    check_ports_free(_instance_ports(spec), runner)

    # ... and the image tag must exist. Checked before anything is written: otherwise a typo
    # ("mssql:2025" — Microsoft publishes 2025-latest, not 2025) is only discovered minutes
    # later, mid-pull, with the instance folder already on disk and a wall of compose output
    # as the error.
    check_image_exists(spec, runner, images=images_in(plan))

    created_now = not fs.exists(instance_dir)
    # The bind mount has to exist before `compose up`: Docker would otherwise create it itself,
    # as root, and the restore workflow copies its .bak files in over SSH as an ordinary user.
    # The failure that produces is a permission error on a directory that plainly exists.
    backup_mount = spec.resolved_backup_mount
    if backup_mount:
        fs.mkdirs(backup_mount)
    _write_plan(plan, fs)

    # From here on the instance exists on disk. If bringing it up fails, undo what this run
    # created — otherwise the retry (with the tag fixed) is refused with "instance folder
    # already exists, pass --force", which is a confusing thing to be told after a failure that
    # created nothing that works. An instance that already existed is never removed.
    def _rollback(reason: str) -> None:
        if not created_now:
            return
        _say(f"\nRolling back '{spec.name}' ({reason}) ...")
        runner(["docker", "compose", "down", "-v", "--remove-orphans"],
               cwd=instance_dir, check=False)
        fs.rmtree(instance_dir)
        _say(f"Removed {instance_dir}. Fix the problem and run the command again.")

    _say(f"Starting {spec.mode} {spec.engine} instance '{spec.name}' ...")
    up = runner(plan.up_command, cwd=plan.instance_dir, check=False)
    if getattr(up, "returncode", 1) != 0:
        _rollback("docker compose up failed")
        raise ProvisionError(f"`docker compose up` failed for {spec.name} (exit {up.returncode}).")

    # An explicit --health-timeout wins; otherwise the engine's own ceiling, because "how long
    # may a first start take" is an engine fact (Oracle creates a database, postgres opens a
    # port) and not something the caller should have to know.
    wait_seconds = int(health_timeout) if health_timeout else spec.meta.health_timeout
    _say(f"Waiting for health (up to {wait_seconds}s) ...")
    statuses = healthcheck.wait_healthy(plan.services, spec.engine, timeout=wait_seconds, runner=runner)
    healthy = all(s == "healthy" for s in statuses.values())
    status_label = "running" if healthy else "started (health check timed out)"

    # Some stacks are not finished when their containers are healthy: a SQL Server availability
    # group has to be built across the running nodes, in order, once each one accepts
    # connections (compose cannot express it and the image has no init hook).
    for command in templates.post_start_commands(spec):
        if not healthy:
            # The containers' own last words, so the failure explains itself wherever it is
            # read — a Telegram message, a log line — instead of only naming a status.
            detail = healthcheck.failure_detail(statuses, runner=runner)
            raise ProvisionError(
                f"Not running {' '.join(command)}: the nodes never became healthy "
                f"({statuses}). Fix the containers first, then re-run with --force."
                + (f"\n{detail}" if detail else "")
            )
        _say(f"\nRunning {' '.join(command)} ...")
        # A remote post-start step can run for minutes (the Oracle Data Guard RMAN duplicate),
        # which is fragile over one synchronous SSH channel. When the fs is a remote host it
        # exposes run_detached: launch under setsid+nohup and poll over short connections, so a
        # blip in the control connection does not SIGHUP the step. Local runs stay synchronous.
        if hasattr(fs, "run_detached"):
            completed = fs.run_detached(command, cwd=plan.instance_dir,
                                        timeout=spec.meta.post_start_timeout)
        else:
            completed = runner(command, cwd=plan.instance_dir, check=False)
        if getattr(completed, "returncode", 1) != 0:
            raise ProvisionError(
                f"Post-start step failed for {spec.name}: {' '.join(command)} "
                f"(exit {getattr(completed, 'returncode', 'unknown')}). The containers are up; "
                f"the script can be re-run from {plan.instance_dir}."
            )

    # The stack is up even when health lagged behind the timeout, so the command succeeds; the
    # status says whether health was confirmed.
    return {**result, "status": status_label, "healthy": healthy, "statuses": statuses,
            "summary": format_summary(plan, statuses, status_label=status_label)}


def _write_plan(plan: ProvisionPlan, fs) -> None:
    base = plan.instance_dir.rstrip("/")
    fs.mkdirs(base)
    for sub in plan.data_dirs:
        fs.mkdirs(f"{base}/{sub}")
    for pf in plan.files:
        # 0600 keeps the password file owner-only; init scripts must be executable in-container.
        mode = 0o600 if pf.secret else (0o755 if pf.relpath.endswith(".sh") else None)
        fs.write_text(f"{base}/{pf.relpath}", pf.content, mode=mode)
    _say(f"Wrote instance files to {plan.instance_dir}")


def masked_env_preview(spec: DockerDbSpec) -> str:
    """The .env content as it appears in dry-run (password masked). For tests."""
    return compose_mod._env_file_content(spec, MASKED_PASSWORD)
