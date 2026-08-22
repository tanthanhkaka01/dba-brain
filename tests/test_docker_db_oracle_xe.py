"""Oracle 11g R2 as a lab container, through `create-db-docker --engine oracle-xe`.

The estate runs Oracle versions no current image ships — 8i behind the legacy bridge, 11g in
production — so a lab that can only start Oracle Database Free 23ai cannot reproduce anything the
old instances do. `gvenzl/oracle-xe:11` (11.2.0.2) is the only 11g R2 available as an image, and
it is x86-64 only: Oracle never shipped a Linux 32-bit build of 11.2 XE, so there is no 32-bit
option to add here or anywhere.

It is a **separate engine** rather than another tag of `oracle` because almost nothing about it
matches Free 23ai: different image, different service name, non-CDB, and no Data Guard. Sharing
one entry would put the wrong service name in every connection string the registry writes.
"""

from __future__ import annotations

import pytest

from db_ops.common import db_connect
from db_ops.sre.docker_db import healthcheck, templates
from db_ops.sre.docker_db.models import ENGINE_META, HA_SUPPORTED_ENGINES, DockerDbSpec
from db_ops.sre.docker_db.register_config import build_connection_entry


def _spec(**over):
    base = dict(name="ora11g_lab", engine="oracle-xe", version="11", mode="single",
                host_port=1521, password_env="ORA11G_LAB_PASSWORD")
    base.update(over)
    return DockerDbSpec(**base)


def test_an_oracle_xe_single_instance_validates():
    _spec().validate()


def test_it_serves_the_non_cdb_service_not_a_pluggable_database():
    """11g XE is a non-CDB: the service IS the database. Reusing Free's FREEPDB1 — or 18c+ XE's
    XEPDB1 — would put a service that does not exist into every connection string, and the
    failure arrives at first login rather than at provisioning."""
    meta = ENGINE_META["oracle-xe"]

    assert meta.image_repo == "gvenzl/oracle-xe"
    assert meta.database == "XE"
    assert ENGINE_META["oracle"].database == "FREEPDB1"


def test_the_connection_entry_points_at_the_xe_service():
    entry = build_connection_entry(_spec(), host="10.0.0.9", compose_path="/tmp/x.yml")

    assert entry["engine"] == "oracle-xe"
    assert entry["database"] == "XE"
    assert entry["port"] == 1521
    assert entry["username"] == "system"


def test_the_engine_name_still_reaches_the_oracle_driver():
    """The registry records the *engine* that provisioned the container. Anything reading that
    field as a db_type has to land on the same driver as any other Oracle — the distinction
    matters when creating the container, never when connecting to it."""
    assert db_connect.normalize_db_type("oracle-xe") == "oracle"


def test_ha_lab_is_refused_because_xe_has_no_data_guard():
    """Data Guard is an Enterprise feature and XE is the most cut-down edition there is. Free
    23ai supports it, which is why that engine has an ha-lab and this one must not pretend to."""
    with pytest.raises(ValueError, match="does not support --mode ha-lab"):
        _spec(mode="ha-lab").validate()

    assert "oracle-xe" not in HA_SUPPORTED_ENGINES


def test_the_health_probe_is_the_images_own_script():
    """Both gvenzl images ship healthcheck.sh. Falling through to the mssql sqlcmd branch — which
    is what an unlisted engine does — would report a healthy database as unhealthy forever."""
    assert healthcheck.native_probe_command("oracle-xe", "ora11g_lab") == [
        "docker", "exec", "ora11g_lab", "healthcheck.sh"]


def test_a_password_that_sqlplus_would_eat_is_refused():
    """The image sets the password through SQL*Plus, where `&` starts a substitution variable and
    `"` closes a quoted identifier. Both fail silently: the container reports healthy and only the
    first real login says ORA-01017."""
    assert ENGINE_META["oracle-xe"].forbidden_password_chars == '&"'


def test_the_compose_template_renders_for_a_single_instance():
    spec = _spec()
    rendered = templates.render(templates.load_template(spec.engine, spec.mode),
                                templates.build_context(spec))

    assert "gvenzl/oracle-xe:11" in rendered
    assert "ORACLE_PASSWORD: ${DB_PASSWORD}" in rendered
    assert "1521:1521" in rendered
    assert "/opt/oracle/oradata" in rendered
    # The password must never reach the committed compose file; it lives only in .env.
    assert "LabPass" not in rendered


def test_the_first_start_budget_fits_inside_the_callers():
    """`create-db-docker` is reachable from Telegram, whose poller SIGKILLs the process at its own
    timeout. A budget that does not fit turns a provisioning failure into a blunt "timed out"."""
    from db_ops.sre.docker_db.models import CALLER_BUDGET_SECONDS, PULL_AND_STARTUP_ALLOWANCE

    meta = ENGINE_META["oracle-xe"]

    assert meta.health_timeout + meta.post_start_timeout + PULL_AND_STARTUP_ALLOWANCE \
        <= CALLER_BUDGET_SECONDS
