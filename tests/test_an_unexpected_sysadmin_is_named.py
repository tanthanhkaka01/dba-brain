"""A login in the sysadmin role that nobody expects there is a finding (1.148).

`SECURITY_SERVER_PRINCIPALS` lists every login and grades none, on purpose: grading sysadmin there
would warn on `sa` and the engine's service accounts on every instance, for ever. The operator
named the one sysadmin meant to exist - dbabrain's own login - and asked for the check to run when
that login is sysadmin and to be skipped when it is not (2026-10-15).

So `SECURITY_LOGIN_HEALTH` gained a SYSADMIN signal. Expected, and not listed: the login the check
runs as, and what SQL Server puts in the role itself - `sa` by principal id, `NT SERVICE\\*`,
`NT AUTHORITY\\SYSTEM`, `##certificate##` logins - the exclusions sp_Blitz's "Sysadmins" check
makes, a disabled login included. On Linux the Windows principals setup adds (`BUILTIN\\Administrators`,
`NT AUTHORITY\\*`) can log nobody in: what the OS does not have is not a finding. Only a sysadmin sees
every member of the role, so without it the signal says `skipped` instead of an undercount that
reads as clean.

Run on the `.251` Linux lab: OK, `sysadmin=0`; a copy as if not sysadmin `skipped`; a copy as if on
Windows named `BUILTIN\\Administrators` and `NT AUTHORITY\\NETWORK SERVICE`.
"""

from __future__ import annotations

from pathlib import Path
import re

SQL = (Path(__file__).resolve().parents[1] / "dbabrain" / "metrics" / "collectors" / "sqlserver"
       / "063_sqlserver_login_health.sql")


def _code() -> str:
    lines = [line.split("--", 1)[0] for line in SQL.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


def _signal() -> str:
    code = _code()
    start = code.index("IF IS_SRVROLEMEMBER('sysadmin') = 1")
    return code[start:code.index("END CATCH", start)]


def test_the_signal_runs_only_when_this_login_can_see_the_whole_role():
    signal = _signal()
    assert "SET @sysadmin_checked = 1;" in signal
    assert "CASE WHEN @sysadmin_checked = 1" in _code()
    assert "N'skipped (this login is not sysadmin)'" in _code()


def test_the_login_running_the_check_and_the_engines_own_accounts_are_expected():
    signal = _signal()
    for exclusion in ("m.principal_id <> 1", "m.is_disabled = 0", "m.name <> SUSER_SNAME()",
                      "m.name NOT LIKE 'NT SERVICE\\%'", "m.name <> 'NT AUTHORITY\\SYSTEM'",
                      "m.name NOT LIKE '##%'"):
        assert exclusion in signal, exclusion


def test_on_linux_the_windows_principals_setup_adds_are_not_findings():
    signal = _signal()
    assert ("AND NOT (@@VERSION LIKE '%on Linux%' AND (m.name LIKE 'BUILTIN\\%' "
            "OR m.name LIKE 'NT AUTHORITY\\%'))") in signal


def test_no_estate_login_is_named_in_the_shipped_file():
    """The expected sysadmin is whoever runs the check; a name from this estate would ship publicly."""
    assert "tanthanh" not in SQL.read_text(encoding="utf-8").lower()


def test_each_unexpected_member_is_a_logging_row_and_the_summary_carries_the_warning():
    code = _code()
    assert "SELECT 'SYSADMIN', m.name" in code
    assert "CAST(CASE WHEN EXISTS (SELECT 1 FROM #login) THEN 'WARNING' ELSE 'OK' END AS varchar(16))" in code
