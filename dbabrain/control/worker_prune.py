"""The worker holds one dbabrain: the image its container runs, and nothing a deploy left behind.

The operator, 2026-11-20 (translated): *clean every old version off the worker; from now on a
deploy keeps no backup and no old version - the worker holds a single dbabrain*. What was there
that day, read on the host: 54 superseded ``dbabrain:<version>`` images (~1.6 GB each), 70 image
tars under ``runtime/images/`` (24 GB), 63 ``runtime/pre_deploy_*`` folders and a
``runtime/pre_soak_0_28_0`` - each a ``data.tgz`` of ``data/``, so 64 more copies of the encrypted
secret store and of ``data/ssh_keys/*.key``, readable by any login on the host - and 36 folders of
``runtime/config_upgrade/`` copies.

Disk was the smaller cost. Every copy of ``data/`` is one more place a secret lives that nobody
rotates, audits or remembers; a rollback window was the reason to keep them, and a rollback is
done by rebuilding the previous tag (``git archive`` on the build host, ``deploy-code --image-tar``)
rather than by a pile on the production host.

**What is kept is decided by the running container, never by a version list.** The image whose
id the container runs on stays, with every tag it carries; every other ``dbabrain`` image goes.
When the container is not running, nothing is removed: then a leftover may be the way back, and
which image is "the one" is not known.

``deploy`` and ``deploy-code`` call :func:`prune_worker` as their last step, once the new
container is up; ``control worker-prune`` runs it by hand (a plan unless ``--apply``).
"""

from __future__ import annotations

import shlex

from dbabrain.control._support import (
    DEFAULT_CONTAINER,
    DEFAULT_REMOTE_DIR,
    IMAGE_REPO,
    IMAGE_TAR_NAME,
    ssh_run,
)

#: What a deploy writes beside the tool root and must not leave there, relative to the remote dir.
#: ``runtime/config_upgrade`` holds the copies ``upgrade-config`` makes of each file it rewrites.
LEFTOVERS = ("runtime/pre_deploy_*", "runtime/pre_soak_*", "runtime/config_upgrade",
             "runtime/images", IMAGE_TAR_NAME)

#: Exit code of a refusal: the container is not running, so nothing was touched.
NOT_RUNNING = 3


#: Folders no other login on the host needs to enter (0.42.0 item 1.408). The container runs as
#: root and the master reads over SFTP as the owner, so neither loses anything. Directories, not
#: their trees: a folder another login cannot enter closes every file under it, and ``logs/`` is
#: tens of GB that a recursive chmod would walk for nothing.
CLOSED_DIRS = ("data", "runtime", "logs")


def _closing_modes(apply: bool) -> str:
    """Close the tool root to every other login, and the secrets to everyone but their owner.

    Read 2026-11-20: ``data/ssh_keys/*.key`` (unencrypted private keys) at 644/664 in a 755 folder,
    ``data/`` 755 with the encrypted store 664 - readable by any login on the host, in the docker
    group or not (plan 20261120_plan_worker_secret_exposure.md, F4/F5). Nothing set them: ``put``
    writes with the login's umask, and ``reclaim_worker_files`` deliberately only chowns.
    """
    keys = "find data/ssh_keys -type f ! -name '*.pub' ! -name '.gitkeep'"
    if not apply:
        return (f"for d in {' '.join(CLOSED_DIRS)} data/ssh_keys; do [ -d \"$d\" ] && "
                "stat -c 'mode %a %n' \"$d\"; done; "
                f"[ -d data/ssh_keys ] && {keys} -perm /077 -printf 'mode %m %p (would be 600)\\n'; "
                "[ -f data/encrypted_secret_text.json ] && stat -c 'mode %a %n' data/encrypted_secret_text.json; ")
    return (f"chmod 750 {' '.join(CLOSED_DIRS)} 2>/dev/null; "
            f"if [ -d data/ssh_keys ]; then chmod 700 data/ssh_keys; {keys} -exec chmod 600 {{}} +; fi; "
            "[ -f data/encrypted_secret_text.json ] && chmod 600 data/encrypted_secret_text.json; "
            "echo \"modes: $(stat -c '%a %n' data data/ssh_keys runtime logs 2>/dev/null | tr '\\n' ' ')\"; ")


def prune_script(*, remote_dir: str = DEFAULT_REMOTE_DIR, container: str = DEFAULT_CONTAINER,
                 apply: bool = False) -> str:
    """One shell line that lists - and with *apply* removes - every other image and every leftover."""
    # A plan carries no removing command at all, rather than a guarded one: nothing to get wrong.
    rmi = "docker rmi \"$ref\" >/dev/null || echo \"  could not remove $ref\"; " if apply else ""
    rm = "rm -rf -- \"$p\"; " if apply else ""
    leftovers = " ".join(LEFTOVERS)  # globs on purpose: the shell expands them in the remote dir
    return (
        f"cd {shlex.quote(remote_dir)} || exit 1; "
        f"keep=$(docker inspect -f '{{{{.Image}}}}' {shlex.quote(container)} 2>/dev/null); "
        f"running=$(docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(container)} 2>/dev/null); "
        f"if [ \"$running\" != true ] || [ -z \"$keep\" ]; then "
        f"echo 'REFUSED: {container} is not running - nothing removed'; exit {NOT_RUNNING}; fi; "
        f"echo \"keep image $keep (the one {container} runs)\"; "
        f"for ref in $(docker images {IMAGE_REPO} --format '{{{{.Repository}}}}:{{{{.Tag}}}}' | grep -v ':<none>$'); do "
        "id=$(docker image inspect -f '{{.Id}}' \"$ref\"); "
        f"if [ \"$id\" != \"$keep\" ]; then echo \"image $ref\"; {rmi}fi; done; "
        f"for p in {leftovers}; do [ -e \"$p\" ] || continue; "
        f"echo \"file $p $(du -sh -- \"$p\" | cut -f1)\"; {rm}done; "
        + _closing_modes(apply)
        + ("docker image prune -f | tail -1; " if apply else "")
        + f"echo \"{IMAGE_REPO} images left: $(docker images {IMAGE_REPO} -q | sort -u | wc -l)\""
    )


def prune_worker(client, *, remote_dir: str = DEFAULT_REMOTE_DIR, container: str = DEFAULT_CONTAINER,
                 apply: bool = False) -> int:
    """List, or with *apply* remove, what the worker holds beyond the one running dbabrain.

    Under ``sudo``: the container runs as root, so ``config_upgrade/`` and parts of
    ``pre_deploy_*`` are root's, and an ordinary login would leave them behind without a word.
    Never ``check``: by the time a deploy calls this the new container is up and verified, and a
    deploy that worked is not reported as failed over its tidying; the exit code says how it went.
    """
    print(f"\n=== worker holds one {IMAGE_REPO} {'(removing)' if apply else '(plan - nothing removed)'} ===")
    return ssh_run(client, prune_script(remote_dir=remote_dir, container=container, apply=apply),
                   sudo=True, check=False)
