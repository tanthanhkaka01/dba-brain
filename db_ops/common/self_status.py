"""What this installation is, where it is running, and how much room it has left.

Every other status answer in the toolkit is about something else: ``ops-status`` reads the store to
say whether the *apps* ran on schedule, ``host-facts`` reaches a monitored *host* over its
``cmd_access``, ``worker-status`` drives the worker *from the master* over SSH. Nothing answered the
question asked first when something looks wrong — *which build am I talking to, on which machine,
and is it out of memory or disk*. On 2026-09-03 that was answered by hand four times: two versions
were in play, one on a host and one in a container, and telling them apart took a shell.

No SSH and no store: this process is already on the machine being described, so it reads itself.
That is also why it is the one status that still answers when the store is unreachable.

**Inside a container, ``/proc/meminfo`` is the host's memory, not the limit this process has.** A
report that quotes 64 GB while the cgroup allows 2 GB is worse than no report — it is the number
somebody uses to rule memory out. The cgroup is read first, and every memory figure says which
source it came from.

There are no third-party dependencies here on purpose (the package has exactly two, neither of them
psutil). Anything the standard library cannot answer on this platform is reported as unavailable
rather than guessed.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: cgroup v2, then v1. A container that was given no limit reports "max" in v2 and a number near
#: 2**63 in v1; both mean "the host's memory", so both fall through to /proc/meminfo.
_CGROUP_V2 = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current")
_CGROUP_V1 = ("/sys/fs/cgroup/memory/memory.limit_in_bytes",
              "/sys/fs/cgroup/memory/memory.usage_in_bytes")

#: Above this a cgroup limit is the kernel's "unlimited" sentinel rather than a real ceiling.
_UNLIMITED_ABOVE_BYTES = 1 << 50


def _read_int(path: str) -> int | None:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _memory_from_cgroup() -> dict[str, Any] | None:
    for limit_path, used_path in (_CGROUP_V2, _CGROUP_V1):
        limit = _read_int(limit_path)
        used = _read_int(used_path)
        if limit is None or used is None or limit > _UNLIMITED_ABOVE_BYTES:
            continue
        return {"source": "cgroup", "total_bytes": limit, "used_bytes": used,
                "available_bytes": max(0, limit - used)}
    return None


def _memory_from_proc() -> dict[str, Any] | None:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    fields: dict[str, int] = {}
    for line in lines:
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[name] = int(parts[0]) * 1024
    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    if total is None or available is None:
        return None
    return {"source": "/proc/meminfo (the host's, not a container limit)",
            "total_bytes": total, "used_bytes": total - available,
            "available_bytes": available}


def _memory_from_windows() -> dict[str, Any] | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return {"source": "GlobalMemoryStatusEx", "total_bytes": int(status.ullTotalPhys),
                "used_bytes": int(status.ullTotalPhys - status.ullAvailPhys),
                "available_bytes": int(status.ullAvailPhys)}
    except Exception:  # noqa: BLE001 - an unreadable counter is "unavailable", not a failure.
        return None


def memory() -> dict[str, Any]:
    """Memory as this process actually experiences it, saying where the numbers came from."""
    for reader in (_memory_from_cgroup, _memory_from_proc, _memory_from_windows):
        found = reader()
        if found:
            return found
    return {"source": "unavailable on this platform"}


def cpu() -> dict[str, Any]:
    """Cores and, where the platform has one, the load average.

    ``os.getloadavg`` does not exist on Windows, and a load average is not a percentage: it is
    runnable processes, so it is reported beside the core count rather than instead of it.
    """
    cores = os.cpu_count()
    facts: dict[str, Any] = {"cores": cores}
    try:
        one, five, fifteen = os.getloadavg()
        facts["load_avg"] = [round(one, 2), round(five, 2), round(fifteen, 2)]
        if cores:
            facts["load_per_core"] = round(one / cores, 2)
    except (AttributeError, OSError):
        facts["load_avg"] = None
    return facts


def host_addresses() -> dict[str, Any]:
    """The machine's name and the address it reaches the network from.

    ``gethostbyname(gethostname())`` returns the loopback on many Linux hosts, so the outward
    address is taken from an unconnected UDP socket - which sends nothing and needs nothing to be
    listening, but does make the kernel pick the interface it would route from.
    """
    name = socket.gethostname()
    outward = None
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(0.2)
        probe.connect(("203.0.113.1", 9))  # RFC 5737; nothing is sent to it
        outward = probe.getsockname()[0]
    except OSError:
        outward = None
    finally:
        probe.close()
    try:
        resolved = socket.gethostbyname(name)
    except OSError:
        resolved = None
    return {"hostname": name, "ip": outward or resolved, "resolved_ip": resolved}


def _uptime_seconds_from_proc() -> float | None:
    """Seconds since boot from ``/proc/uptime`` — Linux, including inside a container."""
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _uptime_seconds_from_windows() -> float | None:
    """Seconds since boot from ``GetTickCount64`` — milliseconds, monotonic, and no privilege.

    Deliberately not WMI's ``LastBootUpTime``: that spawns a process, needs the WMI service, and
    returns a local-time string with an offset field that has to be parsed. This is one call and
    the answer is already an interval, so nothing about it depends on the machine's timezone.
    """
    try:
        import ctypes

        ticks = ctypes.windll.kernel32.GetTickCount64()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    return float(ticks) / 1000.0


def uptime(*, now: datetime | None = None) -> dict[str, Any]:
    """How long the machine has been up, and the instant it came up, in UTC.

    **This is the host's uptime, not the daemon's**, which is why the rendered line says so. The
    two answer different questions and this module can only answer one of them honestly: it reads
    *itself*, and the process reading is the short-lived one the bot spawned to ask, not the
    scheduler. Whether the scheduler has been running is `ops-status`, which reads the store.

    Inside a container `/proc/uptime` is the **host's** clock, not the container's age. The source
    is reported for the same reason every memory figure reports one — a number whose meaning
    changes with where it was read has to say where it was read.

    UTC throughout: an interval has no timezone, and the instant it started is only comparable
    across an estate if it is written in one.
    """
    seconds = _uptime_seconds_from_proc()
    source = "/proc/uptime"
    if seconds is None:
        seconds = _uptime_seconds_from_windows()
        source = "GetTickCount64"
    if seconds is None or seconds < 0:
        return {"seconds": None, "hours": None, "since": None, "source": "unavailable"}
    moment = now or datetime.now(timezone.utc)
    return {
        "seconds": round(seconds, 3),
        "hours": round(seconds / 3600.0, 2),
        "since": (moment - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
    }


def disk(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return {"source": "unavailable"}
    return {"path": str(path), "total_bytes": usage.total, "used_bytes": usage.used,
            "free_bytes": usage.free}


def _gib(value: Any) -> str:
    if not isinstance(value, int):
        return "?"
    return f"{value / (1024 ** 3):.1f} GiB"


def _percent(used: Any, total: Any) -> str:
    if not isinstance(used, int) or not isinstance(total, int) or total <= 0:
        return "?"
    return f"{used * 100 / total:.0f}%"


def distribution() -> dict[str, Any]:
    """Which distribution is running: the public ``dbabrain`` wheel or the private ``db_ops`` build.

    The reason this is here at all: on 2026-09-03 both were live on the same estate within the same
    hour - a container built from the private tree and a pip install of the published wheel - and
    telling them apart from a chat message was impossible. The version alone does not do it; the two
    number differently (``2.87.01`` against ``0.5.0``), which is itself the giveaway once something
    says which scheme is in play.

    A source checkout or a container that runs the tree straight from a path has no distribution
    metadata at all, and that is an answer too, not a failure.
    """
    from importlib import metadata

    for name in ("dbabrain", "db_ops"):
        try:
            found = metadata.distribution(name)
        except Exception:  # noqa: BLE001 - not installed under this name; try the other.
            continue
        return {"name": name, "installed_version": found.version,
                "product": "DBA Brain (published)" if name == "dbabrain" else "db_ops (private build)",
                "source": "pip-installed"}
    return {"name": None, "installed_version": None,
            "product": "db_ops (source tree)", "source": "run from a source tree, not installed"}


def runtime() -> str:
    """Docker, Kubernetes, or straight on the operating system."""
    if Path("/.dockerenv").exists():
        return "docker"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return "host"
    for marker, label in (("docker", "docker"), ("containerd", "containerd"),
                          ("kubepods", "kubernetes"), ("lxc", "lxc")):
        if marker in cgroup:
            return label
    return "host"


def operating_system() -> str:
    """The distribution a person would name, not the kernel version.

    ``platform.platform()`` answers "Linux-6.8.0-generic" for both Ubuntu and RHEL, which does not
    help anybody deciding whether a package name applies.
    """
    if os.name == "nt":
        release, version, _sp, _kind = platform.win32_ver()
        return f"Windows {release} ({version})".strip()
    try:
        release = platform.freedesktop_os_release()
        pretty = release.get("PRETTY_NAME") or release.get("NAME")
        if pretty:
            return f"{pretty} ({platform.release()})"
    except (AttributeError, OSError):
        pass
    return f"{platform.system()} {platform.release()}"


def collect(*, tool_root: Path, version: str, public_version: str | None = None,
            store: str | None = None, node_role: str | None = None) -> dict[str, Any]:
    """Everything the report needs, as data. Callers that want JSON stop here."""
    return {
        "version": version,
        "public_version": public_version,
        "python": platform.python_version(),
        "distribution": distribution(),
        "runtime": runtime(),
        "os": operating_system(),
        "tool_root": str(tool_root),
        "node_role": node_role or os.environ.get("DB_OPS_NODE_ROLE") or "master (default)",

        "store": store,
        "host": host_addresses(),
        "cpu": cpu(),
        "memory": memory(),
        "disk": disk(tool_root),
        "uptime": uptime(),
        "pid": os.getpid(),
    }


def render(facts: dict[str, Any]) -> str:
    """The same facts as a chat message. Read on a phone, so it is short and every line is a fact."""
    host = facts.get("host") or {}
    cpu_facts = facts.get("cpu") or {}
    mem = facts.get("memory") or {}
    disk_facts = facts.get("disk") or {}

    dist = facts.get("distribution") or {}
    version = facts.get("version") or "?"
    public = facts.get("public_version")

    # Which product, before anything else: two builds of this toolkit can run the same estate and
    # they number differently, so the version below is only readable once this line is read.
    product_line = f"product   : {dist.get('product') or 'db_ops'}"
    if dist.get("name"):
        product_line += f"  [pip: {dist['name']} {dist.get('installed_version')}]"

    version_line = f"version   : {version}" + (f"  (public {public})" if public else "")

    runtime_text = facts.get("runtime") or "host"
    where = "in Docker" if runtime_text == "docker" else (
        "on the OS directly" if runtime_text == "host" else f"in {runtime_text}")

    lines = [
        "DBA Brain / db_ops - current state",
        product_line,
        version_line,
        f"running   : {where}, on {facts.get('os')}",
        f"python    : {facts.get('python')}",
        f"host      : {host.get('hostname') or '?'}",
        f"ip        : {host.get('ip') or 'unavailable'}",
        f"tool root : {facts.get('tool_root')}",
        f"node_role : {facts.get('node_role')}",
    ]
    if facts.get("store"):
        lines.append(f"store     : {facts['store']}")

    load = cpu_facts.get("load_avg")
    cpu_line = f"cpu       : {cpu_facts.get('cores') or '?'} core(s)"
    if load:
        cpu_line += f", load {load[0]} / {load[1]} / {load[2]}"
        if cpu_facts.get("load_per_core") is not None:
            cpu_line += f" ({cpu_facts['load_per_core']} per core)"
    lines.append(cpu_line)

    if isinstance(mem.get("total_bytes"), int):
        lines.append(
            f"memory    : {_gib(mem.get('used_bytes'))} used of {_gib(mem.get('total_bytes'))}"
            f" ({_percent(mem.get('used_bytes'), mem.get('total_bytes'))}), "
            f"{_gib(mem.get('available_bytes'))} free"
        )
        # Which source answered decides whether the number means anything in a container.
        lines.append(f"            source: {mem.get('source')}")
    else:
        lines.append(f"memory    : {mem.get('source')}")

    if isinstance(disk_facts.get("total_bytes"), int):
        lines.append(
            f"disk      : {_gib(disk_facts.get('free_bytes'))} free of "
            f"{_gib(disk_facts.get('total_bytes'))} "
            f"({_percent(disk_facts.get('used_bytes'), disk_facts.get('total_bytes'))} used)"
        )

    up = facts.get("uptime") or {}
    if up.get("hours") is not None:
        # "host up since" rather than "up since": this is the machine's clock, and on a node whose
        # daemon was restarted an hour ago the two numbers are nothing like each other.
        lines.append(f"uptime    : {up['hours']:.2f} h  (host up since {up.get('since')})")
    else:
        lines.append(f"uptime    : {up.get('source') or 'unavailable'}")
    return "\n".join(lines)
