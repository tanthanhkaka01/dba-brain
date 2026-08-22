"""Move a lab database Docker instance from one host to another, with its data.

``create-db-docker`` builds an instance from a template; this moves one that already exists —
its compose file, its ``.env``, the image it runs, and the contents of its named volumes — onto
a second machine and starts it there. The lab databases this repo provisions are not disposable
by the time anyone wants to move them: ``ora11g_lab`` carries a restored 11g R2 estate that took
hours to load, and re-provisioning it on the new host would produce an empty database with the
same name, which is the one outcome an operator asking to "move it" never means.

**Why not just re-pull the image on the target.** Because a lab host is not assumed to reach a
registry. The image travels as a ``docker save`` archive for the same reason the volumes do: the
only network path this command relies on is the one it already has, an SSH session to each host
from the machine running the CLI.

**Why root arrives through Docker and not through sudo.** The instance directory is root-owned
(``.env`` is 0600 root, because it holds the database password) and a volume's contents belong to
the engine's own uid — 54321 for the Oracle images. Reading either as the SSH user fails. Rather
than require sudo rights on both hosts, every privileged read and write is done by a throwaway
``docker run --user 0`` container using **the instance's own image**, which is already present on
the source and has just been loaded on the target. Nothing is pulled and no new privilege is
granted: the SSH user is already in the ``docker`` group, which is what lets it manage the
instance at all.

**A named volume is not proof of where the data is.** ``gvenzl/oracle-xe:11`` mounts
``/opt/oracle/oradata`` — and keeps XE 11.2's datafiles at ``/u01/app/oracle/oradata``, which no
volume covers. So ``ora11g_lab``'s declared volume is empty and its 15.2 GB database lives in the
container's writable layer. A move that ships the image and the volumes is, for that instance,
a move that ships nothing: the destination starts the stock image, comes up **healthy**, and
answers as an empty database with the right name and the right password. That is the worst
possible failure shape, so it is checked (:func:`assert_data_travels`) rather than trusted, and
``commit_container`` is the answer to it — ``docker commit`` turns the writable layer into an
image, which travels like any other.

**The stack is stopped for the volume export and started again immediately.** An Oracle datafile
copied while the instance is writing to it restores to a database that opens and is wrong, which
is the failure this whole area of the repo keeps trying to make impossible. The stop is therefore
not optional when volumes are included; what *is* optional (``stop_source``) is whether the
source stays down after the target is proven healthy.

Ordering on the target matters and is not obvious:

1. ``docker load`` — the image must exist before anything else can use it;
2. extract the instance directory — compose needs the file and the ``.env`` beside it;
3. ``docker compose create`` — this is what creates the named volumes **with compose's own
   labels**. Creating them with ``docker volume create`` instead leaves them unlabelled, and
   compose then refuses the stack with "volume already exists but was not created by Docker
   Compose"; that refusal arrives after the data has been restored into them, so the whole
   transfer has to be redone;
4. restore each volume's contents into the volumes step 3 made;
5. ``docker compose up -d`` and wait for health.
"""

from __future__ import annotations

import ipaddress
import json
import posixpath
import re
import shlex
import time
from dataclasses import dataclass, field

from db_ops.sre.docker_db import healthcheck, register_config
from db_ops.sre.docker_db.models import ENGINE_META
from db_ops.sre.docker_db.provisioner import DEFAULT_CONTAINERS_DIR

#: Where the bundle is written on both hosts. Under /tmp on purpose: it is reproducible output
#: that must not survive a reboot, and a half-finished bundle left in the containers dir would
#: be indistinguishable from an instance.
DEFAULT_STAGE_DIR = "/tmp/db_ops_move"

IMAGES_ARCHIVE = "images.tar.gz"
INSTANCE_ARCHIVE = "instance.tar.gz"
MANIFEST = "manifest.json"

#: Mount points used inside the throwaway root containers. Prefixed so they cannot collide with
#: a path the engine image already has (``/data``, ``/backup`` and ``/opt`` all exist in one
#: image or another here).
_SRC_MOUNT = "/dbops_src"
_OUT_MOUNT = "/dbops_out"

#: Seconds ``docker stop`` waits for the engine to shut itself down before SIGKILL. Docker's own
#: default is 10, and every database image here needs more than that: the gvenzl Oracle images
#: trap SIGTERM and run ``shutdown immediate``, which on an 11g XE with a warm buffer cache does
#: not finish in ten seconds. A SIGKILLed Oracle leaves datafiles mid-checkpoint — and the copy
#: taken straight afterwards would carry that state to the new host, where it opens and is wrong.
STOP_TIMEOUT_SECONDS = 180

#: A writable layer bigger than this is data, not logs. Chosen well above what an idle container
#: accumulates (a few MB of logs and pid files) and well below any real database: the check it
#: guards only has to separate "the engine wrote its files here" from "nothing happened here".
WRITABLE_LAYER_DATA_BYTES = 256 * 1024 * 1024


class MoveError(RuntimeError):
    """A user-facing failure: instance not found, port taken, subnet clash, transfer refused."""


@dataclass(frozen=True)
class MoveSpec:
    """What to move, from where to where, and what to do with the source afterwards."""

    name: str
    source_target: str
    dest_target: str
    containers_dir: str = DEFAULT_CONTAINERS_DIR
    dest_containers_dir: str = ""
    stage_dir: str = DEFAULT_STAGE_DIR
    include_volumes: bool = True
    commit_container: bool = False
    stop_source: bool = False
    keep_stage: bool = False
    force: bool = False
    engine: str = ""
    health_timeout: int = 0
    register: bool = True

    @property
    def target_containers_dir(self) -> str:
        return (self.dest_containers_dir or self.containers_dir).rstrip("/")

    @property
    def source_instance_dir(self) -> str:
        return f"{self.containers_dir.rstrip('/')}/{self.name}"

    @property
    def dest_instance_dir(self) -> str:
        return f"{self.target_containers_dir}/{self.name}"

    def stage(self) -> str:
        return f"{self.stage_dir.rstrip('/')}/{self.name}"


@dataclass
class InstanceFacts:
    """What the source host says the instance actually is, read from Docker rather than the
    compose file: the file describes intent, the daemon describes what is running."""

    name: str
    instance_dir: str
    containers: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    subnets: list[str] = field(default_factory=list)
    #: container name -> compose service name. Needed to name the moved image in an override
    #: file: compose keys everything by service, and the container name is a different string.
    services: dict[str, str] = field(default_factory=dict)
    #: container name -> bytes written into its layer since it was created.
    layer_bytes: dict[str, int] = field(default_factory=dict)

    @property
    def helper_image(self) -> str:
        """The image used for the throwaway root containers that read/write the privileged
        paths. The instance's own, so nothing has to be pulled on either host."""
        if not self.images:
            raise MoveError(f"{self.name}: no image found for the instance; nothing to move.")
        return self.images[0]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "instance_dir": self.instance_dir,
            "containers": list(self.containers),
            "images": list(self.images),
            "volumes": list(self.volumes),
            "ports": list(self.ports),
            "subnets": list(self.subnets),
            "services": dict(self.services),
            "layer_bytes": dict(self.layer_bytes),
        }


# --------------------------------------------------------------------------- #
# Reading the source
# --------------------------------------------------------------------------- #
def _out(completed) -> str:
    return (getattr(completed, "stdout", "") or "").strip()


def _checked(host, argv: list[str], *, what: str, cwd: str | None = None) -> str:
    completed = host.run(argv, cwd=cwd, capture_output=True)
    if getattr(completed, "returncode", 1) != 0:
        detail = (getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or "").strip()
        raise MoveError(f"{what} failed on {getattr(host, 'host', 'the host')} "
                        f"(exit {completed.returncode}): {detail[:400]}")
    return _out(completed)


def _sh(host, script: str, *, what: str) -> str:
    """Run a shell *pipeline* — ``docker save | gzip`` and friends, which an argv cannot express."""
    return _checked(host, ["sh", "-c", script], what=what)


def inspect_instance(host, name: str, containers_dir: str) -> InstanceFacts:
    """Everything the move needs to know about the instance, asked of the source's Docker.

    The compose project name is the instance directory's name — that is compose's own rule, and
    it is why the target must land in a directory of the same name: the volumes are called
    ``<project>_<volume>``, so a different directory name would create differently-named volumes
    and the restored data would sit in volumes nothing mounts.
    """
    instance_dir = f"{containers_dir.rstrip('/')}/{name}"
    facts = InstanceFacts(name=name, instance_dir=instance_dir)

    containers = _checked(
        host,
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={name}",
         "--format", "{{.Names}}"],
        what="docker ps",
    ).split()
    if not containers:
        raise MoveError(
            f"No containers belong to compose project '{name}' on {getattr(host, 'host', 'the source')}. "
            f"Check the instance name (it is the directory name under {containers_dir}) — a project "
            "with no containers has nothing to move, and its compose file alone is not an instance."
        )
    facts.containers = containers

    for container in containers:
        image = _checked(host, ["docker", "inspect", "-f", "{{.Config.Image}}", container],
                         what=f"docker inspect {container}")
        if image and image not in facts.images:
            facts.images.append(image)

        volumes = _checked(
            host,
            ["docker", "inspect", "-f",
             '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}', container],
            what=f"docker inspect {container} mounts",
        ).split()
        for volume in volumes:
            if volume not in facts.volumes:
                facts.volumes.append(volume)

        # HostConfig.PortBindings, not NetworkSettings.Ports: the first is the declaration and
        # survives the container being stopped, which is exactly the state it is in when the
        # target-side port check needs the answer.
        bindings = _checked(
            host,
            ["docker", "inspect", "-f",
             "{{range $p, $c := .HostConfig.PortBindings}}{{range $c}}{{.HostPort}} {{end}}{{end}}",
             container],
            what=f"docker inspect {container} ports",
        ).split()
        for port in bindings:
            if port.isdigit() and int(port) not in facts.ports:
                facts.ports.append(int(port))

        facts.services[container] = _checked(
            host,
            ["docker", "inspect", "-f", '{{index .Config.Labels "com.docker.compose.service"}}',
             container],
            what=f"docker inspect {container} service",
        ) or container
        # --size is not free (the daemon walks the layer), which is why `docker ps` hides it by
        # default. It is worth it once per move: this number is the difference between a data
        # move and an empty database that reports itself healthy.
        size = _checked(host, ["docker", "inspect", "--size", "-f", "{{.SizeRw}}", container],
                        what=f"docker inspect {container} size")
        facts.layer_bytes[container] = int(size) if size.strip().isdigit() else 0

    facts.subnets = _subnets_of_project(host, name)
    return facts


def _subnets_of_project(host, project: str) -> list[str]:
    """The compose network's pinned subnet, if it has one.

    It has to travel with the instance and be checked on the target: every lab compose this repo
    generates pins its own /24 because Docker's default pool once handed a lab project
    172.18.0.0/16 and the host then routed a *production* SQL Server addressed inside that /16 into the
    bridge, taking its metrics down for two hours. A pinned subnet that clashes on the target
    fails ``compose up`` with "Pool overlaps with other one on this address space" — after the
    data has been restored, which is why it is checked before anything is transferred.
    """
    names = _checked(host, ["docker", "network", "ls", "--filter",
                            f"label=com.docker.compose.project={project}", "--format", "{{.Name}}"],
                     what="docker network ls").split()
    if not names:
        # A network compose created before it labelled them, or `network.name` set explicitly
        # (which is what the generated files do): fall back to the conventional name.
        names = [f"{project}_default"]
    subnets: list[str] = []
    for network in names:
        completed = host.run(["docker", "network", "inspect", "-f",
                              "{{range .IPAM.Config}}{{.Subnet}} {{end}}", network],
                             capture_output=True)
        if getattr(completed, "returncode", 1) != 0:
            continue
        for subnet in _out(completed).split():
            if subnet not in subnets:
                subnets.append(subnet)
    return subnets


# --------------------------------------------------------------------------- #
# Guards on the destination
# --------------------------------------------------------------------------- #
def volume_is_empty(host, volume: str, image: str) -> bool:
    """Does this named volume hold anything at all?

    Asked through a root container for the same reason everything else here is: the volume's
    contents belong to the engine's uid and are unreadable to the SSH user.
    """
    listing = _checked(host, _root_run(image, mounts=[f"{volume}:{_SRC_MOUNT}:ro"],
                                      script=f"ls -A {_SRC_MOUNT} | head -1"),
                       what=f"listing volume {volume}")
    return not listing.strip()


def assert_data_travels(host, facts: InstanceFacts, spec: MoveSpec) -> None:
    """Refuse a move whose bundle would not actually contain the database.

    The failure this prevents has already happened here once, with ``ora11g_lab``: the compose
    file declares a volume at ``/opt/oracle/oradata``, the gvenzl XE 11 image writes its
    datafiles to ``/u01/app/oracle/oradata``, and the two are not the same place. The volume was
    empty, the 15.2 GB database sat in the container's writable layer, and the destination came
    up **healthy** — as the stock image's empty database, under the right name, on the right
    port, answering the right password. Nothing about the result said it was wrong.

    So "the volumes are empty and the layer is not" is treated as what it is: the data is in the
    container, and moving the container is ``--commit-container``.
    """
    if spec.commit_container or not spec.include_volumes:
        return
    biggest = max(facts.layer_bytes.values() or [0])
    if biggest < WRITABLE_LAYER_DATA_BYTES:
        return
    if any(not volume_is_empty(host, volume, facts.helper_image) for volume in facts.volumes):
        return
    where = "declares no volumes" if not facts.volumes else (
        f"declares {', '.join(facts.volumes)}, and every one of them is empty")
    raise MoveError(
        f"'{facts.name}' {where}, while its container has written {biggest / 1e9:.1f} GB into its "
        f"own filesystem. Its data is in the container, not in a volume — shipping the image and "
        f"the volumes would start an EMPTY database on the destination, and it would report "
        f"itself healthy. Re-run with --commit-container to move the container's filesystem "
        f"(the bundle gets that much bigger), or with --no-volumes if an empty instance really "
        f"is what is wanted."
    )


def check_docker(host) -> None:
    for argv, what in ((["docker", "--version"], "docker"),
                       (["docker", "compose", "version"], "docker compose")):
        if getattr(host.run(argv, capture_output=True), "returncode", 1) != 0:
            raise MoveError(
                f"{what} is not usable as the SSH user on {getattr(host, 'host', 'the host')}. "
                "Install it (create-db-docker --install-docker does), or add the user to the "
                "docker group, then run the move again."
            )


def check_ports_free(host, ports: list[int]) -> None:
    """Refuse before the transfer if the destination already publishes one of these ports."""
    published: set[int] = set()
    completed = host.run(["docker", "ps", "--format", "{{.Ports}}"], capture_output=True)
    for match in re.finditer(r":(\d+)->", _out(completed)):
        published.add(int(match.group(1)))
    clashing = sorted(port for port in ports if port in published)
    if clashing:
        raise MoveError(
            f"Host port(s) already published by another container on "
            f"{getattr(host, 'host', 'the destination')}: {', '.join(map(str, clashing))}. "
            "Free them, or edit the moved instance's compose file afterwards to publish "
            "different ports."
        )


def check_subnets_free(host, subnets: list[str]) -> None:
    """Refuse a pinned subnet that overlaps one the destination's Docker already routes."""
    wanted = []
    for text in subnets:
        try:
            wanted.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            continue
    if not wanted:
        return
    existing_text = _sh(
        host,
        "for n in $(docker network ls -q); do "
        "docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' $n; done",
        what="reading docker network subnets",
    )
    for text in existing_text.split():
        try:
            existing = ipaddress.ip_network(text, strict=False)
        except ValueError:
            continue
        for network in wanted:
            if network.overlaps(existing):
                raise MoveError(
                    f"The instance pins {network}, which overlaps {existing} — already routed by "
                    f"Docker on {getattr(host, 'host', 'the destination')}. `compose up` would "
                    "fail with 'Pool overlaps with other one on this address space'. Pick another "
                    "/24 in the moved instance's compose file, or remove the network that holds it."
                )


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def _root_run(image: str, *, mounts: list[str], script: str) -> list[str]:
    """A throwaway ``--user 0`` container that runs ``script`` — how this module gets root.

    ``--entrypoint /bin/sh`` is not decoration: every database image here declares an entrypoint
    that starts the engine, so without it the "command" is passed to that script as arguments and
    the container spends a minute booting a database instead of running ``tar``.
    """
    argv = ["docker", "run", "--rm", "--user", "0", "--entrypoint", "/bin/sh"]
    for mount in mounts:
        argv += ["-v", mount]
    argv += [image, "-c", script]
    return argv


def stop_containers(host, containers: list[str]) -> None:
    """Stop the instance's containers by name — deliberately not ``docker compose stop``.

    Compose reads the project's ``.env`` before it does anything, and that file is 0600 root
    (it holds the database password), so every compose verb fails for the SSH user with
    "open .../.env: permission denied" — on a stack that same user can otherwise manage
    perfectly well through Docker. Container names come from the daemon's own project label,
    so this addresses exactly what compose would have addressed.

    It is also the honest verb for what ``--stop-source`` promises. The instances carry
    ``restart: unless-stopped``, and an explicit ``docker stop`` is the one thing that policy
    treats as final: the container stays down across a daemon restart and a reboot, and one
    ``docker start`` brings it back if the destination turns out to be wrong.
    """
    _checked(host, ["docker", "stop", "-t", str(STOP_TIMEOUT_SECONDS), *containers],
             what="docker stop")


def _volume_archive(volume: str) -> str:
    return f"volume_{volume}.tar.gz"


def export_bundle(host, facts: InstanceFacts, spec: MoveSpec, *, log=print) -> dict:
    """Write the whole instance — image, files, volume contents — into one staging directory.

    Returns the manifest. The stack is stopped around the volume export and started again
    before this function returns, whatever happens: leaving a lab database down because a
    ``tar`` failed is a worse outcome than the failure itself.
    """
    stage = spec.stage()
    _checked(host, ["rm", "-rf", stage], what="clearing the staging directory")
    _checked(host, ["mkdir", "-p", stage], what="creating the staging directory")

    images = list(facts.images)
    image_overrides: dict[str, str] = {}
    if spec.commit_container:
        # Committed from the STOPPED container, not the running one: a running Oracle's layer
        # includes datafiles mid-write, and an image made from those is a database that opens
        # and is wrong — the same reason the volume copy happens inside the stopped window.
        log(f"Stopping {spec.name} so its filesystem can be committed ...")
        stop_containers(host, facts.containers)
        stamp = time.strftime("%Y%m%d%H%M%S")
        images = []
        for container in facts.containers:
            moved = f"db_ops/{spec.name.lower()}-{facts.services.get(container, container).lower()}:moved-{stamp}"
            log(f"Committing {container} -> {moved} ...")
            _checked(host, ["docker", "commit", container, moved], what=f"docker commit {container}")
            images.append(moved)
            image_overrides[facts.services.get(container, container)] = moved

    log(f"Saving image(s) {', '.join(images)} ...")
    # gzip -1, not the default: docker save writes layers that are already compressed, so the
    # extra passes buy a few percent and cost minutes of CPU on a host that is also running
    # every other lab database.
    _sh(host, f"docker save {' '.join(shlex.quote(i) for i in images)} | "
              f"gzip -1 > {shlex.quote(posixpath.join(stage, IMAGES_ARCHIVE))}",
        what="docker save")

    log(f"Packing the instance directory {facts.instance_dir} ...")
    _checked(host, _root_run(
        facts.helper_image,
        mounts=[f"{spec.containers_dir.rstrip('/')}:{_SRC_MOUNT}:ro", f"{stage}:{_OUT_MOUNT}"],
        script=f"tar czf {_OUT_MOUNT}/{INSTANCE_ARCHIVE} -C {_SRC_MOUNT} {shlex.quote(spec.name)}",
    ), what="packing the instance directory")

    volumes: list[dict] = []
    if spec.include_volumes and facts.volumes:
        if not spec.commit_container:   # already stopped for the commit
            log(f"Stopping {spec.name} so its volumes can be copied consistently ...")
            stop_containers(host, facts.containers)
        try:
            for volume in facts.volumes:
                log(f"Packing volume {volume} ...")
                _checked(host, _root_run(
                    facts.helper_image,
                    mounts=[f"{volume}:{_SRC_MOUNT}:ro", f"{stage}:{_OUT_MOUNT}"],
                    script=f"tar czf {_OUT_MOUNT}/{_volume_archive(volume)} -C {_SRC_MOUNT} .",
                ), what=f"packing volume {volume}")
                volumes.append({"volume": volume, "archive": _volume_archive(volume)})
        finally:
            # Back up before anything else is reported, even if the packing raised. Whether the
            # source stays running is decided at the very end, on the evidence that the
            # destination works — not by where an error happened to land.
            log(f"Starting {spec.name} again on the source ...")
            _checked(host, ["docker", "start", *facts.containers], what="docker start")
    elif spec.commit_container:
        log(f"Starting {spec.name} again on the source ...")
        _checked(host, ["docker", "start", *facts.containers], what="docker start")

    # Everything above was written by root inside a container; the SSH user has to be able to
    # read it back out to stream it, and to delete the staging directory afterwards.
    ids = _sh(host, "id -u; id -g", what="reading the SSH user's uid/gid").split()
    _checked(host, _root_run(
        facts.helper_image, mounts=[f"{stage}:{_OUT_MOUNT}"],
        script=f"chown -R {ids[0]}:{ids[1]} {_OUT_MOUNT}",
    ), what="handing the bundle back to the SSH user")

    manifest = {
        "moved_by": "db_ops.sre.move-db-docker",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_host": getattr(host, "host", ""),
        "instance": facts.to_dict(),
        "images": images,
        "image_overrides": image_overrides,
        "images_archive": IMAGES_ARCHIVE,
        "instance_archive": INSTANCE_ARCHIVE,
        "volumes": volumes,
        "sizes": _artifact_sizes(host, stage),
    }
    host.write_text(posixpath.join(stage, MANIFEST), json.dumps(manifest, indent=2) + "\n")
    return manifest


def _artifact_sizes(host, stage: str) -> dict:
    """Bytes per artifact — the number an operator wants before agreeing to the transfer, and
    the one that explains a move that is taking longer than expected."""
    listing = _sh(host, f"cd {shlex.quote(stage)} && for f in *.tar.gz; do "
                        "echo \"$f $(stat -c %s \"$f\")\"; done",
                  what="measuring the bundle")
    sizes: dict[str, int] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            sizes[parts[0]] = int(parts[1])
    return sizes


# --------------------------------------------------------------------------- #
# Transfer
# --------------------------------------------------------------------------- #
def transfer_bundle(spec: MoveSpec, manifest: dict, *, log=print) -> list[dict]:
    """Relay every artifact from the source's staging directory to the destination's.

    ``common.cli relay-file`` and not two commands: the bytes never touch this machine's disk
    (the image archive alone is around a gigabyte, and a committed Oracle container is several),
    and the sha256 is compared across the whole trip rather than per hop, so a mismatch cannot
    point at the wrong half.

    Through the CLI, not by importing ``file_transfer``: ``common`` is the API layer and an app
    hands it a JSON object. That is also why the request below names its two hosts as *targets* —
    the subprocess resolves them from ``db_instances.json`` itself, and the SSH passwords stay
    inside it.
    """
    from db_ops.lib import common_cli

    stage = spec.stage()
    names = [manifest["images_archive"], manifest["instance_archive"]]
    names += [entry["archive"] for entry in manifest.get("volumes", [])]
    names.append(MANIFEST)

    results: list[dict] = []
    for name in names:
        size = manifest.get("sizes", {}).get(name)
        log(f"Relaying {name}" + (f" ({size} bytes)" if size else "") + " ...")
        results.append(common_cli.run("relay-file", {
            "source": {"target": spec.source_target, "path": posixpath.join(stage, name)},
            "destination": {"target": spec.dest_target, "path": posixpath.join(stage, name)},
            "overwrite": True,
            "make_dirs": True,
        }))
    return results


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def import_bundle(host, facts: InstanceFacts, spec: MoveSpec, manifest: dict, *, log=print) -> None:
    """Load the image, place the files, create the volumes, fill them, and start the stack.

    The order is the docstring at the top of this module; the one step that looks redundant —
    ``docker compose create`` before restoring the volumes — is the step that makes the volumes
    compose's own.
    """
    stage = spec.stage()
    instance_dir = spec.dest_instance_dir

    log("Loading the image(s) ...")
    _sh(host, f"gunzip -c {shlex.quote(posixpath.join(stage, IMAGES_ARCHIVE))} | docker load",
        what="docker load")

    log(f"Placing the instance directory in {instance_dir} ...")
    _checked(host, ["mkdir", "-p", spec.target_containers_dir],
             what="creating the containers directory")
    # Unpacked as root (it is root-owned on the source), then handed to the SSH user — which is
    # not tidiness. Compose reads `.env` before every verb, and `.env` is 0600; owned by root it
    # is unreadable to the user that has to run `compose create` two lines below, and the stack
    # would be unmanageable afterwards for the same reason. 0600 is preserved, so the password
    # is still private — to the account that is already in the docker group, i.e. already root
    # in every way that matters here. This is the same ownership `create-db-docker --remote-host`
    # produces, so a moved instance ends up indistinguishable from a provisioned one.
    ids = _sh(host, "id -u; id -g", what="reading the SSH user's uid/gid").split()
    _checked(host, _root_run(
        facts.helper_image,
        mounts=[f"{spec.target_containers_dir}:{_SRC_MOUNT}",
                f"{stage}:{_OUT_MOUNT}:ro"],
        script=f"tar xzf {_OUT_MOUNT}/{INSTANCE_ARCHIVE} -C {_SRC_MOUNT} && "
               f"chown -R {ids[0]}:{ids[1]} {_SRC_MOUNT}/{shlex.quote(spec.name)}",
    ), what="unpacking the instance directory")

    overrides = manifest.get("image_overrides") or {}
    if overrides:
        # An override file rather than an edit of docker-compose.yml: the file that travelled is
        # the one the operator wrote, and a moved instance that quietly disagrees with it is how
        # a later `git diff` or a re-deploy silently reverts the move. compose loads
        # docker-compose.override.yml automatically and merges it, so `docker compose up` in that
        # directory does the right thing with no extra arguments to remember.
        body = ["# Written by db_ops.sre.move-db-docker.",
                f"# The instance was moved from {manifest.get('source_host', 'another host')} with",
                "# --commit-container: its database lives in the container's own filesystem, not in",
                "# a named volume, so the image below IS the data. Deleting this file starts the",
                "# stock image instead — an empty database that looks healthy.",
                "services:"]
        for service, image in overrides.items():
            body += [f"  {service}:", f"    image: {image}"]
        host.write_text(f"{instance_dir}/docker-compose.override.yml", "\n".join(body) + "\n")
        log(f"Wrote docker-compose.override.yml pinning {', '.join(overrides.values())}")

    log("Creating the containers and volumes (compose create) ...")
    _checked(host, ["docker", "compose", "create"], what="docker compose create", cwd=instance_dir)

    for entry in manifest.get("volumes", []):
        volume, archive = entry["volume"], entry["archive"]
        log(f"Restoring volume {volume} ...")
        # The volume compose just made is empty; a stray lost+found or an aborted earlier run
        # would make `tar x` merge rather than replace, so it is emptied first.
        _checked(host, _root_run(
            facts.helper_image,
            mounts=[f"{volume}:{_SRC_MOUNT}", f"{stage}:{_OUT_MOUNT}:ro"],
            script=f"rm -rf {_SRC_MOUNT}/..?* {_SRC_MOUNT}/.[!.]* {_SRC_MOUNT}/* 2>/dev/null; "
                   f"tar xzf {_OUT_MOUNT}/{archive} -C {_SRC_MOUNT}",
        ), what=f"restoring volume {volume}")

    log("Starting the stack ...")
    _checked(host, ["docker", "compose", "up", "-d"], what="docker compose up", cwd=instance_dir)


# --------------------------------------------------------------------------- #
# The move
# --------------------------------------------------------------------------- #
def resolve_engine(spec: MoveSpec, *, data_dir=None) -> str:
    """Which engine this instance runs, for the health probe.

    Read from the connection registry the instance was registered in, because that is where the
    fact already lives; ``--engine`` is the escape hatch for an instance provisioned before the
    registry existed, or created by hand.
    """
    if spec.engine:
        return spec.engine
    if data_dir:
        try:
            registry = register_config.load_registry(register_config.default_registry_path(data_dir))
        except (OSError, ValueError):
            registry = {}
        for entry in registry.get(register_config.REGISTRY_ROOT_KEY, []):
            if isinstance(entry, dict) and entry.get("id") == register_config.connection_id(spec.name):
                engine = str(entry.get("engine") or "")
                if engine in ENGINE_META:
                    return engine
    raise MoveError(
        f"Cannot tell which engine '{spec.name}' runs, so the health probe has nothing to ask. "
        f"It is not in data/{register_config.REGISTRY_FILENAME}; pass --engine "
        f"({', '.join(sorted(ENGINE_META))})."
    )


def format_plan(facts: InstanceFacts, spec: MoveSpec, *, source_host: str, dest_host: str) -> str:
    lines = [
        f"Instance:        {spec.name}",
        f"From:            {source_host}:{facts.instance_dir}",
        f"To:              {dest_host}:{spec.dest_instance_dir}",
        f"Containers:      {', '.join(facts.containers)}",
        f"Image(s):        {', '.join(facts.images)}",
        f"Volumes:         {', '.join(facts.volumes) if facts.volumes else '(none)'}"
        + ("" if spec.include_volumes else "   [SKIPPED: --no-volumes]"),
        f"Published ports: {', '.join(map(str, facts.ports)) or '(none)'}",
        f"Pinned subnets:  {', '.join(facts.subnets) or '(none)'}",
        "Layer size:      " + ", ".join(
            f"{name} {size / 1e9:.2f} GB" for name, size in facts.layer_bytes.items())
        + ("   [MOVED: --commit-container]" if spec.commit_container
           else "   [NOT moved: only the image and the volumes travel]"),
        f"Staging dir:     {spec.stage()} (on both hosts)",
        f"Source after:    {'stopped (docker stop; containers, volumes and files kept)' if spec.stop_source else 'left running'}",
    ]
    if not spec.include_volumes and facts.volumes:
        lines.append("")
        lines.append("WARNING: --no-volumes ships the image and the compose file only. The moved "
                     "instance will initialise an EMPTY database on first start.")
    return "\n".join(lines)


def move(
    spec: MoveSpec,
    *,
    source,
    destination,
    data_dir=None,
    dry_run: bool = False,
    log=print,
) -> dict:
    """Move ``spec.name`` from ``source`` to ``destination``. Returns a result summary.

    ``source`` and ``destination`` are :class:`db_ops.sre.remote.RemoteUbuntuHost` — one SSH
    session each, used as both a command runner and a small filesystem.
    """
    engine = resolve_engine(spec, data_dir=data_dir)
    facts = inspect_instance(source, spec.name, spec.containers_dir)

    source_host = getattr(source, "host", "")
    dest_host = getattr(destination, "host", "")
    log(format_plan(facts, spec, source_host=source_host, dest_host=dest_host))
    if dry_run:
        return {"ok": True, "dry_run": True, "instance": facts.to_dict(), "engine": engine}

    # Every guard runs before a single byte is packed. The expensive failures in this workflow
    # are the ones discovered after the transfer: a taken port or an overlapping subnet costs
    # the whole bundle a second time, and the volumes have already been restored by then.
    check_docker(destination)
    # Before anything is torn down anywhere: this one refuses the move outright, and it would be
    # a poor trade to have destroyed the destination's existing instance first.
    assert_data_travels(source, facts, spec)
    if destination.exists(spec.dest_instance_dir) and not spec.force:
        raise MoveError(
            f"{dest_host}:{spec.dest_instance_dir} already exists. Pass --force to replace it "
            "(the existing stack there is taken down, WITH its volumes, before the moved one "
            "is written), or move the instance under a different containers dir."
        )

    # The teardown comes BEFORE the port and subnet checks, not after: an earlier attempt at this
    # same move is the most likely thing holding the ports, and checking first made --force
    # unable to replace any instance that publishes its own — it refused on the ports of the
    # stack it was about to remove.
    if spec.force and destination.exists(f"{spec.dest_instance_dir}/docker-compose.yml"):
        log(f"--force: tearing down the existing '{spec.name}' on {dest_host} (with its volumes) ...")
        destination.run(["docker", "compose", "down", "-v", "--remove-orphans"],
                        cwd=spec.dest_instance_dir, capture_output=True)
        destination.run(["rm", "-rf", spec.dest_instance_dir], capture_output=True)

    check_ports_free(destination, facts.ports)
    check_subnets_free(destination, facts.subnets)

    manifest = export_bundle(source, facts, spec, log=log)
    transferred = transfer_bundle(spec, manifest, log=log)
    import_bundle(destination, facts, spec, manifest, log=log)

    wait_seconds = int(spec.health_timeout) if spec.health_timeout else ENGINE_META[engine].health_timeout
    log(f"Waiting for health on {dest_host} (up to {wait_seconds}s) ...")
    statuses = healthcheck.wait_healthy(facts.containers, engine, timeout=wait_seconds,
                                        runner=destination.run)
    healthy = all(status == "healthy" for status in statuses.values())
    if not healthy:
        detail = healthcheck.failure_detail(statuses, runner=destination.run)
        raise MoveError(
            f"The moved instance did not become healthy on {dest_host} ({statuses}). The source "
            f"was left as it was, so nothing has been lost — fix the destination and re-run with "
            f"--force." + (f"\n{detail}" if detail else "")
        )

    registered = ""
    if spec.register and data_dir:
        registered = register_config.relocate_connection(
            register_config.default_registry_path(data_dir),
            register_config.connection_id(spec.name),
            host=dest_host, worker_host=dest_host,
            compose_path=f"{spec.dest_instance_dir}/docker-compose.yml",
        )
        log(f"Connection entry {registered} for {register_config.connection_id(spec.name)}.")

    if spec.stop_source:
        # Only now, with the destination proven healthy. Stopped, not removed: the source's
        # containers and volumes are the only other copy of the data until someone has looked at
        # the new host, and deleting them is a decision for a person, not for this command.
        log(f"Stopping the source stack on {source_host} (containers, volumes and files kept) ...")
        stop_containers(source, facts.containers)
        log(f"  undo: docker start {' '.join(facts.containers)}  on {source_host}")

    if not spec.keep_stage:
        for host in (source, destination):
            host.run(["rm", "-rf", spec.stage()], capture_output=True)

    return {
        "ok": True,
        "instance": facts.to_dict(),
        "engine": engine,
        "source_host": source_host,
        "destination_host": dest_host,
        "instance_dir": spec.dest_instance_dir,
        "statuses": statuses,
        "bytes_transferred": sum(int(item.get("bytes") or 0) for item in transferred),
        "artifacts": [item.get("destination", {}).get("path") for item in transferred],
        "source_stopped": bool(spec.stop_source),
        "registered": registered,
    }


def format_summary(result: dict) -> str:
    instance = result.get("instance", {})
    lines = [
        f"Instance:     {instance.get('name')}",
        f"Moved:        {result.get('source_host')} -> {result.get('destination_host')}",
        f"Directory:    {result.get('instance_dir')}",
        f"Volumes:      {', '.join(instance.get('volumes') or []) or '(none)'}",
        f"Ports:        {', '.join(map(str, instance.get('ports') or [])) or '(none)'}",
        f"Transferred:  {result.get('bytes_transferred')} bytes",
        f"Health:       {result.get('statuses')}",
        f"Source stack: {'stopped' if result.get('source_stopped') else 'left running'}",
    ]
    if result.get("registered"):
        lines.append(f"Registry:     {result['registered']}")
    return "\n".join(lines)
