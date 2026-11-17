"""A lab database container runs on the time zone it is given (0.40.0, 1.385).

A point in time is read in the source database's own clock - Oracle's SYSTIMESTAMP, PostgreSQL's
timezone set at initdb, SQL Server's GETDATE - and every lab ran on the image's UTC, so a restore from a
source off UTC had never been drilled. The operator chose to drill it on a second lab source in +07,
built by the tool that builds every lab: ``create-db-docker --timezone``, the container's ``TZ``.
"""

from __future__ import annotations

import pytest
import yaml

from dbabrain.common.docker_db import compose, templates
from dbabrain.lib.docker_db_spec import DockerDbSpec

ENGINES = [("postgres", "18", 5432), ("mysql", "8.4", 3306), ("mssql", "2022-latest", 1433),
           ("oracle", "23.26.2", 1521), ("oracle-xe", "21.3.0", 1521)]


def _spec(**over):
    values = dict(name="lab_tz", engine="postgres", version="18", host_port=5432, password_env="P", backup_mount="")
    values.update(over)
    return DockerDbSpec(**values)


def _compose(spec):
    return templates.render(templates.load_template(spec.engine, spec.mode), templates.build_context(spec))


@pytest.mark.parametrize("engine, version, port", ENGINES)
def test_each_engines_container_takes_the_stated_zone_as_its_tz(engine, version, port):
    spec = _spec(engine=engine, version=version, host_port=port, timezone="Asia/Ho_Chi_Minh")
    service = yaml.safe_load(_compose(spec))["services"]["lab_tz"]
    assert service["environment"]["TZ"] == "Asia/Ho_Chi_Minh"


@pytest.mark.parametrize("engine, version, port", ENGINES)
def test_no_zone_stated_leaves_the_images_own_clock(engine, version, port):
    service = yaml.safe_load(_compose(_spec(engine=engine, version=version, host_port=port)))["services"]["lab_tz"]
    assert "TZ" not in service["environment"]


def test_the_zone_is_recorded_with_the_connection():
    plan = compose.build_plan(_spec(timezone="Asia/Ho_Chi_Minh"), containers_dir="/srv/labs",
                              worker_host="192.0.2.18", password="x")
    assert plan.connection["timezone"] == "Asia/Ho_Chi_Minh"
    plain = compose.build_plan(_spec(), containers_dir="/srv/labs", worker_host="192.0.2.18", password="x")
    assert "timezone" not in plain.connection


@pytest.mark.parametrize("over, words", [
    ({"timezone": "+07:00"}, "IANA zone name"),
    ({"timezone": "Asia/Ho Chi Minh"}, "IANA zone name"),
    ({"timezone": "Asia/Ho_Chi_Minh; rm -rf /"}, "IANA zone name"),
    ({"mode": "ha-lab", "timezone": "Asia/Ho_Chi_Minh"}, "single instance"),
])
def test_a_zone_that_is_not_one_or_an_ha_lab_is_refused(over, words):
    with pytest.raises(ValueError, match=words):
        _spec(**over).validate()


@pytest.mark.parametrize("zone", ["UTC", "Asia/Ho_Chi_Minh", "America/Argentina/Buenos_Aires", "Etc/GMT+7"])
def test_a_real_zone_name_is_taken(zone):
    _spec(timezone=zone).validate()
