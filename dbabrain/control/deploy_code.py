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
  that name the code (0.29.0 renamed both). Every other line is the worker's and stays;
- the node's ``data/``: what ``upgrade-config`` and ``sync-config`` in the new image change.

Before it writes anything it keeps the compose file and a tar of ``data/``, ``assets/`` and
``config.json`` under ``runtime/pre_deploy_<version>_<stamp>/`` - the way back. Without ``--apply``
it builds nothing and changes nothing: it reads the worker and prints the plan.

The passphrase reaches the remote ``docker compose`` through a script on stdin, never an argv, and
not on the script's first line, which is the only line the remote gate echoes.
"""

from __future__ import annotations

import base64
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
    new_text = "".join(lines)
    old_lines, new_lines = text.splitlines(), new_text.splitlines()
    plan.changes = [(old, new) for old, new in zip(old_lines, new_lines) if old != new]
    plan.text = new_text
    return plan


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


def keyed_script(remote_dir: str, secret_key: str, commands: list[str]) -> str:
    """A script that exports the passphrase and runs *commands* in *remote_dir*.

    The first line is ``cd`` - the remote gate echoes that line alone - and the key arrives
    base64-encoded through ``printf``, a shell builtin, so no process ever has it on its argv.
    """
    encoded = base64.b64encode(secret_key.encode("utf-8")).decode("ascii")
    body = "\n".join(commands)
    return (f"cd {shlex.quote(remote_dir)} || exit 1\n"
            f"export DBABRAIN_SECRET_KEY=\"$(printf %s '{encoded}' | base64 -d)\"\n"
            f"set -e\n{body}\n")


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
                platform: str = "linux/amd64") -> dict:
    """Plan, or with *apply* run, the code-only deployment described in the module docstring."""
    version = read_version()
    image = f"{IMAGE_REPO}:{version}"
    compose_path = f"{remote_dir.rstrip('/')}/docker-compose.yml"

    client = ssh_connect(host, user, password, port)
    try:
        code, compose_text, err = ssh_capture(client, f"cat {shlex.quote(compose_path)}")
        if code != 0:
            raise DeployCodeError(f"cannot read {compose_path}: {err.strip()[:200]}")
        plan = plan_compose(compose_text, image)
        service = compose_service(compose_text)
        print(f"version {version}: image {plan.previous_image} -> {image}; service '{service}', "
              f"container '{container}', root {remote_dir}")
        for old, new in plan.changes:
            print(f"  - {old.strip()}\n  + {new.strip()}")
        steps = ["build the image here" if not skip_build else "use the image already built here",
                 "save it to a tar and send it", "docker load, and compare the image id",
                 "keep the compose file and a tar of data/, assets/, config.json",
                 "write the compose file", "stop and remove the running container",
                 "upgrade-config (plan, then apply) and sync-config in the new image",
                 "start, then read the container and its version back"]
        if not apply:
            print("PLAN ONLY - nothing built, nothing changed. --apply would:\n  "
                  + "\n  ".join(f"{n}. {s}" for n, s in enumerate(steps, 1)))
            return {"applied": False, "image": image, "previous_image": plan.previous_image,
                    "compose_changes": plan.changes, "steps": steps}

        require_docker()
        if not skip_build:
            run_local(["docker", "build", "--platform", platform,
                       "--label", f"org.opencontainers.image.version={version}", "-t", image, "."],
                      cwd=DBABRAIN_ROOT)
        local_id = _local_image_id(image)
        BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
        tar = BUNDLE_DIR / f"{IMAGE_REPO}_image_{version}.tar"
        run_local(["docker", "save", image, "-o", str(tar)])
        remote_tar = f"{remote_dir.rstrip('/')}/runtime/images/{tar.name}"
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

        run = f"docker compose run --rm --no-deps -T {shlex.quote(service)} dbabrain"
        _remote_script(client, keyed_script(remote_dir, secret_key, [
            "docker compose stop -t 120",
            "docker compose rm -f",
            f"{run} common upgrade-config '{{}}'",
            f"{run} common upgrade-config '{{\"dry_run\": false}}'",
            f"{run} db --config config.json sync-config '{{}}'",
            "docker compose up -d",
        ]), "the switch")
        code, state, _ = ssh_capture(client, "docker inspect -f '{{.RestartCount}} {{.State.Status}} "
                                             f"{{{{.Config.Image}}}}' {shlex.quote(container)}")
        print(f"container {container}: {state.strip()}")
        ssh_run(client, f"docker exec {shlex.quote(container)} dbabrain --version")
        return {"applied": True, "image": image, "image_id": local_id, "backup": f"{remote_dir}/{backup}",
                "previous_image": plan.previous_image, "container_state": state.strip()}
    finally:
        client.close()
