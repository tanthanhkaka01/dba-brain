"""A restore's messages follow its flow, as a backup's do (0.39.0, 1.370).

The operator, 2026-11-11: *backup and restore are many steps now and the messages do not follow
them* - the backup got its format that day (1.306: the id and the step on the first line, one line
per step, no JSON). And on 2026-11-13: *restore is like backup - it must message every step of its
flow the same way; restore has restore full -> diff -> log as well, a message for each.*

A restore's Telegram messages were still the older shape - a fixed header and ``key=value`` lines,
the same for every step - with one line for the whole of full, differential and logs. These are
its flow messages, in the backup's format::

    APP_TO_DRILL · Restore started
    SRC-01 -> DRILL-01 · to the newest backup
    next: 1. copy, 2. restore full -> diff -> log, 3. check, 4. clean up

    APP_TO_DRILL · 1. Copy done
    ✅ 1. Copy 48 file(s) · 104.5 MB -> DRILL-01 · decrypted from the repository's encrypted copies

    APP_TO_DRILL · 2. Restore full done (1 of 3)
    ✅ APPDB as APPDB_COPY: full APPDB_FULL.bak (backup finished ...) - left restoring for the next step · 41 s

    APP_TO_DRILL · 3. Check done
    ✅ 3. Check 1 database(s) open, 0 unusable

    APP_TO_DRILL · 4. Clean up done
    ✅ 4. Plain files 48/48 deleted on DRILL-01 (104.5 MB)

    APP_TO_DRILL · Restore done
    ...

Each function answers the two metadata keys ``events._format_flow_message`` renders -
``flow_title`` and ``flow_lines`` - and ``telegram: False`` for a step whose start is not a message
of its own (*a short step one line, a long step a start and a done*): the run row and the log file
still record it. Words only: what happened is decided where it happens (``restore_by_id``,
``workflow``); nothing here can fail a restore.
"""

from __future__ import annotations

from typing import Any

#: The steps of a restore, as its messages number them.
COPY, RESTORE, CHECK, CLEANUP = "1. Copy", "2. Restore", "3. Check", "4. Clean up"
NEXT = "next: 1. copy, 2. restore full -> diff -> log, 3. check, 4. clean up"
NEXT_COPY_ONLY = "next: 1. copy - nothing is restored"

#: What each restore command is called in a title.
STEP_NAMES = {"restore-full": "full", "restore-diff": "diff", "restore-log": "log"}


def size_said(size_bytes: Any) -> str:
    """A number of bytes as a person reads it: ``104.5 MB``; ``""`` for none."""
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return ""
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def flow(restore_id: str, title: str, lines: list[str], *, telegram: bool = True) -> dict[str, Any]:
    """The flow fields of one message; ``telegram`` false keeps a short step's start off Telegram."""
    fields: dict[str, Any] = {"flow_title": f"{restore_id} · {title}",
                              "flow_lines": [" ".join(str(line).split()) for line in lines if str(line).strip()]}
    if not telegram:
        fields["telegram"] = False
    return fields


def started(restore_id: str, *, source: str, target: str, moment: str, copy_only: bool = False,
            from_repository: bool = False) -> dict[str, Any]:
    where = f"{source} -> {target or source}" + (" · from the repository" if from_repository else "")
    return flow(restore_id, "Copy only started" if copy_only else "Restore started",
                [f"{where} · {'to ' + moment if moment else 'to the newest backup'}",
                 NEXT_COPY_ONLY if copy_only else NEXT])


def copy_started(restore_id: str, *, source: str, target: str, from_repository: bool) -> dict[str, Any]:
    return flow(restore_id, f"{COPY} started",
                [("the repository's copies of " if from_repository else "") + f"{source} -> {target}"])


def copy_done(restore_id: str, transferred: dict[str, Any], *, target: str) -> dict[str, Any]:
    parts = [f"✅ {COPY} {transferred.get('copied', 0)} file(s)", size_said(transferred.get("bytes_copied")) + f" -> {target}"]
    if transferred.get("encrypted"):
        parts.append("decrypted from the repository's encrypted copies")
    parts.append("bytes + SHA-256 proven")
    if transferred.get("skipped"):
        parts.append(f"{transferred['skipped']} already there and proven")
    if transferred.get("removed_absent_at_source"):
        parts.append(f"{transferred['removed_absent_at_source']} removed (gone at the source)")
    lines = [" · ".join(part for part in parts if part.strip())]
    if transferred.get("written_after_the_plan"):
        lines.append(f"⚠ {len(transferred['written_after_the_plan'])} file(s) written at the source after the "
                     "plan were left there")
    return flow(restore_id, f"{COPY} done", lines)


def restore_started(restore_id: str, *, plan: str, moment: str, continued: str = "") -> dict[str, Any]:
    return flow(restore_id, f"{RESTORE} started",
                [f"{plan or 'nothing more to apply'} · {moment}", f"↪ {continued}" if continued else ""])


def step_done(restore_id: str, *, op: str, number: int, total: int, said: str, seconds: int) -> dict[str, Any]:
    """One step of the chain - the full, the differential, the logs - as it finishes."""
    return flow(restore_id, f"{RESTORE} {STEP_NAMES.get(op, op)} done ({number} of {total})",
                [f"✅ {said} · {seconds} s"])


def restore_done(restore_id: str, *, steps: int, seconds: int) -> dict[str, Any]:
    # The last step's own message already said it: the run row keeps this one, Telegram does not.
    return flow(restore_id, f"{RESTORE} done", [f"✅ {steps} step(s) · {seconds} s"], telegram=False)


def check_started(restore_id: str) -> dict[str, Any]:
    return flow(restore_id, f"{CHECK} started", ["the restored database(s): can each be opened, and is it consistent"])


def check_done(restore_id: str, *, checked: Any, failed: Any) -> dict[str, Any]:
    bad = int(failed or 0)
    return flow(restore_id, f"{CHECK} {'done' if not bad else 'FAILED'}",
                [f"{'❌' if bad else '✅'} {CHECK} {checked} database(s) checked, {bad} unusable"])


def cleanup_started(restore_id: str) -> dict[str, Any]:
    return flow(restore_id, f"{CLEANUP} started", ["the staging folder"], telegram=False)


def cleanup_done(restore_id: str, *, target: str, plain: dict[str, Any] | None, pruned: dict[str, Any],
                 kept: int = 0) -> dict[str, Any]:
    lines = []
    if plain is not None:
        whole = not plain.get("error") and int(plain.get("deleted", 0)) + int(plain.get("already_gone", 0)) \
            >= int(plain.get("plain_files", 0))
        lines.append(f"{'✅' if whole else '❌'} {CLEANUP}: plain files {plain.get('deleted', 0)}/"
                     f"{plain.get('plain_files', 0)} deleted on {target} ({size_said(plain.get('bytes_deleted'))})"
                     + (f", {plain['earlier_runs']} of them left by an earlier run" if plain.get("earlier_runs") else "")
                     + (f" - {plain['error']}"[:300] if plain.get("error") else ""))
    else:
        lines.append(f"- {CLEANUP}: nothing was decrypted for this restore")
    lines.append(f"{'❌' if pruned.get('error') else '-'} staged backups past retention: {pruned.get('pruned', 0)} removed"
                 + (f" ({pruned['skipped']})" if pruned.get("skipped") else "")
                 + (f", {kept} kept - not in the catalog" if kept else "")
                 + (f" - {pruned['error']}"[:300] if pruned.get("error") else ""))
    return flow(restore_id, f"{CLEANUP} done", lines)


def share_step(restore_id: str, *, op: str, result: str, number: int, total: int, said: str,
               seconds: int | None = None, reason: str = "") -> dict[str, Any]:
    """One file of a share-driven restore as it went in - or was passed over (0.39.0, 1.370 / 1.367):
    the full, the differential, each log, numbered within its database."""
    word = {"applied": "done", "skipped": "skipped"}.get(result, result)
    mark = {"applied": "✅", "skipped": "↷"}.get(result, "❌")
    tail = f" · {seconds} s" if seconds is not None and result == "applied" else ""
    return flow(restore_id, f"{RESTORE} {STEP_NAMES.get(op, op)} {word} ({number} of {total})",
                [f"{mark} {said}{tail}", f"- {reason}"[:300] if reason else ""])


def share_cleanup_done(restore_id: str, *, considered: int, deleted: int, kept: int) -> dict[str, Any]:
    """The share-driven restore's retention cleanup: what it looked at and what it removed."""
    return flow(restore_id, f"{CLEANUP} done",
                [f"- staged backups past retention: {deleted} removed of {considered} considered, {kept} kept"])


def note(restore_id: str, title: str, line: str) -> dict[str, Any]:
    """A step the entry has off, or has nothing for - said once, as a line of its own."""
    return flow(restore_id, title, [f"- {line}"])


def finished(restore_id: str, *, status: str, copy_only: bool = False, restored: dict[str, Any] | None = None,
             warnings: list[str] | None = None, error: str = "", seconds: int | None = None) -> dict[str, Any]:
    """The END or ERROR message: what went in, whether it opened, where it stopped - or why it failed."""
    said = restored or {}
    noun = "Copy only" if copy_only else "Restore"
    if status != "done":
        return flow(restore_id, f"{noun} FAILED", [f"❌ {error or 'failed'}"[:1500],
                                                   f"{seconds} s" if seconds is not None else ""])
    lines = []
    if copy_only:
        lines.append("✅ the files are staged and proven on the target - nothing restored")
    for label, key in (("restored", "restored"), ("checked", "verified"), ("stopped", "landed"),
                       ("continued", "continued"), ("plain files deleted", "plain_deleted")):
        if said.get(key):
            lines.append(f"{'↪' if key == 'continued' else '✅'} {label}: {said[key]}")
    lines.extend(f"⚠ {warning}" for warning in warnings or [])
    if seconds is not None:
        lines.append(f"{seconds} s")
    return flow(restore_id, f"{noun} done" + (" with a warning" if warnings else ""), lines)
