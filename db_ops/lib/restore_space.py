"""How much room a restore needs before it is allowed to start.

**The run this exists for.** On 2026-09-17 a restore drill copied about 115 GB onto the host that
also carries the runtime store. ``/`` reached **42 MB free**, PostgreSQL could not write, and the
daemon went with it. Nothing had asked the obvious question first: *do the files being copied fit?*

The rule the operator set on 2026-09-19 is deliberately blunt, because a clever one is a rule nobody
can check at three in the morning:

    free >= bytes_to_copy x safety_factor        default factor 1.5

The factor is not decoration. The copy is not the only thing that grows during a restore: the
database engine writes its own data and log files as it restores, a `.bak` expands to more than its
compressed size, and the host keeps working while all of it happens. 1.5 covers a restore that lands
beside its own backup; **2.0 is the value to set when the restored database will live on the same
filesystem as the staged files**, which is the shape that emptied the disk.

This module is arithmetic and vocabulary only — no filesystem, no SSH — so the rule can be read and
tested without a host to run it against. Measuring the two numbers is the app's job
(:mod:`db_ops.backup_restore.space`), and refusing is the caller's.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Multiplied by the bytes about to be copied to get the room a restore must find. 1.5 rather than
#: 1.0 because the copy is never the only thing that grows; see the module docstring.
DEFAULT_SAFETY_FACTOR: float = 1.5

#: Refused below this. A factor under 1.0 asks for less room than the files need, which is not a
#: safety margin but a way of writing the check off while appearing to have one.
MINIMUM_SAFETY_FACTOR: float = 1.0

#: What to do when a number could not be read. ``refuse`` is the default and the reason this module
#: exists: an unmeasured restore is exactly the one that filled the disk. ``proceed`` is for a
#: target whose free space genuinely cannot be reached, and it says so in the log every time.
UNKNOWN_CHOICES: tuple[str, ...] = ("refuse", "proceed")

_GIB = 1024 ** 3


class RestoreSpaceError(ValueError):
    """A ``space_check`` block that cannot be obeyed as written."""


@dataclass(frozen=True)
class SpaceCheck:
    """The configured rule for one restore entry."""

    enabled: bool = True
    factor: float = DEFAULT_SAFETY_FACTOR
    on_unknown: str = "refuse"


@dataclass(frozen=True)
class SpaceVerdict:
    """The answer, with every number that went into it, so a log line needs no second lookup."""

    ok: bool
    incoming_bytes: int
    free_bytes: int
    required_bytes: int
    factor: float

    @property
    def shortfall_bytes(self) -> int:
        return max(0, self.required_bytes - self.free_bytes)

    @property
    def text(self) -> str:
        head = (f"{format_gib(self.incoming_bytes)} to copy, "
                f"x{self.factor:g} = {format_gib(self.required_bytes)} needed, "
                f"{format_gib(self.free_bytes)} free")
        if self.ok:
            return head + " - fits"
        return head + f" - SHORT BY {format_gib(self.shortfall_bytes)}"


def format_gib(value: int) -> str:
    """Bytes as GiB, to one decimal. Restores are measured in tens of gigabytes; bytes do not read."""
    return f"{value / _GIB:.1f} GiB"


def parse_space_check(entry: dict | None) -> SpaceCheck:
    """Read a restore entry's ``space_check`` block.

    Absent means **on, at the default factor** — a check that has to be switched on protects only
    the entries somebody remembered, and the entry nobody remembered is the one that fills the disk.
    """
    raw = (entry or {}).get("space_check")
    if raw is None:
        return SpaceCheck()
    if not isinstance(raw, dict):
        raise RestoreSpaceError("space_check must be an object, e.g. {\"factor\": 2.0}")

    unknown = sorted(set(raw) - {"enabled", "factor", "on_unknown"})
    if unknown:
        raise RestoreSpaceError(f"Unknown space_check field(s): {', '.join(unknown)}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RestoreSpaceError(f"space_check.enabled must be true or false, got {enabled!r}")

    factor_raw = raw.get("factor", DEFAULT_SAFETY_FACTOR)
    try:
        factor = float(factor_raw)
    except (TypeError, ValueError) as exc:
        raise RestoreSpaceError(f"space_check.factor must be a number, got {factor_raw!r}") from exc
    if factor < MINIMUM_SAFETY_FACTOR:
        raise RestoreSpaceError(
            f"space_check.factor must be >= {MINIMUM_SAFETY_FACTOR:g}, got {factor:g}: a factor "
            "below 1 asks for less room than the files themselves need. To turn the check off, say "
            "so - {\"enabled\": false}")

    on_unknown = str(raw.get("on_unknown") or "refuse").strip().lower()
    if on_unknown not in UNKNOWN_CHOICES:
        raise RestoreSpaceError(
            f"space_check.on_unknown must be one of {', '.join(UNKNOWN_CHOICES)}, got {on_unknown!r}")

    return SpaceCheck(enabled=enabled, factor=factor, on_unknown=on_unknown)


def required_bytes(incoming_bytes: int, factor: float = DEFAULT_SAFETY_FACTOR) -> int:
    """The room the restore must find, rounded **up**: a check that rounds down can pass at zero."""
    if incoming_bytes < 0:
        raise RestoreSpaceError(f"incoming_bytes must be >= 0, got {incoming_bytes}")
    required = incoming_bytes * factor
    whole = int(required)
    return whole if whole == required else whole + 1


def judge(incoming_bytes: int, free_bytes: int,
          factor: float = DEFAULT_SAFETY_FACTOR) -> SpaceVerdict:
    """Does it fit, and by how much."""
    needed = required_bytes(incoming_bytes, factor)
    return SpaceVerdict(
        ok=free_bytes >= needed,
        incoming_bytes=incoming_bytes,
        free_bytes=free_bytes,
        required_bytes=needed,
        factor=factor,
    )


__all__ = [
    "DEFAULT_SAFETY_FACTOR",
    "MINIMUM_SAFETY_FACTOR",
    "UNKNOWN_CHOICES",
    "RestoreSpaceError",
    "SpaceCheck",
    "SpaceVerdict",
    "format_gib",
    "judge",
    "parse_space_check",
    "required_bytes",
]
