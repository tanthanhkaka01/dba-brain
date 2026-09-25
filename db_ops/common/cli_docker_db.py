"""``create-db-docker`` / ``move-db-docker`` — build a lab database Docker instance, or move one.

Plumbing only: :mod:`db_ops.common.docker_db` does the work, :mod:`db_ops.lib.response` shapes
the answer. Both commands lived in the ``sre`` app until 0.23.0, where the provisioner decrypted
its own password, the mover resolved both hosts out of ``db_instances.json`` and launched
``relay-file`` as a subprocess, and both wrote ``data/docker_db_connections.json``. None of that
is here: the request carries the resolved password and the resolved SSH logins, and the answer
carries what ``sre`` needs to register the result. ``sre.cli`` (and so ``/spbot_create_db_docker``)
is the caller that looks things up.

**stdin only.** Both requests carry secrets - a database password, one or two SSH passwords - so
they arrive on ``-``, like ``secret-set``: inline they would be on the command line for every
process on the machine to read, and ``@file`` is a plaintext file on disk.

**Progress goes to stderr, and so does anything a child process prints.** stdout is the JSON
answer. ``docker compose up`` run locally writes to file descriptor 1 directly, which a Python
``redirect_stdout`` does not reach, so for the length of the work fd 1 *is* stderr.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Any

from db_ops.lib import response

USAGE = """\
Usage: python -m db_ops.common.cli create-db-docker -
       python -m db_ops.common.cli move-db-docker -

The request arrives on stdin (-): it carries passwords. Reads no config - the passwords and the
SSH logins are RESOLVED values; the caller looks them up and registers the answer.

create-db-docker - build one lab database instance, here or on an Ubuntu host over SSH:

  {"name": "lab01", "engine": "postgres",   // postgres | mysql | mssql | oracle | oracle-xe
   "version": "18", "mode": "single",       // single | ha-lab
   "replicas": 2,                           // ha-lab only; omit for the engine's default
   "host_port": 5432,                       // omit for the engine's own port
   "password_ref": "LAB01_PASSWORD",        // the secret REF the connection record names
   "password": "...",                       // its RESOLVED value; a dry run may omit it
   "backup_mount": "/opt/db_ops/backup",    // omit for the engine default, "" for none
   "network_subnet": "",                    // omit for the /24 derived from the name
   "containers_dir": "/opt/db_ops/containers",
   "worker_host": "192.0.2.10",             // the address recorded when built locally
   "remote": {"host": "192.0.2.249", "port": 22, "username": "labuser",
              "password": "...", "key_file": ""},   // omit to build on this machine
   "install_docker": false, "sudo_password": "...",  // remote only; sudo defaults to the SSH one
   "health_timeout": 0, "force": false, "dry_run": false}

  data: {"name", "engine", "version", "mode", "replicas", "host_port", "password_ref",
         "worker_host", "instance_dir", "compose_path", "backup_folder", "status", "healthy",
         "statuses", "docker", "dry_run", "plan_text" (dry run), "summary"}

move-db-docker - move an existing instance, data included, from one host to another:

  {"name": "ora11g_lab", "engine": "oracle-xe",
   "source":      {"label": "LAB-A", "host": "192.0.2.249", "port": 22, "username": "u",
                   "password": "...", "key_file": ""},
   "destination": {"label": "LAB-B", "host": "192.0.2.250", ...},
   "containers_dir": "/opt/db_ops/containers", "dest_containers_dir": "",
   "stage_dir": "/tmp/db_ops_move", "include_volumes": true, "commit_container": false,
   "stop_source": false, "keep_stage": false, "force": false, "health_timeout": 0,
   "dry_run": false}

  data: {"instance", "engine", "source_host", "destination_host", "instance_dir",
         "compose_path", "statuses", "bytes_transferred", "artifacts", "source_stopped",
         "dry_run", "plan_text" (dry run), "summary"}

A dry run connects to nothing for create-db-docker; move-db-docker reads the source to say what
it would move, and changes nothing anywhere.
"""

OPERATIONS = ("create-db-docker", "move-db-docker")


@contextlib.contextmanager
def _stdout_is_stderr():
    """Everything written to stdout while the work runs lands on stderr instead.

    Both levels: ``sys.stdout`` for Python, file descriptor 1 for a child process such as a local
    ``docker compose up``. Restored before the answer is printed.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved, 1)
        os.close(saved)


def _flag(request: dict[str, Any], name: str, default: bool = False) -> bool:
    value = request.get(name, default)
    return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes"}


def _create(request: dict[str, Any]) -> dict[str, Any]:
    from db_ops.common.docker_db.provisioner import provision
    from db_ops.common.docker_db.remote_host import RemoteUbuntuHost, ensure_docker
    from db_ops.lib.docker_db_spec import DEFAULT_CONTAINERS_DIR, ENGINE_META, DockerDbSpec

    name = str(request.get("name") or "").strip()
    engine = str(request.get("engine") or "").strip()
    meta = ENGINE_META.get(engine)
    replicas = request.get("replicas")
    replicas_explicit = replicas not in (None, "")
    backup_mount = request.get("backup_mount")
    spec = DockerDbSpec(
        name=name,
        engine=engine,
        version=str(request.get("version") or "").strip(),
        mode=str(request.get("mode") or "single").strip(),
        # Oracle ha-lab is Data Guard 1/1, so its implicit default is one standby.
        replicas=int(replicas) if replicas_explicit else (1 if engine == "oracle" else 2),
        host_port=int(request.get("host_port") or 0) or (meta.container_port if meta else 0),
        password_env=str(request.get("password_ref") or f"{name.upper()}_PASSWORD").strip(),
        backup_mount=None if backup_mount is None else str(backup_mount).strip(),
        network_subnet=str(request.get("network_subnet") or "").strip(),
    )
    spec.validate(replicas_explicit=replicas_explicit)

    dry_run = _flag(request, "dry_run")
    containers_dir = str(request.get("containers_dir") or DEFAULT_CONTAINERS_DIR)
    remote = request.get("remote")
    if remote is not None and not isinstance(remote, dict):
        raise ValueError('"remote" must be an object: host, port, username, password or key_file.')
    # Built remotely, the connection must point at the machine the database runs on.
    worker_host = str((remote or {}).get("host") or request.get("worker_host") or "").strip()

    host = None
    docker = None
    kwargs: dict[str, Any] = {}
    try:
        # A dry run renders the plan and connects to nothing - and never installs Docker, which
        # the sre command used to do even with --dry-run.
        if remote and not dry_run:
            host = RemoteUbuntuHost.from_login(remote)
            kwargs = {"runner": host.run, "fs": host}
            if _flag(request, "install_docker"):
                docker = ensure_docker(
                    host, containers_dir=containers_dir, backup_mount=spec.resolved_backup_mount,
                    sudo_password=str(request.get("sudo_password") or remote.get("password") or "") or None)
                print(f"Docker ready on {docker['host']} "
                      f"({'already present' if docker['already_present'] else 'installed'}).",
                      file=sys.stderr, flush=True)
        result = provision(
            spec, password=str(request.get("password") or "") or None,
            containers_dir=containers_dir, worker_host=worker_host, dry_run=dry_run,
            force=_flag(request, "force"),
            health_timeout=int(request.get("health_timeout") or 0) or None, **kwargs)
    finally:
        if host is not None:
            host.close()
    return {**result, "replicas": spec.replicas if spec.is_ha else None,
            "host_port": spec.host_port, "password_ref": spec.password_env,
            "worker_host": worker_host, "docker": docker}


def _move(request: dict[str, Any]) -> dict[str, Any]:
    from db_ops.common.docker_db import mover
    from db_ops.common.docker_db.remote_host import RemoteUbuntuHost
    from db_ops.lib.docker_db_spec import DEFAULT_CONTAINERS_DIR, DEFAULT_STAGE_DIR

    source_login, dest_login = request.get("source"), request.get("destination")
    if not isinstance(source_login, dict) or not isinstance(dest_login, dict):
        raise ValueError('move-db-docker needs "source" and "destination" SSH logins (objects).')
    spec = mover.MoveSpec(
        name=str(request.get("name") or "").strip(),
        source_target=str(source_login.get("label") or source_login.get("host") or ""),
        dest_target=str(dest_login.get("label") or dest_login.get("host") or ""),
        containers_dir=str(request.get("containers_dir") or DEFAULT_CONTAINERS_DIR),
        dest_containers_dir=str(request.get("dest_containers_dir") or ""),
        stage_dir=str(request.get("stage_dir") or DEFAULT_STAGE_DIR),
        include_volumes=_flag(request, "include_volumes", True),
        commit_container=_flag(request, "commit_container"),
        stop_source=_flag(request, "stop_source"),
        keep_stage=_flag(request, "keep_stage"),
        force=_flag(request, "force"),
        engine=str(request.get("engine") or "").strip(),
        health_timeout=int(request.get("health_timeout") or 0),
    )
    if not spec.name:
        raise ValueError("move-db-docker needs the instance's name.")
    # Before either host is reached: without an engine there is no health probe to finish with.
    mover.resolve_engine(spec)
    source = destination = None
    try:
        source = RemoteUbuntuHost.from_login(source_login)
        destination = RemoteUbuntuHost.from_login(dest_login)
        return mover.move(spec, source=source, destination=destination,
                          dry_run=_flag(request, "dry_run"))
    finally:
        for host in (source, destination):
            if host is not None:
                with contextlib.suppress(Exception):
                    host.close()


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    if argv and argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    if not argv or argv[0] != "-":
        print(USAGE, file=sys.stderr)
        return response.emit(response.fail(
            operation, "the request must arrive on stdin (-): it carries a database password and "
                       "SSH passwords, which inline are visible on the command line and in a file "
                       "are plaintext on disk"))
    request, code = read_request("-", USAGE)
    if request is None:
        return code

    from db_ops.common.docker_db.mover import MoveError
    from db_ops.common.docker_db.provisioner import ProvisionError
    from db_ops.common.docker_db.remote_host import RemoteHostError
    from db_ops.common.remote_exec import RemoteExecError
    from db_ops.lib.ssh_errors import SshError

    started = time.monotonic()
    try:
        with _stdout_is_stderr():
            data = _create(request) if operation == "create-db-docker" else _move(request)
    except (ValueError, ProvisionError, MoveError, RemoteHostError, RemoteExecError, SshError) as exc:
        return response.emit(response.fail(operation, str(exc)))
    except Exception as exc:  # noqa: BLE001 - the caller parses an answer; a traceback is none
        # A wrong SSH password used to end here as a RemoteAuthError traceback, and the caller
        # read "exited 1 without a JSON response" instead of the reason (1.37, on the labs).
        return response.emit(response.fail(operation, f"{type(exc).__name__}: {exc}"))
    name = data.get("name") or (data.get("instance") or {}).get("name") or request.get("name")
    message = (f"{name}: {data.get('status') or ('moved' if operation == 'move-db-docker' else 'done')}"
               if not data.get("dry_run") else f"{name}: dry run, nothing changed")
    return response.emit(response.ok(
        operation, message=message, data=data,
        metrics={"duration_ms": int((time.monotonic() - started) * 1000)}))
