"""The bind mount that lets a lab SQL Server actually be a restore target.

`RESTORE ... FROM DISK` is read by the **engine**, not by whatever put the file there. The restore
workflow copies a .bak onto the container host over SSH and then hands SQL Server that same path —
so unless the host directory is mounted into the container at the identical path, the engine is
told to read a file it cannot see. The error names a path that plainly exists when you go and
look for it over SSH, which is the expensive part.

That is how MSSQL25 on 192.0.2.11 was created on 2026-08-10: a named volume for
`/var/opt/mssql` and nothing else, while the proven target on 192.0.2.249 had
`/opt/mssql2025/backup -> /opt/mssql2025/backup` added by hand. These tests pin the mount into the
template so the next lab instance does not need the same hour.
"""

from db_ops.sre.docker_db import templates as t
from db_ops.sre.docker_db.models import DEFAULT_BACKUP_MOUNT, ENGINE_META, DockerDbSpec


def _spec(engine="mssql", **overrides):
    values = dict(name="MSSQL25", engine=engine, version="2025-latest", mode="single",
                  host_port=1433, password_env="MSSQL25_PASSWORD")
    values.update(overrides)
    return DockerDbSpec(**values)


def _compose(spec):
    return t.render(t.load_template(spec.engine, spec.mode), t.build_context(spec))


def test_a_sqlserver_lab_is_mounted_at_the_same_path_on_both_sides():
    """One string in restore_config.json has to mean the same file to the workflow and the engine.
    A mount at a *different* container path would look fine and fail at RESTORE time."""
    compose = _compose(_spec())

    assert f'- "{DEFAULT_BACKUP_MOUNT}:{DEFAULT_BACKUP_MOUNT}"' in compose


def test_the_data_volume_is_still_a_named_volume():
    """The bind mount is for backups only — the database files stay under Docker's ownership, or
    the container hits the host-uid mismatch the template was written to avoid."""
    compose = _compose(_spec())

    assert "- MSSQL25_data:/var/opt/mssql" in compose
    assert "MSSQL25_data:" in compose.split("volumes:")[-1]


def test_a_caller_can_ask_for_no_mount_at_all():
    """An explicit empty value is "none", distinct from "not stated" which takes the default."""
    compose = _compose(_spec(backup_mount=""))

    assert DEFAULT_BACKUP_MOUNT not in compose
    assert "- MSSQL25_data:/var/opt/mssql" in compose


def test_a_caller_can_point_the_mount_somewhere_else():
    compose = _compose(_spec(backup_mount="/srv/restore_in"))

    assert '- "/srv/restore_in:/srv/restore_in"' in compose
    assert DEFAULT_BACKUP_MOUNT not in compose


def test_not_stating_a_mount_takes_the_engine_default():
    assert _spec().resolved_backup_mount == DEFAULT_BACKUP_MOUNT
    assert _spec(backup_mount=None).resolved_backup_mount == DEFAULT_BACKUP_MOUNT


def test_only_sqlserver_carries_a_default_mount():
    """PostgreSQL and Oracle restores move files by other means; a mount there would be an empty
    directory on every lab host and nothing else."""
    assert ENGINE_META["mssql"].backup_mount == DEFAULT_BACKUP_MOUNT
    for engine in ("postgres", "mysql", "oracle"):
        assert ENGINE_META[engine].backup_mount == ""


def test_a_postgres_lab_compose_is_unchanged_by_this():
    compose = _compose(_spec(engine="postgres", version="18", host_port=5433))

    assert DEFAULT_BACKUP_MOUNT not in compose


def test_the_mount_line_does_not_disturb_the_rest_of_the_file():
    """The renderer has {% for %} but no {% if %}, so an absent mount must leave the surrounding
    YAML byte-identical rather than a stray blank line where the loop was."""
    with_mount = _compose(_spec())
    without = _compose(_spec(backup_mount=""))

    assert "\n\n    healthcheck:" not in with_mount
    assert "\n\n    healthcheck:" not in without
    assert without.count("healthcheck:") == 1


def test_run_cmd_exposes_one_privileged_path_instead_of_inlined_passwords():
    """Installing mssql-tools18 on the target VM needed root, and run-cmd had no way to ask for it
    — the only route was writing the sudo password into the script text, where it lands in the
    background-task row and in `ps`. `"sudo": true` routes through the same helper host-service
    and host-restart use: the password comes from the target's own configured credential and goes
    in on stdin."""
    import inspect

    from db_ops.common import cli, host_ops

    assert callable(host_ops.run_privileged)
    source = inspect.getsource(cli._run_cmd_command)
    assert 'request.get("sudo"' in source
    assert "host_ops.run_privileged" in source
