"""What is a machine actually listening on — and what that rules in or out.

Written on 2026-08-19 because the same question had been answered three times by three throwaway
socket loops, each of which took its port list and its interpretation away with it. The standing
rule names this exact case: *"probing what a host listens on"* belongs in
``common`` with a CLI command, because a scratch script answers once and the next person rewrites
it and gets a different answer.

The answer that matters is rarely "is 5985 open". It is **what can db_ops do with this host**, and
the three possible verdicts lead to three different follow-ups:

* ``manageable`` — SSH or WinRM answers, so a command can be run and ``check-secret`` can prove
  the login.
* ``interactive_only`` — RDP answers and neither management port does. The host is up and is
  administered by hand. ``192.0.2.235`` and ``.236`` are the two here: Windows Server 2003 ships
  no WinRM and cannot run the OpenSSH server, so this is not a misconfiguration to fix but a fact
  about the OS, and it is why both carry ``cmd_access: null`` and nine disabled ``OS_*`` codes.
* ``unreachable`` — nothing answered at all. ``192.0.2.136`` and ``.211`` are in this state:
  every port closed, which is what a decommissioned host looks like from here.

The distinction between the last two is the one a scratch script never draws, and it is the
difference between "the host is gone" and "there is no way in from a script".

**A closed port and a filtered one are the same answer here** and deliberately so: this reports
reachability, not a firewall audit. What it *can* tell apart is ``refused`` (the machine answered
"nothing is listening") from ``timeout`` (nothing answered at all), because on a host that is
otherwise up, a refusal means the service is off and a timeout means a filter — see the
``.235``/``.236`` finding, where 5985 was refused rather than dropped and that was the proof the
box itself was reachable.
"""

from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from db_ops.lib.cmd_access import PLATFORM_WINDOWS
from db_ops.lib.coerce import as_optional_int
from db_ops.lib.target_profile import (
    SOURCE_CONFIG, TargetProfile, windows_management_transport_available,
)

__all__ = [
    "DEFAULT_PORTS",
    "HostProbeError",
    "PORT_NAMES",
    "probe",
    "probe_port",
]


class HostProbeError(RuntimeError):
    """The probe could not be set up — a bad request or an unknown target."""


#: What each port means, so the answer reads as capabilities rather than as numbers. Only ports
#: this tool actually decides something from; a caller wanting others passes them explicitly.
PORT_NAMES: dict[int, str] = {
    22: "ssh",
    135: "msrpc",
    139: "netbios",
    445: "smb",
    1433: "sqlserver",
    1521: "oracle",
    3306: "mysql",
    3389: "rdp",
    5432: "postgresql",
    5985: "winrm",
    5986: "winrm-ssl",
}

#: The default sweep: both management transports, RDP (which separates "administered by hand" from
#: "gone"), the RPC/SMB pair that says a Windows box is alive even without WinRM, and the four
#: database ports. Small on purpose — this is a diagnosis, not a port scanner.
DEFAULT_PORTS: tuple[int, ...] = (22, 135, 445, 1433, 1521, 3306, 3389, 5432, 5985, 5986)

#: Ports that mean "a script can get in".
MANAGEMENT_PORTS = (22, 5985, 5986)

_MAX_PARALLEL = 16


def probe_port(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    """One port, with *why* it is not open when it is not.

    ``refused`` and ``timeout`` are kept apart because they mean opposite things about the machine:
    a refusal is the host answering that nothing listens there, a timeout is nothing answering at
    all. On ``192.0.2.236`` that distinction was the evidence — 5985 refused while 3389 accepted
    proved the box was up and simply has no WinRM, rather than being firewalled or down.
    """
    started = time.monotonic()
    result: dict[str, Any] = {"port": int(port), "service": PORT_NAMES.get(int(port), "")}
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            result.update(open=True, state="open")
    except socket.timeout:
        result.update(open=False, state="timeout")
    except ConnectionRefusedError:
        result.update(open=False, state="refused")
    except OSError as exc:  # noqa: BLE001 - unresolvable/unreachable are one answer: not open.
        result.update(open=False, state="error", detail=str(exc)[:120])
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


def probe(request: dict[str, Any], *, instance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Probe one host and say what db_ops can do with it.

    The request is a JSON object, like every other command here::

        {"host": "192.0.2.236",         // the machine; nothing is read to find it
         "os": "Windows Server 2003",     // optional, and it changes the verdict — see _verdict
         "ports": [22, 5985, 3389],       // default: DEFAULT_PORTS
         "timeout_seconds": 3}

    **This module reads no file.** ``instance`` is the already-resolved ``db_instances.json``
    record when the caller had a ``target`` to resolve — ``common/cli.py`` does that, because
    resolving config before handing a JSON object down is the composition root's job and not this
    one's (``docs/13_common.md`` rule 3). A caller that knows the ip passes it and this stays a
    pure function of its arguments.
    """
    if not isinstance(request, dict):
        raise HostProbeError("request must be a JSON object.")

    instance = instance or {}
    host = str(request.get("host") or instance.get("ip") or "").strip()
    server_id = (
        str(request.get("target") or "").strip()
        or str(instance.get("server_id") or "").strip()
        or host
    )
    # The request's own facts win over the record's, the same precedence everything else here uses.
    profile = TargetProfile.from_json(request).merge(
        TargetProfile.from_json(instance, source=SOURCE_CONFIG)
    )
    if not host:
        raise HostProbeError('give a "host" (an ip or hostname) — or a "target" the caller resolves.')

    ports = request.get("ports") or DEFAULT_PORTS
    if not isinstance(ports, (list, tuple)):
        raise HostProbeError('"ports" must be a list of port numbers.')
    resolved_ports = []
    for value in ports:
        number = as_optional_int(value)
        if number is None or not 1 <= number <= 65535:
            raise HostProbeError(f"not a port number: {value!r}")
        resolved_ports.append(number)

    timeout = float(request.get("timeout_seconds") or 3.0)
    # Sequentially this is ten timeouts on a dead host — over half a minute to learn nothing.
    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, len(resolved_ports) or 1)) as pool:
        results = list(pool.map(lambda p: probe_port(host, p, timeout), resolved_ports))
    results.sort(key=lambda item: item["port"])

    open_ports = [item["port"] for item in results if item["open"]]
    management = [port for port in MANAGEMENT_PORTS if port in open_ports]
    verdict, detail = _verdict(profile, open_ports, management, results)
    return {
        "server_id": server_id,
        "host": host,
        "profile": profile.to_dict(),
        "verdict": verdict,
        "detail": detail,
        "management_ports": management,
        "open_ports": open_ports,
        "ports": results,
    }


def _verdict(
    profile: TargetProfile,
    open_ports: list[int],
    management: list[int],
    results: list[dict[str, Any]],
) -> tuple[str, str]:
    """What the open ports mean for db_ops, in words an operator can act on.

    The OS matters to exactly one of these answers and it is the important one: *"WinRM is not
    listening"* invites somebody to go and enable it, which on Windows Server 2003 is an afternoon
    spent discovering it cannot be done. Saying so here is the difference.
    """
    if management:
        names = ", ".join(f"{PORT_NAMES.get(port, port)}:{port}" for port in management)
        return "manageable", f"a script can reach this host over {names}"

    if 3389 in open_ports:
        if profile.platform == PLATFORM_WINDOWS and not windows_management_transport_available(profile):
            return "interactive_only", (
                f"answers on RDP 3389 and no management port, and none is possible: "
                f"{profile.os_text or 'this Windows release'} ships no WinRM and cannot run the "
                "OpenSSH server. Automating it needs a different transport, not a service enabled."
            )
        return "interactive_only", (
            "answers on RDP 3389 but neither SSH nor WinRM is listening — it is administered "
            "interactively, so no command can be run and no OS credential can be proven"
        )

    if open_ports:
        names = ", ".join(f"{PORT_NAMES.get(port, port)}:{port}" for port in open_ports)
        return "service_only", (
            f"reachable on {names} but on no management port, so the service can be used and the "
            "machine behind it cannot be operated on"
        )

    refused = [item["port"] for item in results if item["state"] == "refused"]
    if refused:
        return "unreachable", (
            "nothing is listening on any probed port, but the host answered (refused rather than "
            "dropped), so it is up"
        )
    return "unreachable", "nothing answered on any probed port — host down, filtered, or gone"
