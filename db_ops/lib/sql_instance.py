"""Reading a SQL Server instance address — ``host``, ``host,port``, ``host\\instance``.

**Why this is its own rule.** A restore entry says where it restores to in SQL Server's own
spelling: ``localhost,1453``. Everything that *performs* the restore passes that string through to
``sqlcmd -S``, which understands it. Everything that *checks* the restore connects with a driver,
which wants a host and a port as separate numbers — and until 2026-09-19 the check did not read the
string at all: it used port 1433 unconditionally.

Measured on the estate that day: the entry restores into ``localhost,1453``, which is the container
``MSSQL_192_0_2_115_1453``; the verification asked ``192.0.2.115:1433``, which is a different
container (``MSSQL_LAB``) holding thirteen unrelated databases. Port 1453 held exactly the three
databases the drill restores, one of them stuck in ``RESTORING`` — the single state the whole
verification exists to catch — and the check could not see it. It would instead have reported all
three ``ABSENT``, which sends whoever reads it to look for a restore that never ran.

**A named instance has no port to give**, and this says so rather than guessing. ``HOST\\SQLEXPRESS``
is resolved by the SQL Server Browser at connect time; substituting 1433 would ask a different
server and call the answer a verdict, which is the failure above with different numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Where SQL Server listens when nothing says otherwise.
DEFAULT_PORT = 1433


@dataclass(frozen=True)
class InstanceAddress:
    """What an instance string resolves to for a driver that needs host and port."""

    host: str
    port: int | None
    named_instance: str = ""

    @property
    def has_port(self) -> bool:
        """False for a named instance, which cannot be reached by a port this module can name."""
        return self.port is not None


def parse(value: str | None) -> InstanceAddress:
    """Read ``host``, ``host,port``, ``tcp:host,port`` or ``host\\instance``.

    The host is returned as written and is often ``localhost`` — *localhost on the target*, which is
    not where the caller is. A caller that reaches the machine across the network keeps its own
    address and takes only the port from here; see ``_verify_request``.
    """
    text = str(value or "").strip()
    if not text:
        return InstanceAddress(host="", port=DEFAULT_PORT)
    # `tcp:` is a protocol prefix SQL Server accepts and a driver does not.
    for prefix in ("tcp:", "np:", "lpc:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if "\\" in text:
        host, _, instance = text.partition("\\")
        instance, _, port_text = instance.partition(",")
        if port_text.strip().isdigit():
            # `HOST\INSTANCE,1453` states both. The port wins: it is what a driver can use.
            return InstanceAddress(host=host.strip(), port=int(port_text.strip()),
                                   named_instance=instance.strip())
        return InstanceAddress(host=host.strip(), port=None, named_instance=instance.strip())
    host, _, port_text = text.partition(",")
    port_text = port_text.strip()
    if port_text.isdigit():
        return InstanceAddress(host=host.strip(), port=int(port_text))
    return InstanceAddress(host=host.strip(), port=DEFAULT_PORT)


__all__ = ["DEFAULT_PORT", "InstanceAddress", "parse"]
