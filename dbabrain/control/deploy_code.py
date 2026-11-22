"""Ship the code only: a new image to the worker, its own ``data/``, ``assets/`` and ``config.json`` kept.

0.28.0 was the first version soaked on the worker itself, and the tool had no command for it
(0.28.0 sheet §3.3 T1, 0.29.0 item 1.118): ``build-image`` opens the store for the config-drift gate
and ``copy`` / ``deploy`` carry the master's ``data/`` and ``assets/`` with the image - on
2026-10-09 the master's ``restore_config.json`` had the worker's one nightly restore switched off,
and a full deploy would have ended it without a word. So it was done by hand: build, save, send,
load, one line of the compose file, ``upgrade-config`` and ``sync-config`` in the new image, start.
This is that run, written down once.

What it changes on the worker, and nothing else:

- the image: built here from this tree, sent as a tar, loaded, its id compared with the one built;
- the compose file: the tool's ``image:`` line, and - through ``lib.package_rename``, the rules
  ``upgrade-config`` applies to ``data/`` - the environment-variable names and the container paths
  that name the code (0.29.0 renamed both). And the passphrase's line (0.42.0, item 1.409): the
  ``DBABRAIN_SECRET_KEY`` environment entry becomes ``DBABRAIN_UNLOCK_FILE`` and the host's
  ``/run/dbabrain`` (a ``tmpfs``) is mounted, so the daemon starts locked and is handed the
  passphrase after the start (``control.worker_unlock``) - never again in Docker's configuration,
  which kept it in clear on the host's disk. Every other line is the worker's and stays;
- the node's ``data/``: what ``upgrade-config`` and ``sync-config`` in the new image change.

**The image may be built elsewhere** (``--image-tar`` with ``--image-id``, 2026-11-11 - the
operator: *docker runs on the build host now*, not on this PC): the tar ``docker save`` wrote there, and the
id ``docker image inspect`` gave there. Nothing then needs Docker here; the worker's loaded image is
still held to that id, and a tar that is not of this version's tag is refused by the load check.

Before it writes anything it keeps the compose file and a tar of ``data/``, ``assets/`` and
``config.json`` under ``runtime/pre_deploy_<version>_<stamp>/`` - the way back **for the switch
only**. Once the new container runs, that folder, the loaded tar, ``upgrade-config``'s copies and
every other ``dbabrain`` image are removed (``worker_prune``; the operator, 2026-11-20: *the worker
holds a single dbabrain, no backup, no old version*). A failed switch keeps them. Rolling back is a
rebuild of the previous tag, sent the same way. Without ``--apply`` it builds nothing and changes
nothing: it reads the worker and prints the plan.

The passphrase reaches the one-off ``docker compose run`` containers and the new daemon through a
script on stdin, never an argv, never a container's configuration, and not on the script's first
line, which is the only line the remote gate echoes.
"""

from __future__ import annotations

import difflib
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dbabrain.control._support import (
    BUNDLE_DIR,
    DBABRAIN_ROOT,
    DEFAULT_CONTAINER,
    DEFAULT_REMOTE_DIR,
    IMAGE_REPO,
    read_version,
    require_docker,
    run_local,
    ssh_capture,
    ssh_connect,
    ssh_run,
)
from dbabrain.control.worker_prune import prune_worker
from dbabrain.control.worker_unlock import UNLOCK_DIR, UNLOCK_FILE, keyed_compose_run, unlock_worker
from dbabrain.lib import package_rename
from dbabrain.lib.timezone import file_stamp

#: An image line that is the tool's: this repository's name before or after 0.29.0, or the published one.
_TOOL_IMAGE = re.compile(rf"^(?P<lead>\s*image:\s*)(?P<image>(?:{package_rename.OLD_PACKAGE}|dbabrain|"
                         r"[\w.\-]+/[\w.\-]+/dbabrain)(?::\S+)?)\s*$")

#: The environment variables a compose file hands the tool - the new spellings the rename may write.
COMPOSE_ENV_NAMES = frozenset({"DBABRAIN_CONFIG", "DBABRAIN_DAEMON_DELAY", "DBABRAIN_NODE_ROLE",
                               "DBABRAIN_SECRET_KEY", "DBABRAIN_TIMEZONE", "DBABRAIN_HOME",
                               "DBABRAIN_DATA_DIR", "DBABRAIN_KEY_BASE64"})


class DeployCodeError(SystemExit):
    """A refusal that stops the run before (or instead of) changing the worker."""


@dataclass
class ComposePlan:
    text: str
    changes: list[tuple[str, str]] = field(default_factory=list)
    previous_image: str = ""


def plan_compose(text: str, image: str) -> ComposePlan:
    """The worker's compose file pointed at *image*, with the names the rename moved; what changed.

    Only the tool's own ``image:`` line is replaced - a compose file that runs another service
    keeps it - and exactly one such line has to be there, or the file is not one this command
    understands and nothing is written.
    """
    lines = text.splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines) if _TOOL_IMAGE.match(line.rstrip("\r\n"))]
    if len(hits) != 1:
        raise DeployCodeError(f"the compose file has {len(hits)} image line(s) of the tool; expected one")
    plan = ComposePlan(text="")
    index = hits[0]
    match = _TOOL_IMAGE.match(lines[index].rstrip("\r\n"))
    plan.previous_image = match["image"]
    ending = lines[index][len(lines[index].rstrip("\r\n")):]
    lines[index] = f"{match['lead']}{image}{ending}"
    for number, line in enumerate(lines):
        renamed = package_rename.renamed(line, COMPOSE_ENV_NAMES) if number != index else line
        if renamed != line:
            lines[number] = renamed
    lines = unlock_through_a_file(lines, service_index=index)
    new_text = "".join(lines)
    plan.changes = _changed_lines(text.splitlines(), new_text.splitlines())
    plan.text = new_text
    return plan


def _changed_lines(old_lines: list[str], new_lines: list[str]) -> list[tuple[str, str]]:
    """Pairs of (before, after); a line only added or only removed is paired with ``""``."""
    changes: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old, new = old_lines[a0:a1], new_lines[b0:b1]
        width = max(len(old), len(new))
        old += [""] * (width - len(old))
        new += [""] * (width - len(new))
        changes.extend(zip(old, new))
    return changes


_KEY_ENTRY = re.compile(r"^(?P<lead>\s*)(?:(?P<dash>-\s*)DBABRAIN_SECRET_KEY(?:=.*)?|DBABRAIN_SECRET_KEY:.*)\s*$")
_UNLOCK_ENTRY = re.compile(r"^\s*(?:-\s*)?DBABRAIN_UNLOCK_FILE[:=]")
_UNLOCK_COMMENT = ("# The passphrase is NOT here: the daemon starts locked and the master hands it over",
                   "# into this file (tmpfs), read once and removed - `control unlock-worker` (0.42.0, 1.409).")


def unlock_through_a_file(lines: list[str], *, service_index: int) -> list[str]:
    """The tool's service with its passphrase entry turned into the unlock file, and its tmpfs mounted.

    The ``DBABRAIN_SECRET_KEY`` entry - either compose spelling - and the comment block right above
    it are replaced by ``DBABRAIN_UNLOCK_FILE``; ``/run/dbabrain`` is added to the service's
    volumes. A file already converted is returned unchanged. A service that neither hands the
    passphrase over nor names an unlock file is not one this command understands: the daemon it
    would start could decrypt nothing.
    """
    lines = list(lines)
    start, end = _service_span(lines, service_index)
    ending = lines[service_index][len(lines[service_index].rstrip("\r\n")):] or "\n"
    if not any(_UNLOCK_ENTRY.match(line.rstrip("\r\n")) for line in lines[start:end]):
        hits = [n for n in range(start, end) if _KEY_ENTRY.match(lines[n].rstrip("\r\n"))]
        if len(hits) != 1:
            raise DeployCodeError("the tool's service hands over no passphrase (DBABRAIN_SECRET_KEY) and names no "
                                  "unlock file (DBABRAIN_UNLOCK_FILE); expected exactly one of them")
        at = hits[0]
        match = _KEY_ENTRY.match(lines[at].rstrip("\r\n"))
        lead = match["lead"]
        entry = (f"{lead}- DBABRAIN_UNLOCK_FILE={UNLOCK_FILE}" if match["dash"] is not None
                 else f"{lead}DBABRAIN_UNLOCK_FILE: {UNLOCK_FILE}")
        top = at
        while top - 1 > service_index and lines[top - 1].startswith(lead + "#"):
            top -= 1
        lines[top:at + 1] = [*(f"{lead}{comment}{ending}" for comment in _UNLOCK_COMMENT), f"{entry}{ending}"]
        start, end = _service_span(lines, service_index)
    if not any(f"{UNLOCK_DIR}:" in line for line in lines[start:end]):
        volumes = [n for n in range(start, end) if re.match(r"^\s+volumes:\s*$", lines[n].rstrip("\r\n"))]
        if len(volumes) != 1:
            raise DeployCodeError(f"the tool's service has {len(volumes)} volumes: key(s); expected one, "
                                  f"to mount {UNLOCK_DIR}")
        item = next((line for line in lines[volumes[0] + 1:end] if line.lstrip().startswith("- ")), None)
        if item is None:
            raise DeployCodeError("the tool's service's volumes: list is empty; cannot place the unlock mount")
        indent = item[:len(item) - len(item.lstrip())]
        lines[volumes[0] + 1:volumes[0] + 1] = [
            f"{indent}# tmpfs on the host: the daemon's unlock file, gone with a reboot (1.409){ending}",
            f"{indent}- {UNLOCK_DIR}:{UNLOCK_DIR}{ending}"]
    return lines


def _service_span(lines: list[str], index: int) -> tuple[int, int]:
    """The lines of the service the line at *index* belongs to: from its name to the next one."""
    top = index
    while top > 0 and not re.match(r"^  [A-Za-z0-9_.\-]+:\s*$", lines[top].rstrip("\r\n")):
        top -= 1
    end = top + 1
    while end < len(lines):
        bare = lines[end].rstrip("\r\n")
        if bare.strip() and not bare.lstrip().startswith("#") and len(bare) - len(bare.lstrip()) <= 2:
            break
        end += 1
    return top, end


def compose_service(text: str) -> str:
    """The first service's name - the one the tool runs as."""
    in_services = False
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services:
            match = re.match(r"^  ([A-Za-z0-9_.\-]+):\s*$", line)
            if match:
                return match.group(1)
            if line and not line.startswith(" ") and not line.startswith("#"):
                break
    raise DeployCodeError("the compose file names no service")


def switch_script(remote_dir: str, service: str, secret_key: str) -> str:
    """Stop the old container, upgrade the configuration in the new image, start the new one.

    The first line is ``cd`` - the remote gate echoes that line alone. The passphrase is in no
    shell's environment and no container's configuration: each one-off run reads it on stdin
    (:func:`keyed_compose_run`), and the daemon ``up -d`` starts is locked until it is handed over.
    """
    return "\n".join([
        f"cd {shlex.quote(remote_dir)} || exit 1",
        "set -e",
        "docker compose stop -t 120",
        "docker compose rm -f",
        keyed_compose_run(service, secret_key, "common upgrade-config '{}'"),
        keyed_compose_run(service, secret_key, "common upgrade-config '{\"dry_run\": false}'"),
        keyed_compose_run(service, secret_key, "db --config config.json sync-config '{}'"),
        "docker compose up -d",
    ]) + "\n"


def _local_image_id(tag: str) -> str:
    out = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", tag],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise DeployCodeError(f"no local image {tag}: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def _remote_script(client, script: str, what: str) -> str:
    result = client.run_script(script)
    output = (result.stdout or "") + (result.stderr or "")
    print(output.rstrip())
    if not result.ok:
        raise DeployCodeError(f"{what} failed (exit {result.exit_code}); the worker's previous state "
                              "is kept under runtime/pre_deploy_*")
    return output


def deploy_code(*, host: str, user: str, password: str, port: int = 22, secret_key: str,
                remote_dir: str = DEFAULT_REMOTE_DIR, container: str = DEFAULT_CONTAINER,
                apply: bool = False, skip_build: bool = False,
                platform: str = "linux/amd64", image_tar: str | Path | None = None,
                image_id: str = "") -> dict:
    """Plan, or with *apply* run, the code-only deployment described in the module docstring."""
    if bool(image_tar) != bool(image_id):
        raise DeployCodeError("--image-tar and --image-id go together: the tar built elsewhere, and the id "
                              "it was built with - the worker's loaded image is held to that id.")
    if image_tar and not re.fullmatch(r"sha256:[0-9a-f]{64}", str(image_id).strip()):
        raise DeployCodeError(f"--image-id must be the image's full id (sha256:<64 hex>); got {image_id!r}.")
    if image_tar and not Path(image_tar).is_file():
        raise DeployCodeError(f"--image-tar {image_tar} is not a file here.")
    version = read_version()
    image = f"{IMAGE_REPO}:{version}"
    compose_path = f"{remote_dir.rstrip('/')}/docker-compose.yml"

    client = ssh_connect(host, user, password, port)
    try:
        code, fstype, _ = ssh_capture(client, f"stat -f -c %T {shlex.quote(UNLOCK_DIR.rsplit('/', 1)[0] or '/')}")
        if code == 0 and fstype.strip() and fstype.strip() != "tmpfs":
            raise DeployCodeError(f"{UNLOCK_DIR} is on {fstype.strip()}, not a tmpfs: the passphrase written there "
                                  "would outlive a reboot and sit on the disk")
        code, compose_text, err = ssh_capture(client, f"cat {shlex.quote(compose_path)}")
        if code != 0:
            raise DeployCodeError(f"cannot read {compose_path}: {err.strip()[:200]}")
        plan = plan_compose(compose_text, image)
        service = compose_service(compose_text)
        print(f"version {version}: image {plan.previous_image} -> {image}; service '{service}', "
              f"container '{container}', root {remote_dir}")
        for old, new in plan.changes:
            print(f"  - {old.strip()}\n  + {new.strip()}")
        steps = [("use the image tar built elsewhere" if image_tar else
                  "build the image here" if not skip_build else "use the image already built here"),
                 "save it to a tar and send it", "docker load, and compare the image id",
                 "keep the compose file and a tar of data/, assets/, config.json",
                 "write the compose file", "stop and remove the running container",
                 "upgrade-config (plan, then apply) and sync-config in the new image",
                 "start (locked), then hand the daemon its passphrase (unlock-worker)",
                 "read the container and its version back",
                 "remove every other dbabrain image, the tar and the way back (worker_prune)"]
        if not apply:
            print("PLAN ONLY - nothing built, nothing changed. --apply would:\n  "
                  + "\n  ".join(f"{n}. {s}" for n, s in enumerate(steps, 1)))
            return {"applied": False, "image": image, "previous_image": plan.previous_image,
                    "compose_changes": plan.changes, "steps": steps}

        if image_tar:
            local_id = str(image_id).strip()
            tar = Path(image_tar)
        else:
            require_docker()
            if not skip_build:
                run_local(["docker", "build", "--platform", platform,
                           "--label", f"org.opencontainers.image.version={version}", "-t", image, "."],
                          cwd=DBABRAIN_ROOT)
            local_id = _local_image_id(image)
            BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
            tar = BUNDLE_DIR / f"{IMAGE_REPO}_image_{version}.tar"
            run_local(["docker", "save", image, "-o", str(tar)])
        remote_tar = f"{remote_dir.rstrip('/')}/runtime/images/{IMAGE_REPO}_image_{version}.tar"
        client.put(tar, remote_tar)
        ssh_run(client, f"docker load -i {shlex.quote(remote_tar)}")
        code, remote_id, _ = ssh_capture(client, f"docker image inspect -f '{{{{.Id}}}}' {shlex.quote(image)}")
        if code != 0 or remote_id.strip() != local_id:
            raise DeployCodeError(f"the loaded image is not the one built: {remote_id.strip()} != {local_id}")

        backup = f"runtime/pre_deploy_{version}_{file_stamp()}"
        ssh_run(client, f"cd {shlex.quote(remote_dir)} && mkdir -p {backup} && "
                        f"cp -p docker-compose.yml {backup}/docker-compose.yml.before && "
                        f"tar czf {backup}/data.tgz data assets config.json")
        client.put_bytes(plan.text.encode("utf-8"), compose_path)

        _remote_script(client, switch_script(remote_dir, service, secret_key), "the switch")
        # The new daemon waits locked; the predecessor's state may still be on the tmpfs, hence force.
        if unlock_worker(client, container=container, secret_key=secret_key, force=True) != 0:
            raise DeployCodeError("the new container runs but its daemon is still locked - run "
                                  "`control unlock-worker` (the way back is kept under runtime/pre_deploy_*)")
        code, state, _ = ssh_capture(client, "docker inspect -f '{{.RestartCount}} {{.State.Status}} "
                                             f"{{{{.Config.Image}}}}' {shlex.quote(container)}")
        print(f"container {container}: {state.strip()}")
        ssh_run(client, f"docker exec {shlex.quote(container)} dbabrain --version")
        # The way back lived only for the switch. Once the new container runs, it goes, with every
        # other image and the tar just loaded: the worker holds one dbabrain (worker_prune). A
        # container that is not running keeps all of it - prune_worker refuses then.
        pruned = prune_worker(client, remote_dir=remote_dir, container=container, apply=True)
        return {"applied": True, "image": image, "image_id": local_id,
                "backup": f"{remote_dir}/{backup}" if pruned != 0 else "",
                "pruned": pruned == 0,
                "previous_image": plan.previous_image, "container_state": state.strip()}
    finally:
        client.close()
