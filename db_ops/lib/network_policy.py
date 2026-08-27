"""Whether a host's container networks can take a monitored database off the map.

Docker's default address pool is 172.17.0.0/16 through 172.31.0.0/16. This estate routes real
databases inside that space, so on a host where nothing pins Docker's allocation, a bridge
eventually claims a range the host already needs. What breaks is the *host route table*: the
address stops leaving the machine. The database is healthy, reachable from every other machine,
and simply absent from that one worker — which surfaces as an ordinary connect timeout and tells
the reader nothing.

That has now cost three outages on the same instance (2026-08-05, 2026-08-14, 2026-08-26), and the
third one is why this module exists. The first two were answered by pinning: db_ops's own compose
network, then every generated lab compose file, then ``/etc/docker/daemon.json`` on the worker
host. Pinning is necessary and it held — but only on hosts where somebody applied it. When the
worker moved to a new VM the code-side pins travelled inside the image and the host-side one did
not, so ``docker0`` took 172.18.0.0/16 on a machine nobody had prepared, and the same SQL Server
disappeared for the third time.

So the rule here is not "pin things" — that is already done elsewhere. It is **detection**: given
what a host's networks actually are, say whether they collide, in one line, before a human spends
an afternoon on driver timeouts. The three findings answer three different questions:

``HIJACK``
    A container network contains a monitored address *right now*. This is the outage, already
    happening. It names the network, the address and the instance.

``OVERLAP``
    A container network overlaps a range the estate routes, but no monitored address sits inside
    it yet. The next instance added to that range vanishes on arrival.

``UNCONFINED``
    A container network sits outside every declared container range. Nothing is broken today and
    the range may be harmless — but Docker, not the operator, chose it, which means the host's
    pool is unpinned and the *next* network is a coin flip. This is the finding that would have
    named the new worker VM on the day it was built.

Pure values and rules (ORD 14): the caller collects the host's networks and the inventory's
addresses, this decides what they mean.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: A container network contains a monitored address. The outage, in progress.
HIJACK = "HIJACK"
#: A container network overlaps a routed range, but nothing monitored is inside it yet.
OVERLAP = "OVERLAP"
#: A container network is outside every declared container range — the host's pool is unpinned.
UNCONFINED = "UNCONFINED"

#: Worst first, so a caller can sort findings by how much they should interrupt someone.
FINDING_ORDER: tuple[str, ...] = (HIJACK, OVERLAP, UNCONFINED)

_NETWORK = ipaddress.IPv4Network | ipaddress.IPv6Network
_ADDRESS = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class RangeDeclaration:
    """One declared range from ``data/network_reservations.json``."""

    cidr: str
    owner: str
    note: str
    network: _NETWORK


@dataclass(frozen=True)
class NetworkReservations:
    """What the estate routes, and what the container runtime is allowed instead."""

    routed_ranges: tuple[RangeDeclaration, ...] = ()
    container_ranges: tuple[RangeDeclaration, ...] = ()

    def routed_match(self, network: _NETWORK) -> RangeDeclaration | None:
        """The first routed range *network* overlaps, or ``None``."""
        for declared in self.routed_ranges:
            if _overlaps(network, declared.network):
                return declared
        return None

    def is_confined(self, network: _NETWORK) -> bool:
        """Whether *network* sits wholly inside a declared container range.

        Subnet-of, not overlap: a bridge that straddles the boundary is half outside the range it
        was supposed to be confined to, and that half is the part that can collide.
        """
        return any(_subnet_of(network, declared.network) for declared in self.container_ranges)


@dataclass(frozen=True)
class HostNetwork:
    """One container network as the host reports it."""

    name: str
    subnet: str
    network: _NETWORK


@dataclass(frozen=True)
class MonitoredAddress:
    """One address the inventory expects this host to reach."""

    address: str
    label: str
    parsed: _ADDRESS


@dataclass(frozen=True)
class Finding:
    """One verdict about one container network."""

    kind: str
    network_name: str
    subnet: str
    #: The routed range that was overlapped — absent on ``UNCONFINED``.
    routed_cidr: str = ""
    routed_owner: str = ""
    #: The monitored addresses caught inside the subnet — populated only on ``HIJACK``.
    captured: tuple[MonitoredAddress, ...] = ()

    @property
    def severity(self) -> str:
        """``CRITICAL`` once something monitored is actually inside the subnet."""
        return "CRITICAL" if self.kind == HIJACK else "WARNING"

    def summary(self) -> str:
        """The one line the 2026-08-14 audit asked for and did not get."""
        if self.kind == HIJACK:
            caught = ", ".join(f"{item.address} ({item.label})" if item.label else item.address
                               for item in self.captured)
            return (f"HIJACK: container network {self.network_name} ({self.subnet}) contains "
                    f"{caught} — this host routes it into a local bridge, so the target is "
                    f"unreachable from here while healthy everywhere else")
        if self.kind == OVERLAP:
            return (f"OVERLAP: container network {self.network_name} ({self.subnet}) overlaps "
                    f"{self.routed_cidr} ({self.routed_owner}) — nothing monitored is inside it "
                    f"yet, so the next instance added there vanishes on arrival")
        return (f"UNCONFINED: container network {self.network_name} ({self.subnet}) is outside "
                f"every declared container range — Docker chose it, so this host's address pool "
                f"is not pinned and the next network is a coin flip")


def load_reservations(raw: Any) -> NetworkReservations:
    """Parse ``data/network_reservations.json``.

    A malformed or missing CIDR drops that one entry rather than raising: this check runs inside
    ``worker-status``, and a typo in a declaration must not take down the health report that would
    have told the operator about it.
    """
    if not isinstance(raw, Mapping):
        return NetworkReservations()
    return NetworkReservations(
        routed_ranges=_parse_ranges(raw.get("routed_ranges")),
        container_ranges=_parse_ranges(raw.get("container_ranges")),
    )


def parse_host_networks(entries: Iterable[Any]) -> tuple[HostNetwork, ...]:
    """Coerce ``(name, subnet)`` pairs or ``{"name":..., "subnet":...}`` mappings.

    Networks with no subnet (Docker's ``host`` and ``none``) drop out — they allocate nothing and
    so can collide with nothing.
    """
    parsed: list[HostNetwork] = []
    for entry in entries or ():
        name, subnet = _name_and_subnet(entry)
        network = _coerce_network(subnet)
        if network is None:
            continue
        parsed.append(HostNetwork(name=name, subnet=str(network), network=network))
    return tuple(parsed)


def parse_monitored_addresses(entries: Iterable[Any]) -> tuple[MonitoredAddress, ...]:
    """Coerce inventory rows to addresses, keeping a label for the message.

    A row whose ``ip`` is a hostname rather than an address drops out. Resolving it is the
    caller's business and not something a pure rule should reach the network to do.
    """
    parsed: list[MonitoredAddress] = []
    seen: set[str] = set()
    for entry in entries or ():
        address, label = _address_and_label(entry)
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError:
            continue
        if address in seen:
            continue
        seen.add(address)
        parsed.append(MonitoredAddress(address=address, label=label, parsed=resolved))
    return tuple(parsed)


def evaluate_host(
    *,
    host_networks: Sequence[HostNetwork],
    monitored: Sequence[MonitoredAddress],
    reservations: NetworkReservations,
) -> tuple[Finding, ...]:
    """Every finding about *host_networks*, worst first.

    One network yields at most one finding, and the strongest one wins: a bridge that is both
    unconfined and currently swallowing a database is reported as the hijack, because that is the
    sentence the operator has to act on.
    """
    findings: list[Finding] = []
    for host_network in host_networks:
        captured = tuple(item for item in monitored if item.parsed in host_network.network)
        routed = reservations.routed_match(host_network.network)
        if captured:
            findings.append(Finding(
                kind=HIJACK,
                network_name=host_network.name,
                subnet=host_network.subnet,
                routed_cidr=routed.cidr if routed else "",
                routed_owner=routed.owner if routed else "",
                captured=captured,
            ))
        elif routed is not None:
            findings.append(Finding(
                kind=OVERLAP,
                network_name=host_network.name,
                subnet=host_network.subnet,
                routed_cidr=routed.cidr,
                routed_owner=routed.owner,
            ))
        elif not reservations.is_confined(host_network.network):
            findings.append(Finding(
                kind=UNCONFINED,
                network_name=host_network.name,
                subnet=host_network.subnet,
            ))
    findings.sort(key=lambda item: (FINDING_ORDER.index(item.kind), item.network_name))
    return tuple(findings)


def _parse_ranges(raw: Any) -> tuple[RangeDeclaration, ...]:
    declared: list[RangeDeclaration] = []
    for entry in raw or ():
        if not isinstance(entry, Mapping):
            continue
        network = _coerce_network(entry.get("cidr"))
        if network is None:
            continue
        declared.append(RangeDeclaration(
            cidr=str(network),
            owner=str(entry.get("owner") or "").strip(),
            note=str(entry.get("note") or "").strip(),
            network=network,
        ))
    return tuple(declared)


def _coerce_network(value: Any) -> _NETWORK | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        # strict=False so a declaration written as a host address with a prefix
        # ("172.30.0.1/24", the way `bip` states it) is read as the network it names.
        return ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None


def _overlaps(left: _NETWORK, right: _NETWORK) -> bool:
    return left.version == right.version and left.overlaps(right)


def _subnet_of(inner: _NETWORK, outer: _NETWORK) -> bool:
    return inner.version == outer.version and inner.subnet_of(outer)  # type: ignore[arg-type]


def _name_and_subnet(entry: Any) -> tuple[str, str]:
    if isinstance(entry, Mapping):
        return str(entry.get("name") or "").strip(), str(entry.get("subnet") or "").strip()
    name, subnet = entry
    return str(name or "").strip(), str(subnet or "").strip()


def _address_and_label(entry: Any) -> tuple[str, str]:
    if isinstance(entry, Mapping):
        address = str(entry.get("ip") or entry.get("address") or "").strip()
        label = str(entry.get("server_id") or entry.get("label") or "").strip()
        return address, label
    if isinstance(entry, str):
        return entry.strip(), ""
    address, label = entry
    return str(address or "").strip(), str(label or "").strip()
