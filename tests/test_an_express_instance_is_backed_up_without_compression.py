"""SQL Server Express is backed up without compression, because it has none (0.38.0, 1.346).

Both SQL Server backup scripts wrote every backup `WITH INIT, CHECKSUM, COMPRESSION`. Express refuses
that outright - *BACKUP DATABASE WITH COMPRESSION is not supported on Express Edition* (error 1844) -
so the first Express instance handed to the script would have failed every FULL, DIFF and LOG, and the
failure would have read as a broken server rather than as an edition.

It went unseen because the one instance in the estate **named** `SQLEXPRESS` is a Developer edition.
It was found on 2026-11-12, reading a real Express instance (EngineEdition 4) before putting it
on the scheduled backups. So the script asks the instance what it is - never its name - and leaves the
word out for Express alone.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "dbabrain" / "common" / "backup_scripts" / "sqlserver"
POWERSHELL = SCRIPTS / "mssql_backup_database.ps1"
SHELL = SCRIPTS / "mssql_backup_database.sh"
BOTH = [POWERSHELL, SHELL]


def _backup_statements(script: Path) -> list[str]:
    return [line.strip() for line in script.read_text(encoding="utf-8").splitlines()
            if re.search(r"statement\s*=\s*\"BACKUP (DATABASE|LOG) ", line)]


@pytest.mark.parametrize("script", BOTH, ids=lambda p: p.suffix)
def test_the_script_asks_the_instance_which_edition_it_is(script):
    text = script.read_text(encoding="utf-8")

    assert "SERVERPROPERTY('EngineEdition')" in text
    assert "SQLEXPRESS" not in "".join(line for line in text.splitlines() if not line.lstrip().startswith("#")), (
        "the instance's name says nothing of its edition")


@pytest.mark.parametrize("script", BOTH, ids=lambda p: p.suffix)
def test_no_backup_statement_has_the_word_written_into_it(script):
    statements = _backup_statements(script)

    assert len(statements) == 2, "one for a database, one for a log"
    for statement in statements:
        assert "COMPRESSION" not in statement, statement
        assert re.search(r"CHECKSUM\$\{?compress_?[cC]lause\}?", statement), statement


def test_express_alone_goes_without_it_in_powershell():
    text = POWERSHELL.read_text(encoding="utf-8")

    assert "$compressClause = if ($engineEdition -eq '4') { '' } else { ', COMPRESSION' }" in text


def test_express_alone_goes_without_it_in_the_shell_script():
    text = SHELL.read_text(encoding="utf-8")
    block = text[text.index('if [ "$engine_edition" = "4" ]; then'):]
    block = block[:block.index("\nfi\n")]

    assert 'compress_clause=""' in block.split("else")[0]
    assert 'compress_clause=", COMPRESSION"' in block.split("else")[1]


@pytest.mark.parametrize("edition, expected", [("4", "WITH INIT, CHECKSUM;"), ("3", "WITH INIT, CHECKSUM, COMPRESSION;"),
                                               ("2", "WITH INIT, CHECKSUM, COMPRESSION;"), ("", "WITH INIT, CHECKSUM, COMPRESSION;")])
def test_the_shell_scripts_own_lines_build_the_statement_each_edition_takes(edition, expected):
    """The script's own lines, run: the edition block and the statement it feeds - with the instance's
    answer put in by hand. An answer that is not 4 (another edition, or none read) keeps compression:
    the word is dropped only where it is known to be refused."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash on this machine")
    text = SHELL.read_text(encoding="utf-8")
    block = text[text.index('if [ "$engine_edition" = "4" ]; then'):]
    block = block[:block.index("\nfi\n") + 4]
    statement = next(line.strip() for line in text.splitlines() if 'statement="BACKUP DATABASE ' in line)
    script = (f'engine_edition="{edition}"\n{block}\ndb=D; target_file=F; clause=""; encrypt_clause=""\n{statement}\n'
              'printf "%s" "$statement"\n')

    done = subprocess.run([bash, "-c", script], capture_output=True, text=True, timeout=30)

    assert done.returncode == 0, done.stderr
    assert done.stdout.endswith(f"BACKUP DATABASE [D] TO DISK = 'F' {expected}"), done.stdout


def test_the_powershell_script_still_parses():
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("no PowerShell on this machine")
    command = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{POWERSHELL}', [ref]$null, [ref]$e); $e.Count")

    done = subprocess.run([powershell, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=120)

    assert done.stdout.strip() == "0", done.stdout + done.stderr
