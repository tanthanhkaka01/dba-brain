"""Which backup files a cleanup may delete, and with what facts - the rules, no host (0.38.0, 1.329).

The operator, 2026-11-10: *deleting this file is very dangerous* - one file per command, with its
name, path, bytes and checksum all stated and all true on the host. ``delete-verified-file`` is that
command, and from 0.38.0 it is the **only** way dbabrain deletes a backup file: the path lists, the
``find -delete`` of a staging folder, the SMB delete and the ``rm -rf`` of an emptied backup set are
gone, and every cleanup that used them (a backup entry's prune, a restore's staging cleanup, the
target import folder's) builds its request here.

The facts come from the **catalog**, never from the listing being cleaned: a size read from the
folder a moment ago proves only that the file is what it is, not that it is the file dbabrain wrote
or copied. So:

* a file the catalog knows - by its own path, with bytes and a sha256 - becomes one item of a
  ``delete-verified-files`` request (:func:`items_with_facts`);
* **a file the catalog does not know is kept and named.** It is not dbabrain's to delete: an
  operator's own copy, a file from another tool, a backup no run recorded. The cleanup's answer
  lists each with the reason, and somebody decides.

An emptied backup set leaves its folders behind, some of them empty by design inside a live set
(a PostgreSQL base backup always carries empty ``pg_tblspc`` / ``pg_replslot``). :func:`husk_folders`
names the folders to remove - only a set, at the first two levels of the root, that holds no file
anywhere beneath it - deepest first, each for ``delete-empty-folder``, which refuses a folder that
holds anything on its own.

Pure: listings and facts in, requests out. The app fetches the listing and the catalog's rows and
runs the commands.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from dbabrain.lib.integrity import SHA256_HEX as _SHA256

#: Why a file is kept, in the answer's words.
NOT_IN_THE_CATALOG = "not in the catalog - kept: dbabrain did not record this file, so it is not its to delete"
NO_FACTS = "the catalog holds no bytes or sha256 for it - kept: nothing to hold the file to"


def last_part(path: str) -> str:
    """The file's own name, whichever way its host writes a path."""
    return re.split(r"[/\\]", str(path).rstrip("/\\"))[-1]


def same_path(left: str, right: str, *, windows: bool = False) -> bool:
    """Do two paths name one file on one host? Slashes are one separator; a Windows host ignores case."""
    a, b = str(left).replace("\\", "/").rstrip("/"), str(right).replace("\\", "/").rstrip("/")
    return a.casefold() == b.casefold() if windows else a == b


def _key(path: str, windows: bool) -> str:
    text = str(path).replace("\\", "/").rstrip("/")
    return text.casefold() if windows else text


def items_with_facts(paths: Iterable[str], facts: Mapping[str, Mapping[str, Any]], *, must_be_under: str,
                     windows: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(items, kept)`` - the ``delete-verified-files`` items of the paths the catalog knows, and
    each path it does not, with the reason it is kept.

    ``facts`` maps a path **on the host being cleaned** to ``{"size_bytes", "sha256"}``. A path with
    no entry, or an entry without both facts whole, is kept: a delete that cannot say what the file
    must be is not asked for.
    """
    known = {_key(path, windows): dict(row) for path, row in facts.items()}
    items: list[dict[str, Any]] = []
    kept: list[dict[str, str]] = []
    for path in paths:
        row = known.get(_key(path, windows))
        if row is None:
            kept.append({"path": str(path), "reason": NOT_IN_THE_CATALOG})
            continue
        size, sha = row.get("size_bytes"), str(row.get("sha256") or "").strip().lower()
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or not _SHA256.fullmatch(sha):
            kept.append({"path": str(path), "reason": NO_FACTS})
            continue
        items.append({"path": str(path), "file_name": last_part(str(path)), "size_bytes": size, "sha256": sha,
                      "must_be_under": must_be_under})
    return items, kept


def epoch_of(mtime_utc: str) -> float | None:
    """``list-files``' ``mtime_utc`` as seconds since the epoch; ``None`` when it is not a time."""
    try:
        return datetime.strptime(str(mtime_utc), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def past_retention(entries: Iterable[Mapping[str, Any]], *, retention_seconds: int, now: float,
                   keep_names: Iterable[str] = ()) -> list[str]:
    """The regular files of a ``list-files`` answer last written more than ``retention_seconds`` ago.

    ``0`` (or less) disables the cleanup: nothing is past a window nobody stated. A link or anything
    that is not a regular file is never chosen, nor a file named in ``keep_names`` (the staging
    marker), nor one whose time could not be read - an unknown age is not an old age.
    """
    if int(retention_seconds or 0) <= 0:
        return []
    cutoff = float(now) - int(retention_seconds)
    kept = set(keep_names)
    chosen = []
    for entry in entries:
        if str(entry.get("type") or "") != "file":
            continue
        path = str(entry.get("path") or "")
        if not path or last_part(path) in kept:
            continue
        written = epoch_of(entry.get("mtime_utc"))
        if written is not None and written < cutoff:
            chosen.append(path)
    return sorted(chosen)


def _inside(path: str, folder: str) -> bool:
    return path.startswith(folder.rstrip("/") + "/")


def husk_folders(folders: Iterable[str], entries_left: Iterable[str], *, root: str, max_depth: int = 2) -> list[str]:
    """The folders an emptied backup set left behind, deepest first - each for ``delete-empty-folder``.

    ``folders`` is every folder under ``root`` and ``entries_left`` every entry that is not a folder
    still there (a link counts: it is something). A **set** is a folder at most ``max_depth`` levels
    below ``root`` with nothing but folders anywhere beneath it; it goes with every folder inside it.
    A folder that is empty *inside a set that still holds a file* stays: it is part of that backup
    (``pg_tblspc``), and removing it is how a restore failed on a path nobody deleted at the source.
    The root itself is never named. POSIX paths (a staging folder is on a Linux host).
    """
    base = str(root).rstrip("/")
    every = sorted({str(folder).rstrip("/") for folder in folders if _inside(str(folder), base)})
    left = [str(entry) for entry in entries_left]

    def holds_something(folder: str) -> bool:
        return any(_inside(entry, folder) for entry in left)

    def depth(folder: str) -> int:
        return folder[len(base) + 1:].count("/") + 1

    sets = [folder for folder in every if depth(folder) <= max_depth and not holds_something(folder)]
    # A set inside a set is covered by the outer one; name each folder once.
    doomed = {folder for folder in every if any(folder == one or _inside(folder, one) for one in sets)}
    return sorted(doomed, key=lambda folder: (-folder.count("/"), folder))


def read_answer(answer: Mapping[str, Any] | None, items: list[dict[str, Any]], *,
                error: str = "") -> dict[str, Any]:
    """A ``delete-verified-files`` (or ``delete-empty-folders``) answer, per item asked for.

    ``{"deleted": [...], "already_gone": [...], "not_deleted": [{"path", "status", "reason"}]}`` -
    an item the answer does not mention is ``not answered``, never assumed done.
    """
    lines = {str(line.get("path")): line for line in
             ((answer or {}).get("files") or (answer or {}).get("folders") or [])}
    out: dict[str, Any] = {"deleted": [], "already_gone": [], "not_deleted": []}
    for item in items:
        line = lines.get(str(item["path"])) or {"status": "not answered", "reason": error}
        status = str(line.get("status") or "")
        if status == "deleted":
            out["deleted"].append(item["path"])
        elif status == "not_found":
            out["already_gone"].append(item["path"])
        elif status == "skipped":
            out.setdefault("would_delete", []).append(item["path"])
        else:
            out["not_deleted"].append({"path": item["path"], "status": status or "not answered",
                                       "reason": str(line.get("reason") or error or "")})
    return out
