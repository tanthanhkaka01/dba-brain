"""Deleting backup files for a cleanup - each one by its own verified command (0.38.0, 1.329).

The one way this app removes a backup file, whatever asks for it: a backup entry's prune, a
restore's staging cleanup, the target import folder's. The rules are :mod:`dbabrain.lib.verified_cleanup`;
this module makes the calls.

* **A file the catalog knows** is one item of ``delete-verified-files``: its name, path, bytes and
  sha256 stated, and deleted on the host only when all of them are true there.
* **A file the catalog does not know is kept and named** - unless the operator, by hand, said
  ``force``: then each is deleted by its own ``delete-file`` (``"force": true``) command, which
  measures the file on the host, deletes it and answers with its bytes and sha256. One command, one
  file: a list is an array here and a call per file there (the operator, 2026-11-11). A scheduled
  run never forces.
* **An emptied backup set's folders** go by ``delete-empty-folders``, deepest first; a folder that
  still holds anything is refused by the command itself.

Nothing here decides *which* files are old: the caller hands the paths, and the facts.
"""

from __future__ import annotations

from typing import Any, Mapping

from dbabrain.lib import verified_cleanup as rules
from dbabrain.transport import common_cli

#: Seconds on top of a command's own timeout before its process is given up on.
_PROCESS_MARGIN_SECONDS = 120


def delete_files(*, host: dict[str, Any], paths: list[str], facts: Mapping[str, Mapping[str, Any]],
                 must_be_under: str, windows: bool = False, force: bool = False, dry_run: bool = False,
                 timeout_seconds: int | None = None) -> dict[str, Any]:
    """Delete ``paths`` on ``host``: the ones ``facts`` knows by their facts, the rest kept and named.

    Answers ``{"deleted", "already_gone", "would_delete", "not_deleted": [{"path", "status",
    "reason"}], "kept": [{"path", "reason"}], "forced": [{"path", "size_bytes", "sha256"}]}``.
    ``force`` deletes each kept file by its own forced command and records what it removed.

    ``timeout_seconds`` is each file's limit on the host; left out, the command's own applies (the
    hash's hour) and this process puts no second clock on it - the app states no default of its own.
    """
    limit = {"timeout_seconds": int(timeout_seconds)} if timeout_seconds else {}
    items, kept = rules.items_with_facts(paths, facts, must_be_under=must_be_under, windows=windows)
    out: dict[str, Any] = {"deleted": [], "already_gone": [], "would_delete": [], "not_deleted": [],
                           "kept": kept, "forced": []}
    if items:
        _ok, answer, error = common_cli.run_allowing_failure(
            "delete-verified-files",
            {"host": host, "files": items, "dry_run": bool(dry_run), **limit},
            timeout_seconds=(int(timeout_seconds) * len(items) + _PROCESS_MARGIN_SECONDS) if timeout_seconds else None)
        read = rules.read_answer(answer, items, error=error)
        for name in ("deleted", "already_gone", "not_deleted"):
            out[name].extend(read[name])
        out["would_delete"].extend(read.get("would_delete") or [])
    if force and kept:
        out["kept"] = []
        for one in kept:
            # One command per file, each its own process: a forced delete never carries a list.
            _ok, answer, error = common_cli.run_allowing_failure(
                "delete-file",
                {"host": host, "path": one["path"], "must_be_under": must_be_under, "force": True,
                 "dry_run": bool(dry_run), **limit},
                timeout_seconds=(int(timeout_seconds) + _PROCESS_MARGIN_SECONDS) if timeout_seconds else None)
            row = (answer or {}).get("file") or {}
            status = str(row.get("status") or "")
            if status == "deleted":
                out["forced"].append({"path": one["path"], "size_bytes": row.get("size_bytes_found"),
                                      "sha256": row.get("sha256_found")})
            elif status == "not_found":
                out["already_gone"].append(one["path"])
            elif status == "skipped":
                out["would_delete"].append(one["path"])
            else:
                out["not_deleted"].append({"path": one["path"], "status": status or "not answered",
                                           "reason": str(row.get("reason") or error or "")})
    return out


def delete_husks(*, host: dict[str, Any], root: str, folders: list[str], entries_left: list[str],
                 dry_run: bool = False, timeout_seconds: int = 600) -> dict[str, Any]:
    """Remove the folders of every backup set under ``root`` that holds no file any more.

    ``folders`` and ``entries_left`` describe the folder after the file deletes (a ``list-files``
    answer ``with_folders``, less what was deleted). Answers ``{"removed": [...], "not_removed":
    [{"path", "status", "reason"}]}``.
    """
    husks = rules.husk_folders(folders, entries_left, root=root)
    out: dict[str, Any] = {"removed": [], "not_removed": []}
    if not husks:
        return out
    items = [{"path": folder, "must_be_under": root} for folder in husks]
    _ok, answer, error = common_cli.run_allowing_failure(
        "delete-empty-folders", {"host": host, "folders": items, "dry_run": bool(dry_run),
                                 "timeout_seconds": int(timeout_seconds)},
        timeout_seconds=int(timeout_seconds) + _PROCESS_MARGIN_SECONDS)
    read = rules.read_answer(answer, items, error=error)
    out["removed"] = read["deleted"] + read["already_gone"]
    out["not_removed"] = read["not_deleted"]
    return out


def summary_line(cleanup: Mapping[str, Any]) -> str:
    """One sentence of a cleanup's answer, for a run's message."""
    parts = [f"{len(cleanup.get('deleted') or [])} deleted"]
    if cleanup.get("forced"):
        parts.append(f"{len(cleanup['forced'])} deleted by force")
    if cleanup.get("already_gone"):
        parts.append(f"{len(cleanup['already_gone'])} already gone")
    if cleanup.get("would_delete"):
        parts.append(f"{len(cleanup['would_delete'])} would be deleted (dry run)")
    if cleanup.get("kept"):
        names = ", ".join(rules.last_part(one["path"]) for one in cleanup["kept"][:3])
        parts.append(f"{len(cleanup['kept'])} kept - not in the catalog ({names}"
                     f"{', ...' if len(cleanup['kept']) > 3 else ''})")
    if cleanup.get("not_deleted"):
        first = cleanup["not_deleted"][0]
        parts.append(f"{len(cleanup['not_deleted'])} not deleted (first: {rules.last_part(first['path'])} "
                     f"{first['status']} - {first['reason']})")
    return ", ".join(parts)
