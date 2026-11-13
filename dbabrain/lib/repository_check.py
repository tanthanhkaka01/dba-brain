"""What a server's repository folder must hold, and what it holds that it must not (0.37.0).

The operator, 2026-11-10 (translated): *if /mnt/nas/<server_id> has a stray file - one that is not in
the backup tables - it does not need a critical alert: the backup/restore run fails and says so, to
the group already set for backup/restore notices*; and *every case must have a test proving it*.

**What is expected** under ``<offload_folder>/<server_id>/``:

* every file an offload PROVED there (``BackupCatalogStore.repository_proofs`` - a base backup's
  members included), at the bytes it was proven at, while its backup file is still ``verified``;
* for a copy encrypted at the repository: its ``.age`` file at the bytes measured when it was
  written - and the plain file no longer, once the encryption recorded the plain file's delete. Until
  then both are expected: a pass that stopped between the two leaves both, and the next pass ends it.

**What is a finding** - each one fails the run:

* ``stray`` - an entry the catalog does not know: a file left by hand, a partial a copy left behind,
  an expired copy whose delete did not happen, a file of another server written into this folder;
* ``missing`` - a proven copy that is not there any more;
* ``size_differs`` - a proven copy whose bytes changed;
* ``not_a_file`` - a link or a device, whatever it points at.

Bytes, not a hash: the check reads the whole folder on every pass, and hashing terabytes every hour
is not a check anyone can afford. Each file's hash was proven when it was copied (and encrypted).

Pure: the caller lists the folder (``common.cli list-files``) and reads the catalog; this decides.
"""

from __future__ import annotations

from typing import Any, Iterable

STRAY = "stray"
MISSING = "missing"
SIZE_DIFFERS = "size_differs"
NOT_A_FILE = "not_a_file"
KINDS = (STRAY, MISSING, SIZE_DIFFERS, NOT_A_FILE)
#: How many paths of each kind a message names; the rest are counted.
NAMED_IN_A_MESSAGE = 3


def _under(path: str, folder: str) -> bool:
    return path.startswith(folder.rstrip("/") + "/")


def expected_files(folder: str, proofs: dict[str, dict[str, Any]],
                   encrypted: Iterable[dict[str, Any]]) -> dict[str, int | None]:
    """Every path ``folder`` must hold, with its bytes (``None``: the size was not recorded)."""
    expected: dict[str, int | None] = {}
    for path, proof in proofs.items():
        if _under(path, folder):
            size = proof.get("size_bytes")
            expected[path] = int(size) if size is not None else None
    for row in encrypted:
        plain, sealed = str(row.get("plain_path") or ""), str(row.get("encrypted_path") or "")
        if row.get("plain_deleted_at") and plain in expected:
            del expected[plain]
        if sealed and _under(sealed, folder):
            size = row.get("encrypted_size_bytes")
            expected[sealed] = int(size) if size is not None else None
    return expected


def findings(expected: dict[str, int | None], listed: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """``{stray, missing, size_differs, not_a_file}`` - each a list of ``{path, ...}``, sorted."""
    found: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KINDS}
    seen: set[str] = set()
    for entry in listed:
        path = str(entry.get("path") or "")
        seen.add(path)
        if entry.get("type") != "file":
            found[NOT_A_FILE].append({"path": path, "type": entry.get("type")})
        elif path not in expected:
            found[STRAY].append({"path": path, "size_bytes": entry.get("size_bytes")})
        elif expected[path] is not None and int(entry.get("size_bytes") or 0) != expected[path]:
            found[SIZE_DIFFERS].append({"path": path, "size_bytes": entry.get("size_bytes"),
                                        "expected_size_bytes": expected[path]})
    found[MISSING] = [{"path": path, "expected_size_bytes": size}
                      for path, size in expected.items() if path not in seen]
    for kind in KINDS:
        found[kind].sort(key=lambda item: item["path"])
    return found


def is_clean(found: dict[str, list[dict[str, Any]]]) -> bool:
    return not any(found.get(kind) for kind in KINDS)


def describe(folder: str, found: dict[str, list[dict[str, Any]]]) -> str:
    """One line for the run's message: the counts, and the first few paths of each kind."""
    if is_clean(found):
        return f"{folder}: holds exactly the proven copies"
    words = {STRAY: "not in the catalog", MISSING: "proven and gone", SIZE_DIFFERS: "of other bytes than proven",
             NOT_A_FILE: "a link or not a file"}
    parts = []
    for kind in KINDS:
        items = found.get(kind) or []
        if items:
            names = ", ".join(item["path"].rsplit("/", 1)[-1] for item in items[:NAMED_IN_A_MESSAGE])
            more = f" (+{len(items) - NAMED_IN_A_MESSAGE} more)" if len(items) > NAMED_IN_A_MESSAGE else ""
            parts.append(f"{len(items)} {words[kind]}: {names}{more}")
    return f"{folder}: " + "; ".join(parts)
