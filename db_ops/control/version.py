"""Read and bump the db_ops version (db_ops/__init__.py __version__).

Convention: bump on every code fix / deploy so each shipped image is identifiable.
Format is MAJOR.MINOR.PATCH, zero-padded (e.g. 2.01.00).
"""

from __future__ import annotations

import re

from db_ops.control._support import INIT_PY, read_version


def bump_string(version: str, part: str) -> str:
    segs = version.split(".")
    if len(segs) != 3 or not all(s.isdigit() for s in segs):
        raise SystemExit(f"Unexpected version format (need MAJOR.MINOR.PATCH): {version!r}")
    widths = [len(s) for s in segs]
    nums = [int(s) for s in segs]
    idx = {"major": 0, "minor": 1, "patch": 2}[part]
    nums[idx] += 1
    for i in range(idx + 1, 3):
        nums[i] = 0
    return ".".join(str(n).zfill(w) for n, w in zip(nums, widths))


def bump_version(*, part: str = "patch", set_to: str | None = None, dry_run: bool = False) -> dict:
    old = read_version()
    new = set_to if set_to else bump_string(old, part)
    if new == old:
        raise SystemExit(f"Version unchanged ({old}); nothing to do.")
    if not dry_run:
        text = INIT_PY.read_text(encoding="utf-8")
        new_text, count = re.subn(
            r'(__version__\s*=\s*")[^"]+(")',
            lambda m: m.group(1) + new + m.group(2),
            text,
        )
        if count != 1:
            raise SystemExit(f"Could not locate a single __version__ assignment in {INIT_PY}")
        INIT_PY.write_text(new_text, encoding="utf-8")
    print(f"{'[dry-run] ' if dry_run else ''}version: {old} -> {new}")
    return {"old": old, "new": new, "dry_run": dry_run}
