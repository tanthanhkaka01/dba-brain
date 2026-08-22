"""Which backup file a SQL Server restore drill picks for each database.

The drill finds its FULL/DIFF/LOG chain by listing a directory and ordering the names, so the
naming convention *is* the chain metadata. That made two failures possible at once on the CLOUD
lab, and both restored something without complaining:

  * a file belonging to another database (``test_db_01_FULL_01.bak`` left in ``mssql_ha_db/FULL``)
    sorted after every real ``mssql_ha_db_FULL_2026*.bak``, so ``sort | tail -1`` handed RESTORE
    the wrong database's backup - and the genuine ``test_db_01`` restore then died with
    "test_db_01.mdf is being used by database mssql_ha_db";
  * a file with the right prefix but no timestamp (``test_db_01_LOG_01.trn``) survived the
    stamp-extracting sed unchanged, so the "is this log newer than the FULL?" test compared the
    literal filename against ``20260805_085205`` - which any letter wins - and a pre-FULL log was
    always applied, failing with "the log in this backup set terminates at LSN ..., too early".

So the rule under test is narrow on purpose: a piece is eligible only if its name is exactly
``<db>_<LEVEL>_<YYYYMMDD>_<HHMMSS>.<ext>``, the name the backup job writes. Anything else is not
"probably fine" - it is a file the ordering logic cannot reason about, and the whole chain is
ordered by that name.

The test runs the shipped ``pieces()`` straight out of the asset rather than a copy of it, so the
assertion cannot drift away from what actually ships.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from db_ops.lib.paths import resolve_tool_path

SCRIPT = resolve_tool_path("assets/restore/sqlserver/mssql_restore.sh")


def _pieces_function() -> str:
    """The real ``pieces()`` definition, lifted from the asset."""
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^pieces\(\) \{.*?^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "pieces() is no longer defined in mssql_restore.sh"
    return match.group(0)


def _run_pieces(backup_root: Path, db: str, level: str, ext: str) -> list[str]:
    """Run the shipped selector over a real directory tree, with ``in_t`` stubbed to plain ls.

    ``in_t`` normally runs the listing inside the target container over docker exec; here the
    same shell command runs against a local directory, which is all the selector needs - it
    only ever asks for a listing.
    """
    harness = (
        f'backup_dir="{backup_root.as_posix()}"\n'
        'in_t() { bash -c "$1"; }\n'
        f"{_pieces_function()}\n"
        f'pieces "{db}" "{level}" "{ext}"\n'
    )
    # Fed in as bytes on stdin rather than written to a file: a harness written through Python's
    # text layer on Windows gets CRLF, and the trailing \r then rides along inside backup_dir -
    # so every path the selector built ended in a carriage return, ls found nothing, and the
    # test went green while proving nothing.
    done = subprocess.run(["bash", "-s"], input=harness.encode("utf-8"), capture_output=True)
    assert done.returncode in (0, 1), done.stderr.decode("utf-8", "replace")
    return [Path(line).name for line in done.stdout.decode("utf-8", "replace").split()]


def _layout(root: Path, db: str, level: str, names: list[str]) -> None:
    folder = root / db / level
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text("backup", encoding="utf-8")


pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the selector is shell, and it runs on the Linux restore target; a bash launched "
           "from Windows Python resolves the drive path differently and sees no files at all, "
           "so a run here would prove nothing either way",
)


def test_another_databases_backup_left_in_the_folder_is_not_selected(tmp_path):
    _layout(tmp_path, "mssql_ha_db", "FULL", [
        "mssql_ha_db_FULL_20260804_081743.bak",
        "mssql_ha_db_FULL_20260805_085205.bak",
        "test_db_01_FULL_01.bak",          # sorts last, belongs to a different database
    ])

    found = _run_pieces(tmp_path, "mssql_ha_db", "FULL", "bak")

    assert found == ["mssql_ha_db_FULL_20260804_081743.bak",
                     "mssql_ha_db_FULL_20260805_085205.bak"]
    assert found[-1] == "mssql_ha_db_FULL_20260805_085205.bak"   # what `| tail -1` restores


def test_a_piece_without_a_timestamp_is_not_part_of_the_chain(tmp_path):
    _layout(tmp_path, "test_db_01", "LOG", [
        "test_db_01_LOG_01.trn",                    # right prefix, no stamp to order by
        "test_db_01_LOG_20260805_084905.trn",
        "test_db_01_LOG_20260805_090810.trn",
    ])

    found = _run_pieces(tmp_path, "test_db_01", "LOG", "trn")

    assert "test_db_01_LOG_01.trn" not in found
    assert found == ["test_db_01_LOG_20260805_084905.trn",
                     "test_db_01_LOG_20260805_090810.trn"]


def test_pieces_come_back_in_stamp_order(tmp_path):
    _layout(tmp_path, "mssql_ha_db", "DIFF", [
        "mssql_ha_db_DIFF_20260805_050521.bak",
        "mssql_ha_db_DIFF_20260804_081745.bak",
        "mssql_ha_db_DIFF_20260804_230241.bak",
    ])

    found = _run_pieces(tmp_path, "mssql_ha_db", "DIFF", "bak")

    assert found == ["mssql_ha_db_DIFF_20260804_081745.bak",
                     "mssql_ha_db_DIFF_20260804_230241.bak",
                     "mssql_ha_db_DIFF_20260805_050521.bak"]


def test_an_empty_level_folder_yields_nothing_rather_than_failing(tmp_path):
    """A database with no DIFF at all is normal - the caller then restores FULL + LOGs."""
    _layout(tmp_path, "mssql_ha_db", "FULL", ["mssql_ha_db_FULL_20260805_085205.bak"])

    assert _run_pieces(tmp_path, "mssql_ha_db", "DIFF", "bak") == []


def test_a_restoring_database_is_dropped_without_being_set_single_user():
    """A drill that failed part way leaves its database in RESTORING (state 1), where ALTER
    DATABASE is rejected outright - and with sqlcmd -b that error failed the next run too, so
    no later drill could ever clear the wreckage. Only state 0 (ONLINE) gets SET SINGLE_USER."""
    text = SCRIPT.read_text(encoding="utf-8")

    guarded = ("IF (SELECT state FROM sys.databases WHERE name = '${esc_db}') = 0\n"
               "        ALTER DATABASE [${db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;\n"
               "    DROP DATABASE [${db}];")

    assert guarded in text
