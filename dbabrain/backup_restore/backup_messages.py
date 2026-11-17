"""A backup's messages, one step each, in the order they happen (0.40.0, 1.374).

The operator, 2026-11-15, reading a night's DIFF (translated): *these messages are not quite right. It
should be: 1. backup process start, 2. backup on server_id start, 3. backup on server_id done, 4. copy
file from server_id to repo start, 5. copy done, 6. encrypt start, 7. encrypt done, 8. clear the plain
files from server_id and the repo - done, 9. backup process done.* On 2026-11-16 he decided those steps
for every level alike - *full, diff, log have shared one code path from the start*: what a LOG job sends
is its ``notify``'s to decide, never a rule here - and added a step: *the repository's upkeep a step of
its own, 9, and 9 becomes 10*.

What the chat got before: no start or end of the process, the repository's *Copy + encrypt started*
before the backup's own *done*, step numbers that were ranges (``2-3``, ``2-5``), a ``next:`` line the
following messages then numbered differently. Each step is now its own message::

    SRV_DIFF · 1. Backup process started
    SRV · diff · started 2026-11-15 03:00:16 +07
    steps: 2-3 backup on SRV, 4-5 copy -> REPO, 6-7 encrypt (age, on the way), 8 plain files cleared,
           9 repository upkeep, 10 done

    SRV_DIFF · 2. Backup on SRV started
    SRV_DIFF · 3. Backup on SRV done
    SRV_DIFF · 4. Copy to REPO started          SRV_DIFF · 6. Encrypt started
    SRV_DIFF · 5. Copy to REPO done             SRV_DIFF · 7. Encrypt done
    SRV_DIFF · 8. Plain files cleared
    SRV_DIFF · 9. Repository upkeep done
    SRV_DIFF · 10. Backup process done

Steps 1-3 are the backup run's; 4-10 the offload pass's that takes the run's files once the run has
ended. A run with nothing for the repository (its entry offloads nothing, or it wrote no file to copy)
ends itself with 10. With the copy encrypting on the way, 4 and 6 go together and so do 5 and 7 - the
operator allowed it (*where encrypt and copy run at the same time, two messages can be sent at once*).

Words only, as ``restore_messages``: what happened is decided where it happens; nothing here can
fail a backup.
"""

from __future__ import annotations

from typing import Any

from dbabrain.backup_restore.restore_messages import flow, size_said
from dbabrain.lib.timezone import format_display_text


def _when(text: Any) -> str:
    return format_display_text(str(text)) if text else ""


def _span(started_at: Any, finished_at: Any) -> str:
    """``2026-11-15 03:00:16 +07 → 03:08:27 +07`` - the end's date left out when it is the start's."""
    start, end = _when(started_at), _when(finished_at)
    if not start or not end:
        return start or end
    return f"{start} → {end[11:] if end[:10] == start[:10] else end}"


def plan_line(*, server_id: str, offload: Any) -> str:
    """The steps this entry's config runs, a step it has off said so - read off the config, never assumed."""
    if offload is None or not getattr(offload, "enabled", False):
        return f"steps: 2-3 backup on {server_id}, 4-9 off (no repository for this entry), 10 done"
    repository = getattr(offload, "server_id", "") or "the repository"
    encrypt = "6-7 encrypt (age, on the way)" if getattr(offload, "encrypt", False) else "6-7 encrypt off"
    clear = ("8 plain files cleared (deleted at the source once proven)" if getattr(offload, "delete_source", False)
             else "8 plain files: kept at the source (offload_delete_source false)")
    return (f"steps: 2-3 backup on {server_id}, 4-5 copy -> {repository}, {encrypt}, {clear}, "
            "9 repository upkeep, 10 done")


def process_started(item: Any, *, started_at: str) -> dict[str, Any]:
    return flow(item.backup_id, "1. Backup process started",
                [f"{item.server_id} · {item.job} · started {_when(started_at)}",
                 plan_line(server_id=str(item.server_id), offload=getattr(item, "offload", None))])


def backup_started(item: Any, *, started_at: str) -> dict[str, Any]:
    return flow(item.backup_id, f"2. Backup on {item.server_id} started",
                [f"{item.job} into {getattr(item, 'backup_dir', '') or 'its backup folder'}"])


def backup_done(item: Any, *, result: Any, started_at: str, finished_at: str | None,
                catalog_summary: dict[str, Any] | None, scrub: dict[str, Any] | None,
                anomalies: list[str], markers: list[str], server_metadata: Any,
                chain: dict[str, Any] | None = None) -> dict[str, Any]:
    """Step 3: when, how many files, verified - and each finding on a line of its own; an Oracle entry
    that records its RMAN chain says whether it was kept (1.380)."""
    done = result.status == "done"
    files = (catalog_summary or {}).get("files") or 0
    summary = [_span(started_at, finished_at)] + (
        [f"{files} file(s)", f"{(catalog_summary or {}).get('verified', 0)}/{files} hashed and verified"]
        if files else ["no file written"])
    lines = [" · ".join(part for part in summary if part)]
    if not done:
        lines.append(f"❌ {' '.join(str(result.error_text or 'failed').split())[:300]}")
    if markers:
        lines.append(f"❌ RANSOM MARKERS in {item.backup_dir}: {', '.join(markers[:5])}")
    if anomalies:
        lines.append("⚠ size far from its history: " + "; ".join(anomalies[:3]))
    if scrub:
        changed, gone = scrub.get("mismatch") or [], scrub.get("missing") or []
        if changed or gone:
            names = ", ".join(str(f.get("file")) for f in [*changed, *gone][:5])
            lines.append(f"❌ stored backups changed: {len(changed)}, gone: {len(gone)} - {names}")
        elif scrub.get("unreadable"):
            lines.append(f"⚠ {len(scrub['unreadable'])} stored file(s) could not be re-read")
    if isinstance(server_metadata, dict) and not server_metadata.get("ok", True):
        lines.append(f"⚠ server metadata export failed: {' '.join(str(server_metadata.get('error') or '').split())[:160]}")
    if chain and chain.get("error"):
        # A warning, not a failure (the operator, 2026-11-16: the backup stands): ⚠, as every warning here.
        lines.append(f"⚠ RMAN chain not recorded - {str(chain['error'])[:240]} (a restore from the repository "
                     "needs one recorded by a run from this backup's level 0 on)")
    elif chain:
        lines.append(f"✅ RMAN chain recorded: {chain.get('pieces', 0)} piece(s) - what a restore from the repository copies")
    return flow(item.backup_id, f"3. Backup on {item.server_id} {'done' if done else 'FAILED'}", lines)


def copy_started(backup_id: str, *, files: int, size_bytes: int, repository: str, directory: bool = False,
                 again: bool = False) -> dict[str, Any]:
    size = size_said(size_bytes) or "0 bytes"
    return flow(backup_id, f"4. Copy to {repository} {'started again' if again else 'started'}",
                [f"{files} file(s), {size}"
                 + (" (a base backup directory: its size is read as it is copied)" if directory else "")])


def copy_done(backup_id: str, copied: dict[str, Any], *, repository: str, left: int = 0,
              bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    bad = copied.get("failed") or []
    title = f"5. Copy to {repository} " + ("FAILED" if bad else "done" if not left else "done in part")
    lines = [f"{'❌' if bad else '✅'} {copied.get('verified', 0)}/{copied.get('files', 0)} file(s) · "
             "bytes + SHA-256 = server" + (f" - {bad[0]}"[:300] if bad else "")]
    if bundle:
        # The instance's metadata bundle, carried beside the backup (1.376) - sealed, never in the clear.
        lines.append(f"❌ instance metadata bundle not carried to {bundle.get('path')} - {bundle['error']}"[:400]
                     if bundle.get("error") else
                     f"✅ instance metadata bundle -> {bundle.get('path')} · sealed (age key #{bundle.get('key_id')})")
    if left:
        lines.append(f"- {left} more file(s) of this backup go in the next pass (offload_files_per_pass)")
    return flow(backup_id, title, lines)


def encrypt_started(backup_id: str, *, on_the_way: bool) -> dict[str, Any]:
    return flow(backup_id, "6. Encrypt started",
                ["age, on the way - each file sealed as it is copied" if on_the_way
                 else "age, at the repository - each proven copy sealed, then its plain copy removed"])


def encrypt_done(backup_id: str, encrypted: dict[str, Any], *, key_until: str = "", waiting: int = 0,
                 repository: str = "") -> dict[str, Any]:
    """Step 7. ``waiting``: files an earlier pass's ``age`` is still writing on the repository - not a
    failure; this pass ends, and the pass that finishes them says this step again (the operator, 2026-11-16)."""
    bad = encrypted.get("failed") or []
    if waiting and not bad:
        return flow(backup_id, "7. Encrypt waiting",
                    [f"⏳ {waiting} file(s) still being encrypted on {repository or 'the repository'} by an earlier "
                     "pass - steps 7 and 8 finish in a later pass",
                     f"✅ {encrypted.get('encrypted', 0)} sealed so far" if encrypted.get("encrypted") else ""])
    return flow(backup_id, f"7. Encrypt {'FAILED' if bad else 'done'}",
                [f"{'❌' if bad else '✅'} {encrypted.get('encrypted', 0)}/{encrypted.get('rows', 0)} file(s) · "
                 f"age key #{encrypted.get('key_id')}" + (f" (encrypts until {key_until[:10]})" if key_until else "")
                 + " · decrypted back = server" + (f" - {bad[0]}"[:300] if bad else "")])


def plain_cleared(backup_id: str, *, server_id: str, delete_source: bool, released: dict[str, Any] | None,
                  own_files: int, own_released: int, repository: str, repository_plain: int | None,
                  locked: bool = False, waiting: bool = False) -> dict[str, Any]:
    """Step 8: THIS backup's plain files - at the source and at the repository - as found, not assumed.
    Earlier backups' files are step 9's."""
    bad = (released or {}).get("refused") or []
    lines = []
    if not delete_source:
        lines.append(f"- at the source: kept on {server_id} (offload_delete_source false)")
    elif locked:
        lines.append(f"- at the source: kept on {server_id} - the lockdown is on; deleted once it is off")
    else:
        kept = own_files - own_released
        lines.append(f"{'❌' if bad else '✅'} at the source: {own_released} of {own_files} deleted on {server_id}, "
                     "file by file" + (f"; {kept} kept - the newest chain stays on the server" if kept and not bad else "")
                     + (f" - {bad[0]}"[:300] if bad else ""))
    if repository_plain is None:
        lines.append(f"- at {repository}: kept plain (encryption off for this entry)")
    elif repository_plain and waiting:
        lines.append(f"⏳ at {repository}: {repository_plain} file(s) of this backup still plain - cleared once step 7 "
                     "is done, in a later pass")
    elif repository_plain:
        lines.append(f"❌ at {repository}: {repository_plain} file(s) of this backup still plain")
    else:
        lines.append(f"✅ at {repository}: no plain file of this backup - each is .age")
    if waiting and repository_plain:
        return flow(backup_id, "8. Plain files waiting", lines)
    failed = bool(bad) or bool(repository_plain)
    return flow(backup_id, f"8. Plain files {'NOT cleared' if failed else 'cleared'}", lines)


def repository_upkeep(backup_id: str, *, server_id: str, repository: str, offload: Any,
                      earlier_released: int = 0, release_refused: list[str] | None = None,
                      expired: dict[str, Any] | None = None, late: dict[str, Any] | None = None,
                      check: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    """Step 9 (added by the operator, 2026-11-16): what the pass did besides this backup - earlier backups
    released at the source, copies past their retention, copies sealed late, the repository check. Each
    part says its work, ``nothing``, or ``off`` as its config has it; sent alone by a pass that copied no
    backup."""
    lines = []
    refused = release_refused or []
    if not getattr(offload, "delete_source", False):
        lines.append("- earlier backups at the source: off (offload_delete_source false)")
    elif earlier_released or refused:
        lines.append(f"{'❌' if refused else '✅'} earlier backups at the source: {earlier_released} file(s) deleted on "
                     f"{server_id}, no longer the newest chain" + (f" - {refused[0]}"[:300] if refused else ""))
    else:
        lines.append("- earlier backups at the source: nothing")
    shortest = getattr(offload, "shortest_retention", None)
    if callable(shortest) and shortest() is None:
        lines.append("- retention: off (offload_retention keeps every copy)")
    elif expired and (expired.get("expired") or expired.get("refused")):
        bad = expired.get("refused") or []
        lines.append(f"{'❌' if bad else '✅'} retention: {expired.get('expired', 0)} copy(ies) past their window "
                     f"deleted on {repository}" + (f" - {bad[0]}"[:300] if bad else ""))
    else:
        lines.append("- retention: nothing past its window")
    if not getattr(offload, "encrypt", False):
        lines.append("- late encryption: off (offload_encrypt false)")
    elif late and (late.get("encrypted") or late.get("failed")):
        bad = late.get("failed") or []
        lines.append(f"{'❌' if bad else '✅'} late encryption: {late.get('encrypted', 0)} earlier copy(ies) sealed on "
                     f"{repository}" + (f" - {bad[0]}"[:300] if bad else ""))
    else:
        lines.append("- late encryption: nothing")
    if not getattr(offload, "repository_check", False):
        lines.append("- repository check: off (offload_repository_check false)")
    elif not check:
        lines.append("- repository check: not run")
    elif check.get("skipped"):
        lines.append("- repository check: next pass (another offload of this server is writing there)")
    else:
        lines.append(f"✅ repository check: clean · {check.get('files', 0)} file(s)" if check.get("clean")
                     else f"❌ repository check: {check.get('message', '')}"[:400])
    if error:
        lines.append(f"❌ {error}"[:400])
    failed = bool(error) or any(line.startswith("❌") for line in lines)
    return flow(backup_id, f"9. Repository upkeep {'FAILED' if failed else 'done'}", lines)


def process_done(backup_id: str, *, started_at: Any, finished_at: Any, failed_step: str = "",
                 where: list[str] | None = None, later: str = "", error: str = "") -> dict[str, Any]:
    """Step 10: the whole process, from step 1's start; where the backup is now; what a later pass finishes."""
    lines = [_span(started_at, finished_at)]
    lines.extend(where or [])
    if later:
        lines.append(f"⏳ {later}")
    if error:
        lines.append(f"❌ {error}"[:400])
    title = "10. Backup process done" if not failed_step else f"10. Backup process FAILED at step {failed_step}"
    return flow(backup_id, title, lines)
