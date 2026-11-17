"""A restore's messages: eleven steps, said by the workflow that calls them (0.40.0, 1.375).

The operator, 2026-11-15: *a restore's messages step by step, concrete, like the backup's - each log one
message, 10.1, 10.2 ...; full and diff two messages each, a start and a done; the metadata and the
certificate always a message, also when there is none*. The steps he confirmed on 2026-11-16, the same
numbers for every engine and a step that does not apply still said with why::

    APP_TO_DRILL · 1. Restore process started
    SRC-01 -> DRILL-01 · to the newest backup
    steps: 2 copy -> DRILL-01, 3 metadata before, 4 certificate, 5 full, 6 diff, 7 log, 8 check, ...

    APP_TO_DRILL · 2. Copy to DRILL-01 started / done
    APP_TO_DRILL · 3. Instance metadata before the restore: not restored
    APP_TO_DRILL · 4. Certificate: none
    APP_TO_DRILL · 5.1 Restore full APPDB_COPY started / done
    APP_TO_DRILL · 6.1 Restore diff APPDB_COPY started / done
    APP_TO_DRILL · 7.1 Restore log APPDB_LOG_0001.trn done        (one per file)
    APP_TO_DRILL · 8. Check started / done
    APP_TO_DRILL · 9. Instance metadata after the restore: not restored
    APP_TO_DRILL · 10. Plain files cleared
    APP_TO_DRILL · 11. Restore process done

Each function answers the two metadata keys ``events._format_flow_message`` renders - ``flow_title``
and ``flow_lines``. Words only, read by ``workflow._RestoreSay`` alone: what happened is decided where it
happens (the steps of ``restore_by_id`` and ``restore_database``); nothing here can fail a restore.
"""

from __future__ import annotations

from typing import Any

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


# --------------------------------------------------------------------------- #
# The eleven steps (0.40.0, 1.375) - confirmed by the operator 2026-11-16
# --------------------------------------------------------------------------- #
#: Each step's name, as its message says it; the same numbers for every engine.
STEP_TITLES = {2: "Copy", 3: "Instance metadata before the restore", 4: "Certificate", 5: "Restore full",
               6: "Restore diff", 7: "Restore log", 8: "Check", 9: "Instance metadata after the restore",
               10: "Plain files cleared", 11: "Restore process done"}


def plan_line(*, target: str, remote: bool, from_repository: bool, copy_only: bool) -> str:
    """The steps this restore runs, each as its entry has it."""
    copy = (f"2 copy -> {target}" + (" (decrypted from the repository)" if from_repository else "")
            if remote else "2 copy: none (the target reads the source's own folder)")
    if copy_only:
        return f"steps: {copy}, 3-10 off (copy only - nothing restored), 11 done"
    return (f"steps: {copy}, 3 metadata before, 4 certificate, 5 full, 6 diff, 7 log, 8 check, "
            f"9 metadata after, 10 plain files cleared, 11 done")


def process_started(restore_id: str, *, source: str, target: str, moment: str, plan: str,
                    copy_only: bool = False, from_repository: bool = False) -> dict[str, Any]:
    where = f"{source} -> {target or source}" + (" · from the repository" if from_repository else "")
    return flow(restore_id, "1. Copy only process started" if copy_only else "1. Restore process started",
                [f"{where} · {'to ' + moment if moment else 'to the newest backup'}", plan])


def none(restore_id: str, number: int, reason: str) -> dict[str, Any]:
    """A step that does not apply to this restore - still said, with why (the operator, 2026-11-15)."""
    return flow(restore_id, f"{number}. {STEP_TITLES[number]}: none", [f"- {reason}"])


def copy_step(restore_id: str, *, target: str, event: str, transferred: dict[str, Any] | None = None,
              from_repository: bool = False, source: str = "") -> dict[str, Any]:
    if event == "start":
        return flow(restore_id, f"2. Copy to {target} started",
                    [f"from the repository: the encrypted copies of {source}, decrypted on the way" if from_repository
                     else f"from {source}: the backup files, each proven on arrival"])
    done = transferred or {}
    parts = [f"✅ {done.get('copied', 0)} file(s)", (size_said(done.get("bytes_copied")) or "0 bytes")]
    if done.get("encrypted"):
        parts.append("decrypted from the repository's encrypted copies")
    parts.append("bytes + SHA-256 proven")
    if done.get("skipped"):
        parts.append(f"{done['skipped']} already there and proven")
    if done.get("removed_absent_at_source"):
        parts.append(f"{done['removed_absent_at_source']} removed (gone at the source)")
    lines = [" · ".join(parts)]
    if done.get("written_after_the_plan"):
        lines.append(f"⚠ {len(done['written_after_the_plan'])} file(s) written at the source after the plan "
                     "were left there")
    return flow(restore_id, f"2. Copy to {target} done", lines)


def metadata_step(restore_id: str, *, number: int, report: dict[str, Any]) -> dict[str, Any]:
    """Steps 3 and 9: replayed, not restored and why, or FAILED (a warning: the databases stand)."""
    title = STEP_TITLES[number]
    status = str(report.get("status") or "")
    if status == "OFF":
        return flow(restore_id, f"{number}. {title}: not restored", [f"- {report.get('reason')}"])
    if report.get("ok", True) and status != "SKIPPED":
        warnings = [str(w) for w in report.get("warnings") or []]
        return flow(restore_id, f"{number}. {title} done",
                    [f"✅ replayed onto {report.get('target') or 'the target'}"
                     + (f" · {report['status']}" if report.get("status") else "")]
                    # Each WARN said, not only its word: what was skipped and why.
                    + [f"⚠ {warning}" for warning in warnings])
    return flow(restore_id, f"{number}. {title} FAILED",
                [f"❌ {' '.join(str(report.get('error') or status or 'not replayed').split())[:300]}",
                 "- the restore itself is unaffected"])


def certificate_step(restore_id: str, *, result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    if status == "imported":
        return flow(restore_id, "4. Certificate restored",
                    [f"✅ {result.get('name')} from the vault of dbabrain, imported on the target; its files removed"])
    if status == "none":
        return none(restore_id, 4, "none needed - the backup is not encrypted")
    return none(restore_id, 4, f"{result.get('engine') or 'this engine'} has no certificate step")


def _chain_said(facts: dict[str, Any]) -> str:
    database, landing = str(facts.get("database") or ""), str(facts.get("landing") or "")
    if not database and not landing:
        said = str(facts.get("summary") or facts.get("op") or "a step")
    else:
        said = database if not landing or landing.lower() == database.lower() else f"{database} as {landing}"
        files = facts.get("files") or []
        if files:
            said += f": {', '.join(files[:3])}" + (f" (+{len(files) - 3})" if len(files) > 3 else "")
    ends = facts.get("finished") or []
    if len(ends) == 1:
        said += f" (backup finished {ends[0]})"
    elif ends:
        said += f" (backups finished {ends[0]} .. {ends[-1]})"
    if facts.get("stopat"):
        said += f", stopped at {facts['stopat']}"
    if landing:
        said += " - recovered, the database is open" if facts.get("recovered") else " - left restoring for the next step"
    return said


def chain_step(restore_id: str, *, number: str, kind: str, event: str, facts: dict[str, Any],
               seconds: int | None = None) -> dict[str, Any]:
    """``5.1 Restore full APPDB started`` / ``... done`` - each database its own pair (the operator, 2026-11-15)."""
    step = 5 if kind == "full" else 6
    who = str(facts.get("landing") or facts.get("database") or "").strip()
    title = f"{number} {STEP_TITLES[step]}" + (f" {who}" if who else "") + (" started" if event == "start" else " done")
    if event == "start":
        return flow(restore_id, title, [_chain_said(facts)])
    return flow(restore_id, title, [f"✅ {_chain_said(facts)}" + (f" · {seconds} s" if seconds is not None else "")])


def log_step(restore_id: str, *, number: str, facts: dict[str, Any], seconds: int | None = None,
             result: str = "applied", reason: str = "") -> dict[str, Any]:
    """``7.3 Restore log APPDB_LOG_0003.trn done`` - one message per file, numbered under the log step."""
    files = facts.get("files") or []
    name = files[0] if len(files) == 1 else f"{len(files)} files" if files else "WAL replay"
    word = {"applied": "done", "skipped": "skipped"}.get(result, "FAILED")
    mark = {"applied": "✅", "skipped": "↷"}.get(result, "❌")
    position = f" ({facts['log_number']} of {facts['log_total']} logs)" if facts.get("log_total") else ""
    return flow(restore_id, f"{number} {STEP_TITLES[7]} {name} {word}",
                [f"{mark} {_chain_said(facts)}{position}" + (f" · {seconds} s" if seconds is not None else ""),
                 f"- {reason}"[:300] if reason else ""])


def check_step(restore_id: str, *, event: str, checked: Any = None, failed: Any = None) -> dict[str, Any]:
    if event == "start":
        return flow(restore_id, "8. Check started", ["each restored database: can it be opened, is it consistent"])
    bad = int(failed or 0)
    return flow(restore_id, f"8. Check {'done' if not bad else 'FAILED'}",
                [f"{'❌' if bad else '✅'} {checked} database(s) checked, {bad} unusable"])


def plain_step(restore_id: str, *, target: str, cleaned: dict[str, Any]) -> dict[str, Any]:
    """Step 10: the copies this entry staged on the target - deleted now the databases are checked - and
    the staged backups past retention."""
    plain, pruned, kept = cleaned.get("plain"), cleaned.get("pruned") or {}, int(cleaned.get("kept") or 0)
    lines = []
    if plain is not None:
        whole = not plain.get("error") and int(plain.get("deleted", 0)) + int(plain.get("already_gone", 0))             >= int(plain.get("plain_files", 0))
        lines.append(f"{'✅' if whole else '❌'} {plain.get('deleted', 0)} of {plain.get('plain_files', 0)} plain file(s) "
                     f"deleted on {target} ({size_said(plain.get('bytes_deleted')) or '0 bytes'})"
                     + (f", {plain['earlier_runs']} of them left by an earlier run" if plain.get("earlier_runs") else "")
                     + (f" - {plain['error']}"[:300] if plain.get("error") else ""))
    else:
        lines.append("- nothing was decrypted or staged plain for this restore")
    lines.append(f"{'❌' if pruned.get('error') else '-'} staged backups past retention: {pruned.get('pruned', 0)} removed"
                 + (f" ({pruned['skipped']})" if pruned.get("skipped") else "")
                 + (f", {kept} kept - not in the catalog" if kept else "")
                 + (f" - {pruned['error']}"[:300] if pruned.get("error") else ""))
    failed = any(line.startswith("❌") for line in lines)
    return flow(restore_id, f"10. {STEP_TITLES[10]}" + (" - NOT all" if failed else ""), lines)


def process_done(restore_id: str, *, status: str, failed_step: str = "", copy_only: bool = False,
                 restored: dict[str, Any] | None = None, warnings: list[str] | None = None, error: str = "",
                 seconds: int | None = None) -> dict[str, Any]:
    """Step 11: what went in, whether it opened, where it stopped - or the step it failed at, and why."""
    said = finished(restore_id, status=status, copy_only=copy_only, restored=restored, warnings=warnings,
                    error=error, seconds=seconds)
    noun = "Copy only process" if copy_only else "Restore process"
    if status != "done":
        title = f"11. {noun} FAILED" + (f" at step {failed_step}" if failed_step else "")
    else:
        title = f"11. {noun} done" + (" with a warning" if warnings else "")
    return {**said, "flow_title": f"{restore_id} · {title}"}
