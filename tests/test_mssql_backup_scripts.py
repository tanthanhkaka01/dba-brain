"""SQL Server is backed up by two scripts, and they must produce one backup set.

`mssql_backup_database.sh` runs against a containerised instance on a Linux host;
`mssql_backup_database.ps1` runs on a Windows host over WinRM. The restore side reads a set
*without being told which of them wrote it* — `list-backup-files` classifies SQL Server backups by
asking the instance, and the drill picks a file by name and layout. So the moment the two disagree
about a directory name, a file extension or the receipt they print, there are two backup formats
and one of them is the one nobody has ever restored from.

These tests are the contract between them. They are text assertions on purpose: neither script can
be executed here — one needs a container with SQL Server in it and the other a Windows host with an
instance — and a contract that is only checked when a real instance is available is a contract that
is checked after it has already been broken.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from db_ops.lib.paths import resolve_tool_path

BASH = resolve_tool_path("assets/backup/sqlserver/mssql_backup_database.sh")
PS1 = resolve_tool_path("assets/backup/sqlserver/mssql_backup_database.ps1")


@pytest.fixture(scope="module")
def scripts():
    return {"bash": BASH.read_text(encoding="utf-8"), "ps1": PS1.read_text(encoding="utf-8")}


@pytest.mark.parametrize("flavour", ["bash", "ps1"])
def test_both_scripts_exist_and_cover_the_three_levels(scripts, flavour):
    """One script per platform, not one per level: the level is a job setting, so a caller
    schedules `full`, `diff` and `log` against the same entry with three time windows."""
    text = scripts[flavour]

    assert "full" in text and "diff" in text and "log" in text
    assert "BACKUP_LEVEL" in text


@pytest.mark.parametrize("flavour", ["bash", "ps1"])
def test_both_use_the_same_directory_layout(scripts, flavour):
    """`<DB>/FULL/<DB>_FULL_<stamp>.bak` on both. The restore side finds a file by this shape."""
    text = scripts[flavour]

    for part in ("FULL", "DIFF", "LOG"):
        assert part in text
    assert "_cert" in text, "the certificate lives beside the backups on both platforms"


@pytest.mark.parametrize("flavour", ["bash", "ps1"])
def test_both_print_the_same_receipt(scripts, flavour):
    """`RESULT=ok` is what db_ops.common.backup checks for. A script that exits 0 without it is
    reported as a failure, so a script that never prints it can never succeed."""
    text = scripts[flavour]

    assert "RESULT=ok" in text
    assert "RESULT=error" in text, "a refusal says so in the same vocabulary"


@pytest.mark.parametrize("flavour", ["bash", "ps1"])
def test_both_verify_what_landed_on_disk(scripts, flavour):
    """CHECKSUM on the way in is only half of it. VERIFYONLY re-reads the file, which is the
    difference between "the command returned" and "the file is restorable"."""
    text = scripts[flavour]

    assert "CHECKSUM" in text
    assert "RESTORE VERIFYONLY" in text


@pytest.mark.parametrize("flavour", ["bash", "ps1"])
def test_both_refuse_a_diff_with_no_full_behind_it(scripts, flavour):
    """SQL Server can silently promote such a DIFF; "base backup not found" then appears only at
    restore time, which is the worst moment to learn it."""
    text = scripts[flavour]

    assert "msdb.dbo.backupset" in text
    assert "no FULL backup exists" in text


@pytest.mark.parametrize("flavour", ["bash", "ps1"])
def test_both_exclude_system_databases(scripts, flavour):
    """master/msdb/model are `database_id > 4`, and restoring them onto another instance is a
    different operation entirely — that is what the metadata bundle is for."""
    assert "database_id > 4" in scripts[flavour]


@pytest.mark.parametrize("flavour", ["bash", "ps1"])
def test_a_log_backup_skips_simple_recovery_databases(scripts, flavour):
    """BACKUP LOG on a SIMPLE database fails. Filtered out rather than allowed to fail the job,
    which would make one SIMPLE database mark the whole estate's log run as failed."""
    assert "SIMPLE" in scripts[flavour]


@pytest.mark.parametrize("flavour", ["bash", "ps1"])
def test_retention_never_deletes_past_the_newest_full(scripts, flavour):
    """Age alone removes the FULL that every retained DIFF/LOG restores onto, leaving a set that
    looks present and cannot be used."""
    text = scripts[flavour]

    assert "RETENTION_DAYS" in text
    assert "newest FULL" in text or "newest_full" in text or "newestFull" in text


@pytest.mark.parametrize("flavour", ["bash", "ps1"])
def test_encryption_exports_the_certificate_beside_the_backups(scripts, flavour):
    """A backup encrypted with a certificate that exists only inside the source instance is
    restorable nowhere else, which defeats the point of taking it."""
    text = scripts[flavour]

    assert "BACKUP CERTIFICATE" in text
    assert ".pvk" in text and ".cer" in text
    assert "AES_256" in text


# --------------------------------------------------------------------------- #
# The Windows script's own hazards
# --------------------------------------------------------------------------- #
def test_the_windows_script_avoids_powershell_7_only_syntax(scripts):
    """These hosts run Windows PowerShell 5.1, where `??`, `?:` and `?.` are parser errors — the
    script dies before its first line runs, so every guard in it is bypassed at once."""
    text = scripts["ps1"]
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]

    assert not [line for line in code if "??" in line]
    assert not [line for line in code if re.search(r"\?\.", line)]
    assert not [line for line in code if "-SkipCertificateCheck" in line]


def test_the_windows_script_never_uses_write_error_for_a_recoverable_failure(scripts):
    """With `$ErrorActionPreference = 'Stop'`, Write-Error RAISES. `Write-Error ...; $failed = 1;
    continue` would abandon the loop — the difference between "one database could not be backed
    up" and "the other eleven were never attempted"."""
    text = scripts["ps1"]
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]

    assert not [line for line in code if "Write-Error" in line]
    assert "WriteErrorLine" in text


def test_the_windows_script_creates_directories_through_the_engine(scripts):
    """BACKUP TO DISK is executed by the SQL Server service account, not by this session. A
    directory this script can create and the service cannot fails with "Operating system error
    5(Access is denied)" naming a directory that plainly exists."""
    text = scripts["ps1"]
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]

    assert "xp_create_subdir" in text
    assert not [line for line in code if "New-Item" in line]


def test_the_windows_script_does_not_shadow_the_automatic_args_variable(scripts):
    """`$args` is PowerShell's automatic array of unbound arguments. Assigning to it inside a
    function works until it does not, and the failure looks like sqlcmd being called with nothing."""
    text = scripts["ps1"]

    assert "@args" not in text
    assert "@sqlArgs" in text


def test_the_windows_script_asks_the_engine_whether_the_certificate_is_there(scripts):
    """Test-Path answers for this WinRM session. The file was written by the service account and
    may sit on a share this session cannot read, in which case the certificate is exported again
    over the top of itself on every run."""
    text = scripts["ps1"]

    assert "xp_fileexist" in text


def test_the_windows_script_says_it_owns_the_chain(scripts):
    """It writes ordinary FULL backups, not COPY_ONLY, so a FULL taken here resets the differential
    base for the whole instance. On an instance that still has its own Agent backup jobs that
    splits the chain across two locations. The warning belongs in the file somebody reads before
    enabling it."""
    text = scripts["ps1"]

    assert "COPY_ONLY" in text, "the choice not to use it must be stated, not silent"
    assert "differential base" in text
