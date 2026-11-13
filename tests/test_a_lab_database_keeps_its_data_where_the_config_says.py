"""A lab PostgreSQL keeps its data in the host folder the request states, run as the uid it states (0.37.0).

Why it matters: the operator, 2026-11-10 - *put the restore drill's PostgreSQL on /mnt/nas; nothing
hard-coded, a config field says it*. The repository VM's share maps every user to uid 1024 and
PostgreSQL refuses a data directory it does not own, so the container runs as that uid - measured:
initdb and a start on the share work as 1024:100 and are refused as the image's own user. Both are
stated (``data_host_dir``, ``run_as``), recorded with the connection, and absent leaves every lab
exactly as before: a named volume, the image's user.
"""

from __future__ import annotations

import pytest

from dbabrain.common.docker_db import compose, templates
from dbabrain.lib.docker_db_spec import DockerDbSpec


def _spec(**over):
    values = dict(name="dbabrain", engine="postgres", version="18", host_port=5432, password_env="P",
                  backup_mount="")
    values.update(over)
    return DockerDbSpec(**values)


def _compose(spec):
    return templates.render(templates.load_template(spec.engine, spec.mode), templates.build_context(spec))


def test_the_data_goes_to_the_stated_host_folder_run_as_the_stated_uid():
    text = _compose(_spec(data_host_dir="/srv/share/pgdata", run_as="1024:100"))
    assert 'user: "1024:100"' in text
    assert '- "/srv/share/pgdata:/var/lib/postgresql"' in text
    assert "dbabrain_data" not in text, "no named volume when a host folder is stated"


def test_nothing_stated_is_the_named_volume_and_the_images_user_as_before():
    text = _compose(_spec())
    assert "- dbabrain_data:/var/lib/postgresql" in text and "volumes:\n  dbabrain_data:" in text
    assert "user:" not in text


def test_the_folder_is_made_and_both_are_recorded_with_the_connection():
    plan = compose.build_plan(_spec(data_host_dir="/srv/share/pgdata", run_as="1024:100"),
                              containers_dir="/srv/labs", worker_host="192.0.2.18", password="x")
    assert plan.data_dirs == ["/srv/share/pgdata"]
    assert plan.connection["data_host_dir"] == "/srv/share/pgdata" and plan.connection["run_as"] == "1024:100"


@pytest.mark.parametrize("over, words", [
    ({"data_host_dir": "relative/pgdata"}, "absolute folder"),
    ({"data_host_dir": "/"}, "absolute folder"),
    ({"data_host_dir": "/srv/../etc"}, "absolute folder"),
    ({"run_as": "postgres"}, "uid:gid"),
    ({"run_as": "1024"}, "uid:gid"),
    ({"engine": "mysql", "version": "8.4", "data_host_dir": "/srv/x"}, "PostgreSQL single instance only"),
    ({"mode": "ha-lab", "data_host_dir": "/srv/x"}, "PostgreSQL single instance only"),
])
def test_a_folder_or_uid_that_is_not_one_is_refused(over, words):
    with pytest.raises(ValueError, match=words):
        _spec(**over).validate()


def test_a_lab_run_as_a_stated_uid_gets_its_staging_mount_without_a_chown_it_could_not_do():
    """The image's entrypoint chowns the backup folder as root; run as 1024 it would stop there."""
    text = _compose(_spec(data_host_dir="/srv/share/pgdata", run_as="1024:100", backup_mount="/srv/share/stage"))
    assert '- "/srv/share/stage:/srv/share/stage"' in text
    assert "chown" not in text and "archive_command" not in text
