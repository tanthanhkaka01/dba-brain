"""What Linux runs leaves this tree with LF line endings - in the working tree, not only in git.

`.gitattributes` says `eol=lf` for shell scripts and templates, and the repository holds them that
way. But the export copies the WORKING TREE, and on 2026-09-24 seventeen `.sh` / `.j2` files there
were CRLF - checked out before the attribute existed - so the 0.23.0 wheel shipped CRLF restore
scripts and compose templates. A shell script with a CRLF line runs `\\r` as part of its commands.
This fails the tree, on the machine that would export it, before that can happen again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINUX_RUNS = sorted(p for pattern in ("*.sh", "*.j2") for p in (ROOT / "db_ops").rglob(pattern))


def test_there_is_something_to_check():
    assert len(LINUX_RUNS) > 20


@pytest.mark.parametrize("path", LINUX_RUNS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_a_file_linux_runs_has_no_crlf(path):
    assert b"\r\n" not in path.read_bytes(), (
        f"{path.relative_to(ROOT).as_posix()} has CRLF line endings in the working tree. "
        "Refresh it from the index: delete it and `git checkout -- <path>`.")
