"""Which `log_reuse_wait_desc` values are worth waking someone for.

`LOG_REUSE_WAIT` grades by allow-list and sends everything else to `ELSE 'CRITICAL'`. That
catch-all is the right default — an unrecognised reason is worth a look precisely because nobody
has decided what it means yet — but it only works if the *known* values are all listed, and
`DATABASE_SNAPSHOT_CREATION` never was. Microsoft documents that one as "routine, and typically
brief", and `DBCC CHECKDB` creates the internal snapshot that causes it. So a routine consistency
check — including the one db_ops' own restore workflow runs as its last step — reported at the
same severity as a database that is OFFLINE.

Worse, it outlives the check. `log_reuse_wait_desc` is cached and recomputed only when the engine
next attempts to truncate; on a FULL-recovery database with no log backup yet and no write
traffic, that attempt may never come. After the 192.0.2.248 -> 192.0.2.11 migration on
2026-08-11 the five databases still reporting it were exactly the five whose log sat untouched at
its initial 8 MB, while every database whose log had grown read `NOTHING` again. Five standing
CRITICALs for a check that finished hours earlier.

These tests read the shipped SQL rather than a copy of it, so a future edit to either variant has
to face them.
"""

import re
from pathlib import Path

import pytest
from db_ops.lib.paths import resolve_tool_path

SQL_DIR = resolve_tool_path("assets/metrics/sqlserver")
VARIANTS = {
    "modern": SQL_DIR / "018_sqlserver_log_reuse_wait.sql",
    "legacy_2008r2": SQL_DIR / "legacy_2008r2" / "018_sqlserver_log_reuse_wait.sql",
}


def _graded(path: Path) -> dict[str, str]:
    """Map every listed log_reuse_wait_desc to the status its branch returns.

    Parsed from the file rather than restated here: a test that carries its own copy of the list
    passes while the shipped metric says something else.
    """
    text = path.read_text(encoding="utf-8-sig")
    # Comments carry example values; they must not be read as list members.
    text = re.sub(r"--[^\n]*", "", text)
    out: dict[str, str] = {}
    for values, status in re.findall(
        r"log_reuse_wait_desc\s+IN\s*\((.*?)\)\s*THEN\s*'(\w+)'", text, re.DOTALL):
        for value in re.findall(r"'(\w+)'", values):
            out[value] = status
    return out


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_a_database_snapshot_is_not_an_incident(variant):
    """The value this whole file exists for."""
    assert _graded(VARIANTS[variant])["DATABASE_SNAPSHOT_CREATION"] == "LOGGING"


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_the_reason_that_caused_a_real_outage_is_still_reported(variant):
    """`LOG_BACKUP` is SQL Server saying it cannot reuse the log because nobody backs it up — the
    condition behind the 2026-08-09 SALESDB outage on 192.0.2.115. Quietening
    DATABASE_SNAPSHOT_CREATION must not quieten this."""
    assert _graded(VARIANTS[variant])["LOG_BACKUP"] == "LOGGING"


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_an_idle_log_is_still_ok(variant):
    graded = _graded(VARIANTS[variant])

    assert graded["NOTHING"] == "OK"
    assert graded["CHECKPOINT"] == "OK"


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_an_unrecognised_reason_still_falls_through_to_critical(variant):
    """The catch-all is the point of the design and must survive the addition above."""
    text = VARIANTS[variant].read_text(encoding="utf-8-sig")

    assert re.search(r"ELSE\s*'CRITICAL'", text)


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_a_database_that_is_not_online_outranks_every_reason(variant):
    """Checked before the reason lists, so an OFFLINE/RECOVERY_PENDING database is CRITICAL
    whatever its log happens to be waiting on."""
    # Inside the CASE only: log_reuse_wait_desc also appears above it as the metric_value column.
    case = VARIANTS[variant].read_text(encoding="utf-8-sig").split("CASE", 1)[1]
    state_branch = case.index("state_desc <> 'ONLINE'")
    first_reason_branch = case.index("log_reuse_wait_desc")

    assert state_branch < first_reason_branch


def test_both_variants_grade_identically():
    """They are the same judgement against two engine versions; drift between them means one
    estate is judged by a rule the other is not."""
    assert _graded(VARIANTS["modern"]) == _graded(VARIANTS["legacy_2008r2"])


def test_the_two_documented_values_left_out_are_left_out_on_purpose():
    """DATABASE_MIRRORING and LOG_SCAN are the remaining documented reasons. Neither has been
    observed on this estate, so they stay in the CRITICAL catch-all rather than being pre-approved
    from a manual — this test exists so removing them from the catch-all is a decision, not a
    drive-by edit."""
    graded = _graded(VARIANTS["modern"])

    assert "DATABASE_MIRRORING" not in graded
    assert "LOG_SCAN" not in graded
