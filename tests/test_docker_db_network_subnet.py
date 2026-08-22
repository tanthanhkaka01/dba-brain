"""Every lab compose file pins its own network — Docker's address pool must never choose it.

Twice a lab bridge has taken a production database off the map. Docker's default pool hands out
172.17.0.0/16 … 172.31.0.0/16, and this estate routes real hosts inside that space: on 2026-08-05
the db_ops network itself was given 172.18.0.0/16 and the worker answered "No route to host" for
the SQL Server at 192.0.2.41; on 2026-08-14 `ora11g_lab` was created, took the same /16, and the
same instance vanished for two hours — every metric on it failed to connect while the database was
perfectly healthy and reachable from everywhere else.

The collision is decided in the *host* route table, so pinning one project fixes nothing: after the
first incident db_ops pinned its own network to 172.30.240.0/24, and the second incident happened
anyway, through a different project. The rule that holds is the one these tests enforce — every
generated compose file names its own subnet, in a range nothing in the estate routes.
"""

from __future__ import annotations

import pytest
import yaml

from db_ops.sre.docker_db import templates
from db_ops.sre.docker_db.models import (
    LAB_NETWORK_FIRST_OCTET,
    LAB_NETWORK_LAST_OCTET,
    LAB_NETWORK_PREFIX,
    DockerDbSpec,
    lab_network_subnet,
)


ALL_TEMPLATES = [
    ("postgres", "single"),
    ("postgres", "ha-lab"),
    ("mysql", "single"),
    ("mysql", "ha-lab"),
    ("mssql", "single"),
    ("mssql", "ha-lab"),
    ("oracle", "single"),
    ("oracle", "ha-lab"),
    ("oracle-xe", "single"),
]

# The ranges Docker's default pool would otherwise hand out, and that this estate uses for real
# hosts. 172.18.0.0/16 is the one that actually caused both outages.
DOCKER_DEFAULT_POOL_START = 17
DOCKER_DEFAULT_POOL_END = 29


def _spec(name="lab1", engine="postgres", mode="single", **overrides):
    values = {
        "name": name,
        "engine": engine,
        "version": "16",
        "mode": mode,
        "replicas": 1 if (engine == "oracle" and mode == "ha-lab") else 2,
        "host_port": 5433,
        "password_env": "PW",
    }
    values.update(overrides)
    return DockerDbSpec(**values)


@pytest.mark.parametrize("engine,mode", ALL_TEMPLATES)
def test_every_generated_compose_file_pins_its_network_subnet(engine, mode):
    spec = _spec(engine=engine, mode=mode)

    rendered = templates.render(templates.load_template(engine, mode), templates.build_context(spec))
    parsed = yaml.safe_load(rendered)

    config = parsed["networks"]["default"]["ipam"]["config"]
    assert config == [{"subnet": spec.resolved_network_subnet}]


@pytest.mark.parametrize("engine,mode", ALL_TEMPLATES)
def test_no_generated_subnet_lands_in_the_range_docker_would_have_chosen(engine, mode):
    """The whole point: the pinned subnet must be outside 172.17-172.29, where the estate lives."""
    spec = _spec(engine=engine, mode=mode)

    second_octet = int(spec.resolved_network_subnet.split(".")[1])

    assert not DOCKER_DEFAULT_POOL_START <= second_octet <= DOCKER_DEFAULT_POOL_END


def test_the_subnet_is_derived_from_the_name_so_a_re_provision_keeps_it():
    assert lab_network_subnet("ora11g_lab") == lab_network_subnet("ora11g_lab")
    assert lab_network_subnet("ora11g_lab") != lab_network_subnet("pg_ha_01")


def test_derived_subnets_stay_inside_the_reserved_lab_window():
    """Below .240, which is the db_ops runtime network, and above .0, which is docker0's bip."""
    for name in ("a", "pg_ha_01", "MSSQL_LAB_HA_01", "ora_dg_lab", "lab-with-a-very-long-name"):
        subnet = lab_network_subnet(name)
        prefix, third = subnet.rsplit(".", 2)[0], int(subnet.split(".")[2])

        assert prefix == LAB_NETWORK_PREFIX
        assert LAB_NETWORK_FIRST_OCTET <= third <= LAB_NETWORK_LAST_OCTET
        assert third not in {0, 240}
        assert subnet.endswith(".0/24")


def test_an_operator_can_override_the_subnet_to_settle_a_collision():
    spec = _spec(network_subnet="172.30.42.0/24")

    rendered = templates.render(templates.load_template("postgres", "single"), templates.build_context(spec))

    assert yaml.safe_load(rendered)["networks"]["default"]["ipam"]["config"] == [{"subnet": "172.30.42.0/24"}]


def test_a_subnet_that_is_not_a_cidr_is_refused_before_anything_is_written():
    with pytest.raises(ValueError, match="network-subnet"):
        _spec(network_subnet="172.30.42").validate()


def test_an_unset_subnet_is_never_rendered_blank():
    """A blank `subnet:` key is not "use the default" to compose — it is a parse error, and the
    failure mode this guards is worse: silently falling back to an auto-allocated range."""
    spec = _spec(network_subnet="")

    assert spec.resolved_network_subnet
    assert "subnet: \n" not in templates.render(
        templates.load_template("postgres", "single"), templates.build_context(spec)
    )
