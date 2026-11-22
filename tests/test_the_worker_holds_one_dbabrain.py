"""The worker holds one dbabrain: the image its container runs, and nothing a deploy left behind.

The operator, 2026-11-20 (translated): *clean every old version off the worker; from now on a
deploy keeps no backup and no old version - the worker holds a single dbabrain*. Read on the host
that day: 54 superseded images, 70 image tars (24 GB) and 64 tars of ``data/`` - each one more
copy of the encrypted secret store and the SSH keys, readable by any login on the host.

Before that a deploy kept the newest five version tags as a rollback window (2026-08-18: 383 tags
and 526 dangling images had piled up). A rollback is now a rebuild of the previous tag.

What must hold, each a test below:

- **What stays is the image the running container uses**, read from the container - never a
  version list, which once kept the wrong set (a lexical sort) and could keep a tag nothing runs.
- **A container that is not running refuses the whole prune**: then a leftover may be the way back.
- **A plan removes nothing** - it carries no removing command at all.
- It runs under ``sudo`` (the container writes as root) and never fails the deploy that called it.
"""

from dbabrain.control import worker_prune
from dbabrain.control.worker_prune import LEFTOVERS, NOT_RUNNING, prune_script, prune_worker


def test_what_stays_is_read_from_the_running_container():
    script = prune_script(container="dbabrain", apply=True)

    assert "keep=$(docker inspect -f '{{.Image}}' dbabrain" in script
    assert "docker image inspect -f '{{.Id}}' \"$ref\"" in script
    assert "if [ \"$id\" != \"$keep\" ]" in script
    assert "sort -V" not in script and "--format '{{.Tag}}'" not in script, "no version list decides"


def test_a_container_that_is_not_running_refuses_before_anything_is_removed():
    script = prune_script(container="dbabrain", apply=True)

    refusal = script.index(f"exit {NOT_RUNNING}")
    assert "[ \"$running\" != true ]" in script
    assert refusal < script.index("docker rmi") and refusal < script.index("rm -rf")


def test_every_deploy_leftover_is_named():
    for path in ("runtime/pre_deploy_*", "runtime/pre_soak_*", "runtime/config_upgrade",
                 "runtime/images", "dbabrain_image.tar"):
        assert path in LEFTOVERS
    script = prune_script(apply=True)
    assert "rm -rf -- \"$p\"" in script and "docker image prune -f" in script


def test_a_plan_carries_no_removing_command():
    script = prune_script(apply=False)

    assert "docker rmi" not in script and "rm -rf" not in script and "image prune" not in script
    assert "echo \"image $ref\"" in script and "echo \"file $p" in script, "it still lists"


def test_untagged_images_are_not_handed_to_rmi_by_a_bogus_name():
    assert "grep -v ':<none>$'" in prune_script(apply=True)


def test_it_runs_under_sudo_and_never_fails_its_caller(monkeypatch):
    calls = []

    def fake_ssh_run(client, command, *, sudo=False, check=True, **_kwargs):
        calls.append((command, sudo, check))
        return NOT_RUNNING

    monkeypatch.setattr(worker_prune, "ssh_run", fake_ssh_run)
    assert prune_worker(object(), apply=True) == NOT_RUNNING
    (_command, sudo, check), = calls
    assert sudo is True, "config_upgrade/ and parts of pre_deploy_* are root's"
    assert check is False, "a deploy that worked is not failed by its tidying"


def test_the_script_is_not_wrapped_in_another_layer_of_quoting():
    """`sh -c '<script>'` would break on the script's own single quotes; sudo quotes it once."""
    assert prune_script().startswith("cd ")


def test_deploy_no_longer_keeps_a_rollback_window_of_images():
    from dbabrain.control import deploy

    assert not hasattr(deploy, "KEEP_IMAGE_VERSIONS") and not hasattr(deploy, "_prune_old_images")
    assert deploy.prune_worker is prune_worker


# --------------------------------------------------------------------------- #
# ...and holds it closed (0.42.0 item 1.408): read 2026-11-20, the unencrypted SSH private keys
# were 644/664 in a 755 folder - readable by any login on the host, docker group or not.
# --------------------------------------------------------------------------- #
def test_the_secrets_are_closed_to_everyone_but_their_owner():
    script = prune_script(apply=True)

    assert "chmod 700 data/ssh_keys" in script
    assert "find data/ssh_keys -type f ! -name '*.pub' ! -name '.gitkeep' -exec chmod 600 {} +" in script
    assert "chmod 600 data/encrypted_secret_text.json" in script


def test_the_tool_root_is_closed_to_other_logins_without_walking_its_trees():
    script = prune_script(apply=True)

    assert "chmod 750 data runtime logs" in script
    assert "chmod -R" not in script, "logs/ is tens of GB; a closed folder closes what is under it"


def test_a_plan_shows_the_modes_and_changes_none():
    script = prune_script(apply=False)

    assert "chmod" not in script
    assert "-perm /077" in script and "(would be 600)" in script
