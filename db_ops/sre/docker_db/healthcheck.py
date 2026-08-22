"""Health probes for provisioned lab database containers.

The compose files declare container-level healthchecks; the fastest, most
reliable readiness signal is therefore Docker's own health status. We poll
``docker inspect`` for it and fall back to an engine-native probe (pg_isready /
mysqladmin ping / sqlcmd) for images that declare no HEALTHCHECK.

**A container that is not running is not "still starting".** Both probes fail the
same way for a container that crashed, exited on a bad password, or was never
created — so treating a failed probe as "starting" made a dead stack look slow:
the wait burned its whole budget and then reported ``timeout``, sending the
operator to look for a performance problem that was really a container that had
exited seconds in. The state is therefore checked before the health status, and a
dead container ends the wait immediately with its own last log lines attached.
"""

from __future__ import annotations

import subprocess
import time

from db_ops.sre.docker_db.models import ENGINE_META


# Statuses that mean "this will never become healthy without intervention".
DEAD_STATES = ("exited", "dead", "removing", "paused")
MISSING = "missing"


def container_state_command(service: str) -> list[str]:
    """`docker inspect` command returning a container's lifecycle state
    (``running`` / ``created`` / ``exited`` / ``dead`` / ``paused``)."""
    return ["docker", "inspect", "--format", "{{.State.Status}}", service]


def container_logs_command(service: str, *, tail: int = 20) -> list[str]:
    """The container's last log lines — what actually says why it died."""
    return ["docker", "logs", "--tail", str(tail), service]


def container_health_command(service: str) -> list[str]:
    """`docker inspect` command returning a container's health status string
    (``healthy`` / ``starting`` / ``unhealthy``), or ``none`` when the image
    declares no healthcheck."""
    fmt = "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
    return ["docker", "inspect", "--format", fmt, service]


def native_probe_command(engine: str, service: str) -> list[str]:
    """A `docker exec` probe that returns 0 when the engine accepts connections.
    Used for images without a declared HEALTHCHECK (e.g. plain ``postgres``)."""
    meta = ENGINE_META[engine]
    if engine == "postgres":
        inner = ["pg_isready", "-U", meta.username, "-d", meta.database]
    elif engine == "mysql":
        # -p is intentionally omitted; ping succeeds for a running server even
        # with access-denied, which is enough of a readiness signal.
        inner = ["mysqladmin", "ping", "-h", "127.0.0.1", "--silent"]
    elif engine in ("oracle", "oracle-xe"):
        # Both gvenzl images — oracle-free and oracle-xe — ship healthcheck.sh (also declared as
        # the image HEALTHCHECK), so one probe covers 11g XE and Free 23ai alike.
        inner = ["healthcheck.sh"]
    else:  # mssql
        inner = ["/opt/mssql-tools18/bin/sqlcmd", "-C", "-S", "localhost",
                 "-U", meta.username, "-Q", "SELECT 1"]
    return ["docker", "exec", service, *inner]


def wait_healthy(
    services: list[str],
    engine: str,
    *,
    timeout: int = 180,
    interval: int = 5,
    runner=subprocess.run,
) -> dict[str, str]:
    """Poll each service until healthy or ``timeout`` elapses.

    Returns a ``{service: status}`` map where status is ``healthy``,
    ``unhealthy``, ``timeout`` or ``error``. The primary (first) service is the
    one most worth waiting on; all are reported.
    """
    deadline = time.monotonic() + timeout
    statuses: dict[str, str] = {svc: "starting" for svc in services}
    while True:
        for svc in services:
            if statuses[svc] == "healthy":
                continue
            statuses[svc] = _probe_once(svc, engine, runner=runner)
        if all(s == "healthy" for s in statuses.values()):
            return statuses
        # A dead container will not recover on its own; waiting out the budget only delays
        # the same answer by up to half an hour and mislabels it "timeout".
        if any(s in DEAD_STATES or s == MISSING for s in statuses.values()):
            return statuses
        if time.monotonic() >= deadline:
            return {svc: (s if s == "healthy" else "timeout") for svc, s in statuses.items()}
        time.sleep(interval)


def failure_detail(statuses: dict[str, str], *, tail: int = 20, runner=subprocess.run) -> str:
    """Why the stack is not healthy, in the words of the containers themselves.

    Reads the last log lines of every node that is not healthy, so the reason travels with
    the error (into a Telegram message, for instance) instead of requiring an SSH session to
    the host to find out what "timeout" actually meant.
    """
    parts: list[str] = []
    for svc, status in statuses.items():
        if status == "healthy":
            continue
        parts.append(f"--- {svc} ({status}) ---")
        if status == MISSING:
            parts.append("container does not exist (compose up did not create it, or it was removed)")
            continue
        try:
            logs = runner(container_logs_command(svc, tail=tail), capture_output=True, text=True, check=False)
        except OSError as exc:  # noqa: PERF203 - diagnostics must never raise over the real error
            parts.append(f"could not read logs: {exc}")
            continue
        text = ((logs.stdout or "") + (logs.stderr or "")).strip()
        parts.append(text or "(no output)")
    return "\n".join(parts)


def _probe_once(service: str, engine: str, *, runner) -> str:
    # State first: "not running" and "not ready yet" are different answers, and both probes
    # below fail identically for a container that is gone.
    state_result = runner(container_state_command(service), capture_output=True, text=True, check=False)
    if getattr(state_result, "returncode", 1) != 0:
        return MISSING
    state = (state_result.stdout or "").strip().lower()
    if state in DEAD_STATES:
        return state
    if not state:
        return MISSING

    inspect = runner(container_health_command(service), capture_output=True, text=True, check=False)
    status = (inspect.stdout or "").strip().lower()
    if status == "healthy":
        return "healthy"
    if status in ("starting", "unhealthy"):
        return status
    # No declared healthcheck — try a native probe. The container is running, so a failing
    # probe here genuinely does mean "not ready yet".
    probe = runner(native_probe_command(engine, service), capture_output=True, text=True, check=False)
    return "healthy" if probe.returncode == 0 else "starting"
