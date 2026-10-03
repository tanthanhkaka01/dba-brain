"""How much room a restore needs before it is allowed to start.

**The run this exists for.** On 2026-09-17 a restore drill copied about 115 GB onto the host that
also carries the runtime store. ``/`` reached **42 MB free**, PostgreSQL could not write, and the
daemon went with it. Nothing had asked the obvious question first: *do the files being copied fit?*

The rule the operator set on 2026-09-19 is deliberately blunt, because a clever one is a rule nobody
can check at three in the morning:

    free >= bytes_to_copy x safety_factor        default factor 2.0

The factor is not decoration. The copy is not the only thing that grows during a restore: the
database engine writes its own data and log files as it restores, a `.bak` expands to more than its
compressed size, and the host keeps working while all of it happens. It was 1.5 until 0.26.0; the
operator made it **2.0, for every engine** (2026-10-02): the restored database usually lives on the
filesystem the staged files are on, which is the shape that emptied the disk, and one number that
is the same for SQL Server, PostgreSQL and Oracle is a number people remember.

**What the bytes are** (0.26.0 §1.76). They are the files still to stage (:func:`remaining_bytes`):
a file already on the target at its size is not copied, so it is not counted - the second run of a
drill measured its staged chain again and would have been refused by files it was not going to write.

**The rule does not measure the database a backup holds - unless the entry asks.** A compressed
backup says little about what it restores to: on 2026-10-01 a 56.8 GiB chain passed at x2 against
452 GiB free and built one database at **366.6 GB**. The operator's ruling (2026-10-02): the
engineer who sets up a restore knows the disk has to hold the database; x2 on the copy is the
default and the whole of it. An entry that wants the restore itself measured says
``"measure_restore": true``, and then each database is asked, before its first RESTORE, for the
files it creates less the files it overwrites (:func:`judge_restore`), held to the same factor and
the same ``on_unknown``. Only a share-driven SQL Server restore can be asked today; a script-driven
entry (PostgreSQL, Oracle) that sets it is refused when it is read, not left unmeasured.

This module is arithmetic and vocabulary only — no filesystem, no SSH — so the rule can be read and
tested without a host to run it against. Measuring the numbers is the app's job
(:mod:`db_ops.backup_restore.space`), and refusing is the caller's.
"""

from __future__ import annotations

from db_ops.lib import errors
from dataclasses import dataclass
from typing import Mapping

#: Multiplied by the bytes about to be copied to get the room a restore must find. Not 1.0, because
#: the copy is never the only thing that grows; 2.0 for every engine since 0.26.0 (it was 1.5) - see
#: the module docstring.
DEFAULT_SAFETY_FACTOR: float = 2.0

#: Refused below this. A factor under 1.0 asks for less room than the files need, which is not a
#: safety margin but a way of writing the check off while appearing to have one.
MINIMUM_SAFETY_FACTOR: float = 1.0

#: What to do when a number could not be read. ``refuse`` is the default and the reason this module
#: exists: an unmeasured restore is exactly the one that filled the disk. ``proceed`` is for a
#: target whose free space genuinely cannot be reached, and it says so in the log every time.
UNKNOWN_CHOICES: tuple[str, ...] = ("refuse", "proceed")

_GIB = 1024 ** 3


class RestoreSpaceError(errors.RequestError):
    """A ``space_check`` block that cannot be obeyed as written."""


@dataclass(frozen=True)
class SpaceCheck:
    """The configured rule for one restore entry."""

    enabled: bool = True
    factor: float = DEFAULT_SAFETY_FACTOR
    on_unknown: str = "refuse"
    #: Also measure the database each restore builds, before its first RESTORE. Off unless the
    #: entry says so (the operator, 2026-10-02): by default the copy is the only thing measured.
    measure_restore: bool = False


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

    unknown = sorted(set(raw) - {"enabled", "factor", "on_unknown", "measure_restore"})
    if unknown:
        raise RestoreSpaceError(f"Unknown space_check field(s): {', '.join(unknown)}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RestoreSpaceError(f"space_check.enabled must be true or false, got {enabled!r}")

    # Strictly a boolean: "yes" read as true is a measurement nobody stated, and read as false is
    # an entry that says it is measured and is not.
    measure_restore = raw.get("measure_restore", False)
    if not isinstance(measure_restore, bool):
        raise RestoreSpaceError(
            f"space_check.measure_restore must be true or false, got {measure_restore!r}")

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

    return SpaceCheck(enabled=enabled, factor=factor, on_unknown=on_unknown,
                      measure_restore=measure_restore)


#: What a script-driven entry that sets ``measure_restore`` is told. Refused when the entry is read,
#: not at run time: an entry that says its restore is measured, and is not, is the gap this field
#: would otherwise open on the day it is added.
MEASURE_RESTORE_UNSUPPORTED = (
    "space_check.measure_restore is not supported for a script-driven restore yet (PostgreSQL, "
    "Oracle, SQL Server in a container): only a share-driven SQL Server restore can ask its target "
    "what the backup holds. Leave it out - the copy is still held to the factor.")


def as_request(rule: SpaceCheck) -> dict[str, object]:
    """The rule as the ``space_check`` object a copy command takes - the copy's three fields.

    ``measure_restore`` is the restore's, not the copy's, and is not handed on.
    """
    return {"enabled": rule.enabled, "factor": rule.factor, "on_unknown": rule.on_unknown}


def free_space_command(target: str) -> str:
    """``df`` at ``target`` on a Linux host, or at the nearest folder above it that exists.

    The staging folder is made by the copy, which runs after this check - so on a target that has
    never been restored to, it is not there yet, and ``df`` on it answers "no such file". The local
    measurement already climbed to the nearest existing parent; the Linux one did not, and the
    first restore onto every rebuilt lab was refused as "could not measure" (the 0.25.0 soak,
    2026-09-29: the import folder had gone with the rebuild). The filesystem the folder will live
    on is the one that answers.

    ``-P`` for the portable one-line-per-filesystem format and ``-k`` for 1024-byte blocks: without
    both, a long device name wraps onto its own line and the column that gets parsed is the wrong
    one - which reads as a target with almost no free space, or with far too much.
    """
    quoted = "'" + str(target).replace("'", "'\\''") + "'"
    return (f"p={quoted}; "
            'while [ ! -e "$p" ] && [ "$p" != / ]; do p=$(dirname "$p"); done; '
            'df -Pk "$p"')


def parse_df_free_bytes(stdout: str) -> int | None:
    """The free bytes :func:`free_space_command` answered, or ``None`` when it did not say."""
    lines = [line for line in str(stdout or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    columns = lines[-1].split()
    if len(columns) < 4:
        return None
    try:
        return int(columns[3]) * 1024
    except ValueError:
        return None


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


def remaining_bytes(incoming: Mapping[str, int], staged: Mapping[str, int], *,
                    recopy: bool = False) -> tuple[int, int]:
    """``(bytes the copy will write, bytes already staged)`` for one copy.

    Both maps are ``{path relative to the staging root: size}``, spelled the same way. A file is
    staged when the target holds that path **at that size** - the copy's own rule for leaving a file
    alone; one that is there at another size is written again, whole.

    ``recopy`` is a forced copy: every staged file is written again beside itself and moved over the
    old one, one file at a time, so what it needs on top of the files not yet there is room for the
    largest of them.
    """
    to_write = 0
    staged_bytes = 0
    largest_staged = 0
    for path, size in incoming.items():
        if staged.get(path) == size:
            staged_bytes += size
            largest_staged = max(largest_staged, size)
        else:
            to_write += size
    return to_write + (largest_staged if recopy else 0), staged_bytes


@dataclass(frozen=True)
class RestoreRoomVerdict:
    """Whether one database's restore fits, with every number that went into the answer."""

    ok: bool
    new_bytes: int
    replaced_bytes: int
    free_bytes: int
    required_bytes: int
    factor: float

    @property
    def added_bytes(self) -> int:
        return max(0, self.new_bytes - self.replaced_bytes)

    @property
    def shortfall_bytes(self) -> int:
        return max(0, self.required_bytes - self.free_bytes)

    @property
    def text(self) -> str:
        head = f"{format_gib(self.new_bytes)} of database files to create"
        if self.replaced_bytes:
            head += (f", {format_gib(self.replaced_bytes)} of them over the files of the database "
                     f"being replaced = {format_gib(self.added_bytes)} added")
        head += (f", x{self.factor:g} = {format_gib(self.required_bytes)} needed, "
                 f"{format_gib(self.free_bytes)} free")
        if self.ok:
            return head + " - fits"
        return head + f" - SHORT BY {format_gib(self.shortfall_bytes)}"


def judge_restore(new_bytes: int, replaced_bytes: int, free_bytes: int,
                  factor: float = DEFAULT_SAFETY_FACTOR) -> RestoreRoomVerdict:
    """Do the files a restore creates fit where they go.

    ``new_bytes`` is what the backup says its files are - their size as files, which a compressed
    backup's own size does not tell. ``replaced_bytes`` is the size of the files the restore writes
    over: a drill run again over its own last restore adds only what the database grew by, and
    counting it whole refused every run after the first. The factor is the entry's, as for the
    copy: the margin is the point, and a restore that merely fits is the one that left 21 GiB.
    """
    if new_bytes < 0 or replaced_bytes < 0:
        raise RestoreSpaceError(
            f"sizes must be >= 0, got new_bytes={new_bytes}, replaced_bytes={replaced_bytes}")
    needed = required_bytes(max(0, new_bytes - replaced_bytes), factor)
    return RestoreRoomVerdict(
        ok=free_bytes >= needed,
        new_bytes=new_bytes,
        replaced_bytes=replaced_bytes,
        free_bytes=free_bytes,
        required_bytes=needed,
        factor=factor,
    )


__all__ = [
    "DEFAULT_SAFETY_FACTOR",
    "MEASURE_RESTORE_UNSUPPORTED",
    "MINIMUM_SAFETY_FACTOR",
    "UNKNOWN_CHOICES",
    "RestoreRoomVerdict",
    "RestoreSpaceError",
    "SpaceCheck",
    "SpaceVerdict",
    "as_request",
    "format_gib",
    "free_space_command",
    "judge",
    "judge_restore",
    "parse_df_free_bytes",
    "parse_space_check",
    "remaining_bytes",
    "required_bytes",
]
