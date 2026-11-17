"""Which RMAN pieces an Oracle restore from the repository copies (0.40.0, 1.380).

An RMAN folder is flat and its names are RMAN's own - level 0, level 1, archived logs, controlfile
autobackups and spfiles side by side - so the chain cannot be read from the names on the repository,
as it is for PostgreSQL and SQL Server. From the server the copy asks the source's RMAN (`RESTORE
DATABASE PREVIEW`, ``common.backup_copy.oracle_chain_include``); from the repository the source may be
the very thing that is gone. The operator, 2026-11-16: **the chain is recorded at backup time** - after
each backup of an entry with ``record_chain`` the source's RMAN is asked, and its answer kept on the
backup run (``backup_runs.rman_chain_json``) - **with a point in time too**, and **a restore that finds
no recorded chain refuses** before it copies anything: never the whole folder (review 0.25.0, G2.9).

This reads those answers, pure: the runs come in, the pieces go out, with a sentence saying which.

For a moment ``M`` (the newest: the newest run's end):

1. ``E`` - the first finished run that ended at or after ``M``: its logs are the ones that reach it.
   None for a moment no backup has passed yet: the logs that reach it are not backed up.
2. ``L`` - the newest level 0 (a run of level ``full``) that ended at or before ``M`` (the newest:
   before ``E``).
3. ``B`` - the newest run from ``L`` to ``E`` whose recorded chain holds ``L``'s pieces: RMAN's own
   answer of what the chain from that level 0 was, when it was asked.
4. The pieces: ``B``'s recorded chain, and every file of the runs after ``B`` up to ``E`` - the
   archived logs (and level 1s) taken since, which belong to the same chain. RMAN's ``UNTIL TIME``
   then decides what of them the moment needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from dbabrain.lib import errors
from dbabrain.lib.backup_kinds import FULL


class ChainNotRecorded(errors.RequestError):
    """No recorded RMAN chain answers the restore: it refuses before anything is copied."""


@dataclass(frozen=True)
class ChainRun:
    """One finished backup run of the source, as the catalog holds it."""

    backup_run_id: int
    level: str
    ended_at: str
    #: The RMAN chain recorded after the run, as piece names; ``None`` when none was.
    pieces: tuple[str, ...] | None
    #: Why none was recorded: the RMAN question failed, or the entry did not record chains then.
    error: str
    #: The names of the files the run itself wrote.
    files: tuple[str, ...]


@dataclass(frozen=True)
class ChosenChain:
    pieces: tuple[str, ...]
    level0_run_id: int
    recorded_run_id: int
    reaches_run_id: int
    reaches_at: str
    said: str


def _at(text: str) -> datetime:
    value = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def runs_from_rows(runs: Iterable[dict[str, Any]], files: Iterable[dict[str, Any]]) -> list[ChainRun]:
    """``ChainRun`` s from the catalog's rows: a run's ``rman_chain_json`` (``{"pieces": [...]}`` or
    ``{"error": "..."}``, NULL when none was recorded), and its files' ``backup_run_id`` and name."""
    import json

    by_run: dict[int, list[str]] = {}
    for row in files:
        by_run.setdefault(int(row["backup_run_id"]), []).append(str(row["file_name"]))
    chosen: list[ChainRun] = []
    for row in runs:
        recorded: dict[str, Any] = {}
        if row.get("rman_chain_json"):
            try:
                recorded = json.loads(str(row["rman_chain_json"]))
            except ValueError:
                recorded = {"error": "its recorded chain does not read as JSON"}
        pieces = recorded.get("pieces")
        run_id = int(row["backup_run_id"])
        chosen.append(ChainRun(
            backup_run_id=run_id, level=str(row.get("level") or ""), ended_at=str(row.get("ended_at") or ""),
            pieces=tuple(str(p) for p in pieces) if isinstance(pieces, list) else None,
            error=str(recorded.get("error") or ("" if isinstance(pieces, list) else "no chain recorded")),
            files=tuple(by_run.get(run_id, ()))))
    return chosen


def choose(runs: Iterable[ChainRun], *, point_in_time: datetime | None = None,
           server_id: str = "") -> ChosenChain:
    """The pieces that restore ``server_id`` to ``point_in_time`` (naive UTC; ``None``: the newest)."""
    who = server_id or "the source"
    finished = sorted((run for run in runs if run.ended_at), key=lambda run: (_at(run.ended_at), run.backup_run_id))
    if not finished:
        raise ChainNotRecorded(f"{who} has no finished Oracle backup run in the catalog.")
    if point_in_time is None:
        reaches = finished[-1]
        moment = _at(reaches.ended_at)
        asked = "the newest backup"
    else:
        moment = point_in_time
        asked = f"{point_in_time:%Y-%m-%d %H:%M:%S} UTC"
        later = [run for run in finished if _at(run.ended_at) >= moment]
        if not later:
            raise ChainNotRecorded(
                f"no backup run of {who} ended at or after {asked} (the newest ended {finished[-1].ended_at}): "
                "the archived logs that reach it are not backed up yet.")
        reaches = later[0]
    upto = [run for run in finished if _at(run.ended_at) <= _at(reaches.ended_at)]
    level0 = [run for run in upto if run.level == FULL and _at(run.ended_at) <= moment]
    if not level0:
        raise ChainNotRecorded(f"no level 0 backup of {who} ended at or before {asked}: there is no chain to start from.")
    base = level0[-1]
    since_base = [run for run in upto if _at(run.ended_at) >= _at(base.ended_at)]
    recorded = None
    for run in reversed(since_base):
        if run.pieces is not None and set(base.files) & set(run.pieces):
            recorded = run
            break
    if recorded is None:
        why = "; ".join(f"run {run.backup_run_id}: {run.error}" for run in since_base[-3:] if run.pieces is None)
        raise ChainNotRecorded(
            f"no RMAN chain from {who}'s level 0 (backup run {base.backup_run_id}, ended {base.ended_at}) to "
            f"{asked} was recorded at backup time" + (f" - {why}" if why else "")
            + ". The restore from the repository reads the chain RMAN named after a backup; it never copies the "
              "whole folder instead.")
    after = [run for run in since_base if _at(run.ended_at) > _at(recorded.ended_at)]
    pieces = list(recorded.pieces or ())
    for run in after:
        pieces.extend(name for name in run.files if name not in pieces)
    said = (f"the RMAN chain recorded after backup run {recorded.backup_run_id} (level 0: run {base.backup_run_id})"
            + (f" + {sum(len(run.files) for run in after)} file(s) of {len(after)} later run(s)" if after else "")
            + f", up to run {reaches.backup_run_id} ended {reaches.ended_at} - {len(pieces)} piece(s) for {asked}")
    return ChosenChain(pieces=tuple(pieces), level0_run_id=base.backup_run_id, recorded_run_id=recorded.backup_run_id,
                       reaches_run_id=reaches.backup_run_id, reaches_at=reaches.ended_at, said=said)
