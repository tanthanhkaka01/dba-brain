"""PowerShell reads four typographic quotes as single quotes too (review 0.25.0, F12.1).

`quote_powershell` doubled only `'`, so `’` closed the literal and the rest of the value ran as a
command - in a path, a service name, SQL text sent to sqlcmd, a password.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from db_ops.backup_restore import preflight, shell_quoting
from db_ops.common import deletefiles
from db_ops.lib.powershell import quote_powershell

QUOTES = ["'", "\u2018", "\u2019", "\u201a", "\u201b"]


@pytest.mark.parametrize("quote", QUOTES)
def test_each_quote_is_doubled(quote):
    assert quote_powershell(f"a{quote}b") == f"'a{quote}{quote}b'"


def test_the_private_copies_are_the_same_function():
    value = "x\u2019; Remove-Item C:\\ -Recurse; \u2019"
    assert shell_quoting._ps_quote(value) == preflight._ps_quote(value) == deletefiles._ps_quote(value) \
        == quote_powershell(value)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize("quote", QUOTES)
def test_powershell_reads_the_value_back_unchanged(quote):
    value = f"x{quote}; Write-Output INJECTED; {quote}"
    out = subprocess.run(["pwsh", "-NoProfile", "-Command", f"Write-Output {quote_powershell(value)}"],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert out.strip() == value
