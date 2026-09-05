"""A container network that claims a routed range takes a database off one host and nowhere else.

This is the failure these tests defend against, and it has happened three times to the same SQL
Server: 2026-08-05 (the db_ops compose network took 172.20.0.0/16), 2026-08-14 (a lab project took
it), and 2026-08-26 (``docker0`` took it on a newly built worker VM). Each time the instance was
healthy and reachable from every other machine; each time the worker reported an ordinary connect
timeout, because that is exactly what a dropped SYN looks like from a database driver.

Pinning was the answer to the first two, and it works — but only where somebody applied it, which
is why the third happened on a new host. What was missing every time was *detection*: something
that reads the host's own networks and says which one is doing it. That is
``db_ops.lib.network_policy``, and these tests are what make it trustworthy, since a check nobody
asserts on is indistinguishable from one that quietly stopped working.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import pytest

from db_ops.control import worker_status
from db_ops.lib import network_policy
from db_ops.lib.network_policy import HIJACK, OVERLAP, UNCONFINED
from db_ops.lib.paths import DEFAULT_DATA_DIR
from db_ops.sre.docker_db.models import LAB_NETWORK_PREFIX, lab_network_subnet


#: The estate's own declaration. ``data/*.json`` is private and does **not** ship, so in the public
#: tree this is absent and the example beside it is all a reader gets — which is why the tests below
#: assert against whichever of the two is present rather than hard-coding the private one. A test
#: that reads a file the published copy does not contain fails on a correct install; that has
#: already happened once here (`audits/20260822_audit_thin_slice_first_run.md`).
RESERVATIONS_FILE = DEFAULT_DATA_DIR / "network_reservations.json"
EXAMPLE_FILE = DEFAULT_DATA_DIR / "network_reservations.example.json"
INVENTORY_FILE = DEFAULT_DATA_DIR / "db_instances.json"
HOST_DAEMON_FILE = Path(__file__).resolve().parents[1] / "db_ops" / "sre" / "host_config" / "docker-daemon.json"

#: Both declarations where both exist. The example ships and the estate's does not, so the rules
#: below are enforced on whichever the tree actually has.
SHIPPED_DECLARATIONS = [path for path in (EXAMPLE_FILE, RESERVATIONS_FILE) if path.exists()]


def _declaration(path: Path):
    return network_policy.load_reservations(json.loads(path.read_text(encoding="utf-8")))


def _reservations(routed=(), container=()):
    return network_policy.load_reservations({
        "routed_ranges": [{"cidr": cidr, "owner": "estate", "note": ""} for cidr in routed],
        "container_ranges": [{"cidr": cidr, "owner": "docker", "note": ""} for cidr in container],
    })


def _evaluate(networks, addresses, *, routed=(), container=()):
    return network_policy.evaluate_host(
        host_networks=network_policy.parse_host_networks(networks),
        monitored=network_policy.parse_monitored_addresses(addresses),
        reservations=_reservations(routed=routed, container=container),
    )


# --------------------------------------------------------------------------- #
# The outage itself
# --------------------------------------------------------------------------- #

def test_a_bridge_holding_a_monitored_address_is_reported_as_a_hijack():
    """The 2026-08-26 outage, reduced: docker0 on 172.20.0.0/16 with one database at 172.20.99.10."""
    findings = _evaluate(
        [("bridge", "172.20.0.0/16")],
        [{"ip": "172.20.99.10", "server_id": "DB-172-20-99-10"}],
        routed=["172.20.99.0/24"],
        container=["172.30.0.0/16"],
    )

    assert [item.kind for item in findings] == [HIJACK]
    assert findings[0].network_name == "bridge"
    assert findings[0].captured[0].address == "172.20.99.10"


def test_the_hijack_line_names_the_network_the_address_and_the_instance():
    """The whole point is that one line replaces an afternoon of driver debugging."""
    findings = _evaluate(
        [("bridge", "172.20.0.0/16")],
        [{"ip": "172.20.99.10", "server_id": "DB-172-20-99-10"}],
        routed=["172.20.99.0/24"],
    )

    summary = findings[0].summary()
    assert "bridge" in summary
    assert "172.20.0.0/16" in summary
    assert "172.20.99.10" in summary
    assert "DB-172-20-99-10" in summary


def test_a_hijack_is_critical_and_everything_else_is_a_warning():
    hijack = _evaluate([("bridge", "172.20.0.0/16")], [{"ip": "172.20.99.10", "server_id": "s"}],
                       routed=["172.20.99.0/24"])
    overlap = _evaluate([("bridge", "172.20.0.0/16")], [], routed=["172.20.99.0/24"])

    assert hijack[0].severity == "CRITICAL"
    assert overlap[0].severity == "WARNING"


def test_every_captured_address_in_one_subnet_is_named_not_just_the_first():
    """172.20.0.0/16 swallowed both 99.10 and 99.20; a report naming one of them hides the other."""
    findings = _evaluate(
        [("bridge", "172.20.0.0/16")],
        [{"ip": "172.20.99.10", "server_id": "first-db"}, {"ip": "172.20.99.20", "server_id": "second-db"}],
        routed=["172.20.99.0/24"],
    )

    assert {item.address for item in findings[0].captured} == {"172.20.99.10", "172.20.99.20"}


# --------------------------------------------------------------------------- #
# The two findings that fire before anything breaks
# --------------------------------------------------------------------------- #

def test_a_routed_range_with_nothing_in_it_yet_is_an_overlap_not_a_hijack():
    """Nothing is down, but the next instance added to that range would vanish on arrival."""
    findings = _evaluate([("lab", "172.20.0.0/16")], [{"ip": "10.0.0.5", "server_id": "elsewhere"}],
                         routed=["172.20.99.0/24"])

    assert [item.kind for item in findings] == [OVERLAP]
    assert findings[0].routed_cidr == "172.20.99.0/24"


def test_a_network_outside_every_declared_container_range_is_unconfined():
    """The finding that would have named the new worker VM on the day it was built.

    172.19.0.0/16 collided with nothing in the inventory, so no other check would have spoken —
    but Docker chose it, which is the evidence that the host's pool was never pinned.
    """
    findings = _evaluate([("mssql25_default", "172.19.0.0/16")], [], container=["172.30.0.0/16"])

    assert [item.kind for item in findings] == [UNCONFINED]


def test_a_network_inside_a_declared_container_range_is_not_reported():
    findings = _evaluate([("db_ops_default", "172.30.240.0/24")], [{"ip": "172.21.100.10", "server_id": "s"}],
                         routed=["172.21.0.0/16"], container=["172.30.0.0/16"])

    assert findings == ()


def test_a_network_straddling_the_container_range_boundary_is_still_unconfined():
    """Subnet-of, not overlap: the half that sticks out is the half that can collide."""
    findings = _evaluate([("wide", "172.30.0.0/15")], [], container=["172.30.0.0/16"])

    assert [item.kind for item in findings] == [UNCONFINED]


# --------------------------------------------------------------------------- #
# One network, one finding, strongest first
# --------------------------------------------------------------------------- #

def test_a_network_that_is_both_unconfined_and_hijacking_is_reported_as_the_hijack():
    """One line per network, and it has to be the line the operator must act on."""
    findings = _evaluate([("bridge", "172.20.0.0/16")], [{"ip": "172.20.99.10", "server_id": "s"}],
                         routed=["172.20.99.0/24"], container=["172.30.0.0/16"])

    assert [item.kind for item in findings] == [HIJACK]


def test_findings_are_ordered_worst_first():
    findings = _evaluate(
        [("unconfined", "172.19.0.0/16"), ("overlapping", "172.21.0.0/16"), ("hijacking", "172.20.0.0/16")],
        [{"ip": "172.20.99.10", "server_id": "s"}],
        routed=["172.21.0.0/16", "172.20.99.0/24"],
        container=["172.30.0.0/16"],
    )

    assert [item.kind for item in findings] == [HIJACK, OVERLAP, UNCONFINED]


# --------------------------------------------------------------------------- #
# Parsing — a health check must not fall over on the config it is reading
# --------------------------------------------------------------------------- #

def test_networks_that_allocate_nothing_are_ignored():
    """Docker's `host` and `none` report no subnet and can collide with nothing."""
    parsed = network_policy.parse_host_networks([("host", ""), ("none", ""), ("bridge", "172.30.0.0/24")])

    assert [item.name for item in parsed] == ["bridge"]


def test_a_network_declaring_several_subnets_is_checked_on_each_one():
    """Only the second subnet hijacks, so stopping at the first would miss it entirely."""
    findings = _evaluate([("dual", "172.30.9.0/24"), ("dual", "172.20.0.0/16")],
                         [{"ip": "172.20.99.10", "server_id": "s"}],
                         routed=["172.20.99.0/24"], container=["172.30.0.0/16"])

    assert [item.kind for item in findings] == [HIJACK]
    assert findings[0].subnet == "172.20.0.0/16"


def test_an_inventory_row_whose_ip_is_a_hostname_is_skipped_not_fatal():
    """Resolving a name is the caller's business; a pure rule must not reach the network."""
    parsed = network_policy.parse_monitored_addresses(
        [{"ip": "db-prod-01", "server_id": "s"}, {"ip": "172.20.99.10", "server_id": "t"}])

    assert [item.address for item in parsed] == ["172.20.99.10"]


def test_a_repeated_address_is_counted_once():
    """Several instances share a host; the report should name the address once, not per instance."""
    parsed = network_policy.parse_monitored_addresses(
        [{"ip": "172.21.187.10", "server_id": "a"}, {"ip": "172.21.187.10", "server_id": "b"}])

    assert len(parsed) == 1


def test_a_malformed_cidr_drops_that_entry_and_keeps_the_rest():
    """A typo in a declaration must not take down the report that would have named it."""
    reservations = network_policy.load_reservations({
        "routed_ranges": [{"cidr": "172.20.99.0/99"}, {"cidr": "172.21.0.0/16"}],
    })

    assert [item.cidr for item in reservations.routed_ranges] == ["172.21.0.0/16"]


def test_a_range_written_as_a_host_address_reads_as_the_network_it_names():
    """`bip` states 172.30.0.1/24; that declares the 172.30.0.0/24 bridge."""
    reservations = network_policy.load_reservations({"container_ranges": [{"cidr": "172.30.0.1/24"}]})

    assert reservations.container_ranges[0].cidr == "172.30.0.0/24"


@pytest.mark.parametrize("raw", [None, {}, [], "not a mapping", {"routed_ranges": None}])
def test_a_missing_or_unusable_document_yields_no_ranges_rather_than_raising(raw):
    assert network_policy.load_reservations(raw).routed_ranges == ()


def test_an_ipv6_container_network_does_not_match_an_ipv4_range():
    findings = _evaluate([("v6", "fd00::/64")], [{"ip": "172.20.99.10", "server_id": "s"}],
                         routed=["172.20.99.0/24"], container=["172.30.0.0/16"])

    assert [item.kind for item in findings] == [UNCONFINED]


# --------------------------------------------------------------------------- #
# The declaration and the host file it mirrors must agree
# --------------------------------------------------------------------------- #

def test_the_example_declaration_ships_and_declares_both_halves():
    """``data/*.json`` is private; the example is the installer's whole introduction to this."""
    assert EXAMPLE_FILE.exists(), "a check with no example is inert on a fresh install"
    reservations = _declaration(EXAMPLE_FILE)

    assert reservations.routed_ranges, "routed_ranges is what the check defends"
    assert reservations.container_ranges, "container_ranges is what it allows instead"


@pytest.mark.parametrize("path", SHIPPED_DECLARATIONS, ids=lambda path: path.name)
def test_no_declared_container_range_overlaps_a_declared_routed_range(path):
    """The two halves are the whole rule; if they intersect, the file permits its own outage."""
    reservations = _declaration(path)

    for container in reservations.container_ranges:
        for routed in reservations.routed_ranges:
            assert not container.network.overlaps(routed.network), \
                f"{container.cidr} ({container.owner}) overlaps {routed.cidr} ({routed.owner})"


@pytest.mark.parametrize("path", SHIPPED_DECLARATIONS, ids=lambda path: path.name)
def test_the_host_daemon_file_stays_inside_the_declared_container_ranges(path):
    """docker-daemon.json is what an operator applies; this file is what db_ops checks against.

    They are two statements of one decision, kept in different places for different readers, and
    nothing but this test stops them drifting apart.
    """
    reservations = _declaration(path)
    daemon = json.loads(HOST_DAEMON_FILE.read_text(encoding="utf-8"))

    declared = [daemon["bip"]] + [pool["base"] for pool in daemon["default-address-pools"]]
    for cidr in declared:
        network = ipaddress.ip_network(cidr, strict=False)
        assert reservations.is_confined(network), f"docker-daemon.json allocates {cidr}, undeclared"


@pytest.mark.parametrize("path", SHIPPED_DECLARATIONS, ids=lambda path: path.name)
def test_every_derived_lab_subnet_stays_inside_the_declared_container_ranges(path):
    """The generator's pins and this declaration are a third pair that must not drift."""
    reservations = _declaration(path)

    for name in ("pg_ha_01", "ora11g_lab", "MSSQL_LAB_HA_01", "ora_dg_lab", "scratch"):
        subnet = ipaddress.ip_network(lab_network_subnet(name))
        assert str(subnet).startswith(LAB_NETWORK_PREFIX)
        assert reservations.is_confined(subnet), f"lab {name} would get {subnet}, undeclared"


@pytest.mark.skipif(not (RESERVATIONS_FILE.exists() and INVENTORY_FILE.exists()),
                    reason="estate declaration and inventory are private; neither ships")
def test_every_monitored_address_in_the_inventory_sits_in_a_declared_routed_range():
    """A routed range nobody declared is a range this check cannot defend.

    This is the test that fails when the estate grows a subnet and the declaration is not updated
    in the same pass — which is the only way the guard silently stops covering something.

    It reads two private files, so it does not run in the published tree. That is the right
    behaviour and not a gap: the rule it enforces is "your declaration covers *your* inventory",
    which is a statement about an estate, and the published copy has neither.
    """
    reservations = _declaration(RESERVATIONS_FILE)
    inventory = json.loads(INVENTORY_FILE.read_text(encoding="utf-8"))["db_instances"]
    docker_pool = ipaddress.ip_network("172.16.0.0/12")

    undeclared = set()
    for item in network_policy.parse_monitored_addresses(inventory):
        # Only addresses inside Docker's default pool can ever be claimed by a bridge; a public
        # address or a 192.168.x host is out of reach of this failure and needs no declaration.
        if item.parsed.version != 4 or item.parsed not in docker_pool:
            continue
        if reservations.routed_match(ipaddress.ip_network(f"{item.address}/32")) is None:
            undeclared.add(item.address)

    assert not undeclared, f"inside Docker's pool but undeclared in network_reservations.json: {sorted(undeclared)}"


# --------------------------------------------------------------------------- #
# What worker-status actually prints
# --------------------------------------------------------------------------- #

def _data_dir(tmp_path: Path, *, inventory) -> Path:
    (tmp_path / "network_reservations.json").write_text(json.dumps({
        "routed_ranges": [{"cidr": "172.20.99.0/24", "owner": "estate LAN", "note": ""}],
        "container_ranges": [{"cidr": "172.30.0.0/16", "owner": "db_ops", "note": ""}],
    }), encoding="utf-8")
    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": inventory}), encoding="utf-8")
    return tmp_path


def test_worker_status_names_the_hijacking_network_from_a_captured_listing(tmp_path):
    """The exact `docker network ls` output from the worker on 2026-08-26."""
    listing = (
        "bridge\t172.20.0.0/16 \n"
        "db_ops_default\t172.30.240.0/24 \n"
        "host\t\n"
        "none\t\n"
    )
    data_dir = _data_dir(tmp_path, inventory=[{"ip": "172.20.99.10", "server_id": "DB-172-20-99-10"}])

    lines = worker_status.report_network_reservations(listing, data_dir=data_dir)

    assert any("HIJACK" in line and "172.20.99.10" in line for line in lines)
    assert any("host_config" in line for line in lines), "the report must say where the fix lives"


def test_worker_status_reports_a_clean_host_in_one_line(tmp_path):
    """After the fix: docker0 on 172.30.0.0/24 and nothing to say."""
    listing = "bridge\t172.30.0.0/24 \ndb_ops_default\t172.30.240.0/24 \nhost\t\n"
    data_dir = _data_dir(tmp_path, inventory=[{"ip": "172.20.99.10", "server_id": "DB-172-20-99-10"}])

    lines = worker_status.report_network_reservations(listing, data_dir=data_dir)

    assert lines == ["OK — 2 container network(s), none overlapping 1 monitored address(es)."]


def test_worker_status_says_so_when_the_declaration_is_missing(tmp_path):
    """"Not configured" is a state, not a failure — and it must not read as "all clear"."""
    lines = worker_status.report_network_reservations("bridge\t172.20.0.0/16 \n", data_dir=tmp_path)

    assert lines == ["(data/network_reservations.json declares no ranges — nothing to check against)"]


def test_worker_status_survives_an_unreadable_inventory(tmp_path):
    """A broken config here costs the network section, not the run-status report."""
    data_dir = _data_dir(tmp_path, inventory=[])
    (data_dir / "db_instances.json").write_text("{ not json", encoding="utf-8")

    lines = worker_status.report_network_reservations("bridge\t172.19.0.0/16 \n", data_dir=data_dir)

    assert any("UNCONFINED" in line for line in lines)


def test_worker_status_reports_nothing_to_check_when_docker_returned_nothing(tmp_path):
    data_dir = _data_dir(tmp_path, inventory=[{"ip": "172.20.99.10", "server_id": "s"}])

    assert worker_status.report_network_reservations("", data_dir=data_dir) == ["(no container networks reported)"]
