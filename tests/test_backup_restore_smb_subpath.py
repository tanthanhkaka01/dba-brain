"""Making a staged path *relative* — the step that decides where a backup lands.

`smbclient` lists a share recursively and prints directory headers with backslashes
(`\\DBA\\SqlBK\\INST\\<db>\\LOG`). The staging code strips the share's sub-path off the front of
those, and what is left becomes the path under the import directory — `<db>/LOG/x.trn`, which is
exactly where the restore looks for a log.

`_parse_unc_share` hands that sub-path over **POSIX-separated** (`DBA/SqlBK/INST`). The strip
therefore compared `/` against `\\` and matched nothing — but only when the sub-path had more
than one segment, because a single segment has no separator to disagree about. Every share in
use had one segment (`\\host\\SQLBK\\APPDB-DB$APPDB`), so it worked everywhere and was wrong
nowhere until a maintenance-plan tree with no share of its own had to be reached through `D$`.

Nothing raises when it fails. The files copy successfully, several directories too deep, and the
restore reports that there are no backups.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_ops.backup_restore import copy_backup


ENTRY = "  f_LOG_20260813_021500.trn   A   99  Thu Aug 13 02:15:00 2026"


def _relative(header: str, remote_dir: str) -> str:
    parsed = copy_backup._parse_smbclient_ls(header + "\n" + ENTRY + "\n", remote_dir=remote_dir)
    assert parsed, "the listing produced no entries at all"
    return parsed[0].relative_path


def test_a_single_segment_subpath_still_resolves_the_way_it_always_did():
    """`\\\\192.0.2.250\\SQLBK\\APPDB-DB$APPDB` — the shape every working entry uses. This is the
    regression guard: the fix must not move the paths that were already correct."""
    assert _relative("\\APPDB-DB$APPDB\\APPDB_STG\\FULL", "APPDB-DB$APPDB") == \
        "APPDB_STG\\FULL\\f_LOG_20260813_021500.trn"


def test_a_multi_segment_subpath_is_stripped_too():
    """`\\\\192.0.2.248\\D$\\DBA\\SqlBK\\EXAMPLE-SQL$SQLEXPRESS` — reaching a maintenance-plan
    tree that has no share of its own. Before the fix this returned the whole
    `DBA\\SqlBK\\EXAMPLE-SQL$SQLEXPRESS\\...` path, so every file staged three directories too
    deep and the restore found nothing."""
    assert _relative("\\DBA\\SqlBK\\EXAMPLE-SQL$SQLEXPRESS\\Globex_Prod\\LOG",
                     "DBA/SqlBK/EXAMPLE-SQL$SQLEXPRESS") == \
        "Globex_Prod\\LOG\\f_LOG_20260813_021500.trn"


def test_the_separator_the_caller_used_does_not_matter():
    """`_parse_unc_share` returns POSIX; a hand-written caller would use backslashes. Both are
    the same share."""
    posix = _relative("\\a\\b\\Globex_Prod\\LOG", "a/b")
    backslash = _relative("\\a\\b\\Globex_Prod\\LOG", "a\\b")
    trailing = _relative("\\a\\b\\Globex_Prod\\LOG", "/a/b/")

    assert posix == backslash == trailing == "Globex_Prod\\LOG\\f_LOG_20260813_021500.trn"


def test_a_share_root_with_no_subpath_is_unchanged():
    assert _relative("\\Globex_Prod\\LOG", "") == "Globex_Prod\\LOG\\f_LOG_20260813_021500.trn"


def test_the_parser_still_splits_the_unc_into_host_share_and_subpath():
    host, share, subpath = copy_backup._parse_unc_share(
        Path("\\\\192.0.2.248\\D$\\DBA\\SqlBK\\EXAMPLE-SQL$SQLEXPRESS"))

    assert (host, share) == ("192.0.2.248", "D$")
    assert subpath == "DBA/SqlBK/EXAMPLE-SQL$SQLEXPRESS"
