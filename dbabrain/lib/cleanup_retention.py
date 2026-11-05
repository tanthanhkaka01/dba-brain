"""How long a directory of backups keeps them, in seconds, spelled one way everywhere.

There were two fields and two units for one idea:

* ``retention_days`` on a **backup job** — how long the directory that job *writes to* keeps its
  own files, which decides how far back a recovery can reach;
* ``target_retention_seconds`` on a **restore entry** — how long the target keeps the copies
  *staged* for restoring from, which only has to cover the next restore.

The two numbers genuinely differ, and should: a source may hold fourteen days of recovery while the
restore target only needs the newest full and the chain hanging off it. But two names and two units
for "how long are files kept" is one concept wearing two costumes, and it reads as an inconsistency
because it is one. Asked on 2026-09-11: *"why both?"*

So: **one name, `cleanup_retention`, and seconds — the unit dbabrain uses for every interval.** Both
sides read the same field and act on it differently, which is the honest shape: backup prunes its
own output after backing up, restore prunes its staging after restoring.

The old spellings are still accepted so a config written before this does not stop loading, and
``days`` is converted rather than guessed at. Seconds also removes a rounding that was silently
changing configured values: the restore engine spoke hours and the conversion was
``max(1, seconds // 3600)``, so anything under an hour became an hour and nothing said so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dbabrain.lib import errors

#: The canonical field name. One spelling, both halves of the app.
FIELD = "cleanup_retention"

#: Older spellings, with the factor that turns each into seconds. Read but never written.
LEGACY_FIELDS: dict[str, int] = {
    "target_retention_seconds": 1,
    "retention_days": 24 * 3600,
}

#: 8 days. The full backup is weekly, so an 8-day window always keeps the newest full and every
#: incremental chained to it — a margin rather than a tight fit. Quoted in the error below so an
#: operator does not have to go and find it.
DEFAULT_SECONDS = 8 * 24 * 3600


class CleanupRetentionError(errors.ConfigError):
    """The retention setting is missing or is not a number of seconds."""


def parse(entry: dict[str, Any], *, context: str, required: bool = True) -> int:
    """Seconds of retention for one backup job or restore entry.

    Required by default, and that is the point rather than strictness for its own sake: an absent
    field is indistinguishable from a considered one, and on 2026-09-11 six of this estate's
    fourteen restore entries carried none at all — each running on whatever the release's default
    happened to be, which nobody had chosen.

    ``0`` is a real setting and means *no age gate*: every file becomes a candidate and the chain
    rule alone decides (never the newest full, nor anything at or after it). It does not mean
    "keep everything", and the wording here says so because the opposite was believed once and a
    staging directory reached 16.7 GB on the strength of it.
    """
    raw = entry.get(FIELD)
    source = FIELD
    if raw is None:
        for name, factor in LEGACY_FIELDS.items():
            if entry.get(name) is not None:
                raw, source = entry[name], name
                break
        else:
            if not required:
                return DEFAULT_SECONDS
            raise CleanupRetentionError(
                f"{context}.{FIELD} is required: how long this keeps its backup files, "
                f"**in seconds**. {DEFAULT_SECONDS} is 8 days and is the recommended value - the "
                "full backup is weekly, so an 8-day window always keeps the newest full and every "
                "incremental chained to it. 0 removes the age gate and leaves the chain rule "
                "alone (never the newest full, nor anything at or after it).")

    if isinstance(raw, dict):
        # The per-type form (1.227). A reader that still prunes every type with one number gets the
        # LONGEST window the object states, so nothing is removed earlier than any type asked for.
        return parse_per_type({FIELD: raw}, context=context).longest()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CleanupRetentionError(
            f"{context}.{source} must be a whole number "
            f"({'seconds' if source != 'retention_days' else 'days'}); got {raw!r}.") from None
    if value < 0:
        raise CleanupRetentionError(
            f"{context}.{source} must not be negative (0 = no age gate).")
    return value * LEGACY_FIELDS.get(source, 1) if source != FIELD else value


# --------------------------------------------------------------------------------------------- #
# Per file type (0.35.0, 1.227 - audits/20261029_plan_cleanup_retention_per_file_type.md)
# --------------------------------------------------------------------------------------------- #
#: A window that never removes by age. Removed only by hand.
KEEP = "keep"

#: What an integer reader is handed for a "keep" (1.227): a hundred years. 0 would mean the
#: opposite - no age gate, everything a candidate.
KEEP_AS_SECONDS = 100 * 365 * 24 * 3600

#: The file types each engine writes - the catalog's own words (lib.integrity.FILE_TYPES). An
#: entry naming a type its engine never writes is a mistake, not a setting.
ENGINE_FILE_TYPES: dict[str, tuple[str, ...]] = {
    "sqlserver": ("FULL", "DIFF", "LOG", "CERT"),
    "postgresql": ("FULL_BASE", "INCR_BASE", "MANIFEST", "WAL"),
    "oracle": ("RMAN_L0", "RMAN_L1", "ARCHIVELOG", "CONTROLFILE", "SPFILE"),
}
ENGINE_ALIASES = {"mssql": "sqlserver", "postgres": "postgresql"}

#: Each engine's base type and the types that restore only on top of one (T2, T3).
BASE_TYPE = {"sqlserver": "FULL", "postgresql": "FULL_BASE", "oracle": "RMAN_L0"}
DEPENDENT_TYPES = {"sqlserver": ("DIFF", "LOG"), "postgresql": ("INCR_BASE", "WAL"),
                   "oracle": ("RMAN_L1", "ARCHIVELOG")}
#: The type point-in-time recovery replays (T3).
LOG_TYPE = {"sqlserver": "LOG", "postgresql": "WAL", "oracle": "ARCHIVELOG"}

#: Every key the object form takes.
DEFAULT_KEY = "default"
ALL_FILE_TYPES = tuple(t for types in ENGINE_FILE_TYPES.values() for t in types)


def _engine(value: str | None) -> str:
    name = str(value or "").strip().lower()
    return ENGINE_ALIASES.get(name, name)


@dataclass(frozen=True)
class Retention:
    """A directory's windows per file type. ``None`` is ``keep``; ``0`` is no age gate."""

    windows: dict[str, int | None] = field(default_factory=dict)
    default: int | None = DEFAULT_SECONDS
    #: The integer form: every type, one window - CERT included, exactly as before 1.227.
    uniform: bool = False

    def window_for(self, file_type: str) -> int | None:
        """The window of one type: its own, else the default - except CERT, which an object that
        does not name it keeps (T4: a backup encrypted with a certificate nobody kept cannot be
        restored)."""
        kind = str(file_type or "").strip().upper()
        if kind in self.windows:
            return self.windows[kind]
        if kind == "CERT" and not self.uniform:
            return None
        return self.default

    def longest(self) -> int:
        """The longest window stated, in seconds, for a reader that applies one number to all.
        CERT is left out: its default is keep (T4), and counted it would make every such reader
        keep everything for a century."""
        stated = [w for kind, w in self.windows.items() if kind != "CERT"] + [self.default]
        if any(w is None for w in stated):
            return KEEP_AS_SECONDS
        return max(int(w) for w in stated)

    def as_config(self) -> int | dict[str, Any]:
        """The value as a config or a request states it: the integer, or the object."""
        if self.uniform:
            return int(self.default or 0)
        value: dict[str, Any] = {kind: (KEEP if w is None else w) for kind, w in self.windows.items()}
        value[DEFAULT_KEY] = KEEP if self.default is None else self.default
        return value


def _window(value: Any, *, where: str) -> int | None:
    if isinstance(value, str) and value.strip().lower() == KEEP:
        return None
    if isinstance(value, bool):
        raise CleanupRetentionError(f"{where} must be seconds or \"keep\"; got {value!r}.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise CleanupRetentionError(f"{where} must be seconds or \"keep\"; got {value!r}.") from None
    if number < 0:
        raise CleanupRetentionError(f"{where} must not be negative (0 = no age gate, \"keep\" = never by age).")
    return number


def parse_per_type(entry: dict[str, Any], *, context: str, engine: str | None = None,
                   required: bool = True) -> Retention:
    """``cleanup_retention`` as windows per file type (1.227). The integer form (and the legacy
    spellings) is every type under one window, exactly as before; the object form names types -
    ``FULL``, ``LOG``, ``WAL``, ``ARCHIVELOG`` ... - and ``default`` for a type it does not name.
    With ``engine``, a type that engine never writes is refused."""
    raw = entry.get(FIELD)
    if not isinstance(raw, dict):
        return Retention(default=parse(entry, context=context, required=required), uniform=True)
    kind = _engine(engine)
    allowed = ENGINE_FILE_TYPES.get(kind, ALL_FILE_TYPES)
    windows: dict[str, int | None] = {}
    default: int | None = DEFAULT_SECONDS
    has_default = False
    for key, value in raw.items():
        name = str(key).strip()
        where = f"{context}.{FIELD}.{name}"
        if name.lower() == DEFAULT_KEY:
            default, has_default = _window(value, where=where), True
            continue
        upper = name.upper()
        if upper not in allowed:
            raise CleanupRetentionError(
                f"{where}: {name!r} is not a file type {('a ' + kind + ' backup writes') if kind in ENGINE_FILE_TYPES else 'dbabrain knows'}"
                f" - one of {', '.join(allowed)}, or default.")
        windows[upper] = _window(value, where=where)
    if not has_default and not (kind in ENGINE_FILE_TYPES
                                and all(t in windows for t in ENGINE_FILE_TYPES[kind] if t != "CERT")):
        raise CleanupRetentionError(
            f"{context}.{FIELD} names {', '.join(windows) or 'no type'} and no default: a type it does "
            "not name would have no window. Add \"default\" (seconds, or \"keep\").")
    return Retention(windows=windows, default=default)


def dependents_past_their_base(retention: Retention, *, engine: str) -> list[str]:
    """T2: each type that restores only on top of a base, kept longer than the base - it restores
    nothing once its base is gone. ``LOG: 14 days, FULL: 7 days`` reads as 14 days of point-in-time
    and gives 7. Each finding in words; empty when the windows are consistent."""
    kind = _engine(engine)
    base_type = BASE_TYPE.get(kind)
    if base_type is None:
        return []
    base = retention.window_for(base_type)
    found = []
    for dependent in DEPENDENT_TYPES[kind]:
        window = retention.window_for(dependent)
        # 0 is no age gate (the chain rule alone): not longer than anything.
        if base is None or window == 0:
            continue
        if window is None or (base and window > base):
            found.append(f"{dependent} ({describe_window(window)}) outlives {base_type} "
                         f"({describe_window(base)}): a {dependent} older than the oldest {base_type} "
                         "kept restores nothing")
    return found


#: The incrementals that need every link back to their base - Oracle's `INCREMENTAL LEVEL 1` (not
#: CUMULATIVE) and PostgreSQL's `pg_basebackup --incremental` - so they go as a chain, by the base's
#: window. SQL Server's DIFF is cumulative and needs only its FULL, so its own window is safe.
CHAIN_MEMBERS = {"postgresql": "INCR_BASE", "oracle": "RMAN_L1"}


def chain_member_split(retention: Retention, *, engine: str) -> str:
    """Why an incremental that needs its whole chain has a window of its own, or ``""``. Shorter than
    its base deletes mid-chain - a set that looks present and restores nothing; so its window is
    the base's, stated or not."""
    kind = _engine(engine)
    member = CHAIN_MEMBERS.get(kind)
    if member is None or member not in retention.windows:
        return ""
    base_type = BASE_TYPE[kind]
    if retention.windows[member] == retention.window_for(base_type):
        return ""
    return (f"{member} ({describe_window(retention.windows[member])}) differs from {base_type} "
            f"({describe_window(retention.window_for(base_type))}): each {member} needs every link back "
            f"to its {base_type}, so it goes with its chain - leave {member} out, or give it {base_type}'s window")


def point_in_time_reach(retention: Retention, *, engine: str) -> int | None:
    """T3: how far back point-in-time recovery reaches - the shorter of the base and the log window
    (``None`` = both kept, no limit by age). The number an operator means by "retention"."""
    kind = _engine(engine)
    if kind not in BASE_TYPE:
        return retention.default
    if kind == "oracle":
        # The archivelog BACKUPS follow the level 0's window (1.230): ARCHIVELOG is how long a log
        # stays on disk, and a log on disk is not what a restore elsewhere reads.
        return retention.window_for("RMAN_L0")
    windows = [retention.window_for(BASE_TYPE[kind]), retention.window_for(LOG_TYPE[kind])]
    numbers = [w for w in windows if w is not None]
    return min(numbers) if numbers else None


#: The listing's levels (``list-backup-files`` kinds) and the type that decides each, per engine
#: (1.231). Oracle's log level is its archivelog BACKUPS, which follow the level 0 (1.230).
LEVEL_TYPES = {
    "sqlserver": {"full": "FULL", "diff": "DIFF", "log": "LOG"},
    "postgresql": {"full": "FULL_BASE", "diff": "INCR_BASE", "log": "WAL"},
    "oracle": {"full": "RMAN_L0", "diff": "RMAN_L1", "log": "RMAN_L0"},
}


def windows_by_level(retention: Retention, *, engine: str) -> dict[str, int | None] | None:
    """The window of each listing level (``full`` / ``diff`` / ``log``), ``None`` meaning keep - or
    ``None`` altogether for the one-window form, which the planner reads as it always did."""
    if retention.uniform:
        return None
    levels = LEVEL_TYPES.get(_engine(engine))
    if levels is None:
        return None
    return {level: retention.window_for(kind) for level, kind in levels.items()}


def shorter_than_its_interval(window: int | None, interval_seconds: int | None) -> bool:
    """T5: a window shorter than the job's own interval removes what the job just wrote."""
    return bool(window and interval_seconds and 0 < window < int(interval_seconds))


def registrar_refusal(entry: dict[str, Any], *, context: str, engine: str) -> str:
    """Why a registrar refuses this entry's ``cleanup_retention`` (decision D-R3: refuse a dependent
    type kept past its base when the entry is written; an existing file keeps loading)."""
    try:
        retention = parse_per_type(entry, context=context, engine=engine)
    except CleanupRetentionError as exc:
        return str(exc)
    found = dependents_past_their_base(retention, engine=engine)
    split = chain_member_split(retention, engine=engine)
    found += [split] if split else []
    return f"{context}.{FIELD}: " + "; ".join(found) if found else ""


def problems(value: Any) -> list[str]:
    """What is wrong with an object-form value, for the reference's checker (no engine known)."""
    try:
        parse_per_type({FIELD: value}, context=FIELD)
    except CleanupRetentionError as exc:
        return [str(exc)]
    return []


def describe_window(window: int | None) -> str:
    """``keep``, or :func:`describe`."""
    return "keep" if window is None else describe(window)


def states_it(record: dict[str, Any]) -> bool:
    """Does ``record`` state a retention, in the field or a legacy spelling?"""
    return any(record.get(name) is not None for name in (FIELD, *LEGACY_FIELDS))


def retention_source(job: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Where a backup job's retention is read (1.232): the job's own when it states one - the form
    every entry had until 0.35.0 - else its entry's, stated once for the folder all its jobs write."""
    if states_it(job) or not states_it(entry):
        return job
    return {name: entry[name] for name in (FIELD, *LEGACY_FIELDS) if entry.get(name) is not None}


def job_entry_disagreement(job: dict[str, Any], entry: dict[str, Any]) -> str:
    """A job and its entry stating two values for one folder (1.232): which applies is decided, but
    a reader cannot tell which was meant. ``""`` when they agree or only one states it."""
    if not (states_it(job) and states_it(entry)):
        return ""
    try:
        mine = parse_per_type(job, context="job")
        theirs = parse_per_type(entry, context="entry")
    except CleanupRetentionError:
        return ""
    if mine == theirs:
        return ""
    return (f"the job states {mine.as_config()!r} and its entry {theirs.as_config()!r}: two answers for one "
            "folder - the job's applies; state it once, on the entry (upgrade-config step retention-per-type)")


def types_a_job_writes(engine: str, job: dict[str, Any]) -> tuple[str, ...] | None:
    """The file types one backup job writes, for the fold (1.232) - read from its script and its
    BACKUP_LEVEL. An incremental job writes into its full's chain and gives it no window of its own
    (the chain rule), so it answers ``()``. ``None`` when the job's shape is not one this knows."""
    kind = _engine(engine)
    level = str((job.get("env") or {}).get("BACKUP_LEVEL") or "").strip().lower()
    script = str(job.get("script") or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if kind == "sqlserver" and level in ("full", "diff", "log"):
        return (level.upper(),)
    if kind == "postgresql":
        if "archive_wal" in script:
            return ("WAL",)
        if "basebackup" in script and level == "full":
            return ("FULL_BASE", "INCR_BASE", "MANIFEST")
        if "basebackup" in script and level == "incr":
            return ()
    if kind == "oracle":
        if "archivelog" in script:
            return ("ARCHIVELOG",)
        if "rman_database" in script and level == "0":
            return ("RMAN_L0", "RMAN_L1", "CONTROLFILE", "SPFILE")
        if "rman_database" in script and level == "1":
            return ()
    return None


def fold_entry(entry: dict[str, Any], *, where: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Move a backup entry's jobs' retentions onto the entry (1.232, decision D-R2: what each job
    MEANT). Jobs that agree become the integer - nothing changes. Jobs that differ become the object,
    each job's value on the types it writes and the longest as ``default``: until now the shortest
    job's window cut every type in the folder, and that is what changes - named in the change, so
    nothing changes unseen. Answers (changes, conflicts); an entry it cannot fold is left as it is."""
    jobs = [job for job in entry.get("jobs") or [] if isinstance(job, dict)]
    stating = [job for job in jobs if states_it(job)]
    if not stating:
        return [], []
    if states_it(entry):
        return [], [f"{where}: the entry and its job(s) both state cleanup_retention - left as they are; "
                    "check-objects names any that disagree"]
    if any(isinstance(job.get(FIELD), dict) for job in stating):
        return [], [f"{where}: a job states the per-type object already - left for a person to move"]
    try:
        values = {str(job.get("job") or "?"): parse(job, context=f"{where}.{job.get('job')}") for job in stating}
    except CleanupRetentionError as exc:
        return [], [str(exc)]
    if len(stating) != len(jobs):
        return [], [f"{where}: some jobs state no cleanup_retention - left as they are"]
    if len(set(values.values())) == 1:
        folded: int | dict[str, Any] = next(iter(values.values()))
        effect = "unchanged: every job stated the same window"
    else:
        windows: dict[str, Any] = {}
        engine = str(entry.get("db_type") or "")
        for job in stating:
            kinds = types_a_job_writes(engine, job)
            if kinds is None:
                return [], [f"{where}.{job.get('job')}: which file types this job writes is not known "
                            f"({job.get('script')!r}, BACKUP_LEVEL {(job.get('env') or {}).get('BACKUP_LEVEL')!r}) "
                            "- left as it is"]
            for kind in kinds:
                windows[kind] = values[str(job.get("job") or "?")]
        folded = {**windows, DEFAULT_KEY: max(values.values())}
        effect = (f"CHANGES: the shortest job window ({describe(min(values.values()))}) cut every type in this "
                  f"folder; now each type keeps its own job's window ({values})")
    entry[FIELD] = folded
    for job in stating:
        for name in (FIELD, *LEGACY_FIELDS):
            job.pop(name, None)
    return [{"where": where, "field": FIELD, "value": folded, "effect": effect}], []


def fold_into_entries(document: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """:func:`fold_entry` over every backup entry of a restore_config document."""
    changes: list[dict[str, Any]] = []
    conflicts: list[str] = []
    section = document.get("backup_restore") if isinstance(document, dict) else None
    for entry in (section or {}).get("backups") or []:
        if isinstance(entry, dict):
            done, refused = fold_entry(entry, where=f"backups[{entry.get('backup_id')}]")
            changes += done
            conflicts += refused
    return changes, conflicts


def removed_by_retention(retention: Retention, file_type: str, ended_at: str | None, *,
                         now_utc: str) -> str:
    """Why a stored file that is gone was its retention's to remove, or ``""`` (0.35.0, 1.233).

    The backup scripts prune their own folders - ``find``, RMAN, the chain sweep - and tell the
    catalog nothing, so a scrub that finds such a file gone must not read it as an attack: three
    of them engaged the lockdown. A file past its type's window was the window's; one still inside
    it, or kept (``keep``), going is the signal. ``0`` is no age gate: any file may go by the chain
    rule. ``ended_at`` / ``now_utc`` are ISO UTC text, compared as such.
    """
    window = retention.window_for(file_type)
    if window is None or not ended_at:
        return ""
    if window == 0:
        return f"retention: {file_type} has no age gate (0), the chain rule removes it"
    from datetime import datetime, timedelta

    def _moment(text: str) -> datetime:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T"))

    try:
        past = _moment(ended_at) < _moment(now_utc) - timedelta(seconds=int(window))
    except ValueError:
        return ""
    return f"retention: past its {file_type} window ({describe(window)})" if past else ""


def describe_config(value: Any) -> str:
    """A stated retention in words, for a page: ``8.0 days`` or ``FULL 14.0 days, LOG 7.0 days, default ...``."""
    if isinstance(value, dict):
        return ", ".join(f"{key} {describe_window(_window(item, where=FIELD))}" for key, item in value.items())
    try:
        return describe(int(value))
    except (TypeError, ValueError):
        return str(value)


def as_days(seconds: int) -> float:
    """Seconds as days, for the retention planner that still thinks in whole days."""
    return float(seconds) / 86400.0


def describe(seconds: int) -> str:
    """A short human reading, for a log line or an error: ``691200s (8.0 days)``."""
    if not seconds:
        return "0s (no age gate)"
    return f"{seconds}s ({as_days(seconds):.1f} days)"
