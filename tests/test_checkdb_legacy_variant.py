"""DATABASE_CHECKDB has to run on the servers least likely to be checked.

The metric shipped with a single `sqlserver_all` variant whose SQL uses `TRY_CONVERT`, a 2012+
function. On 2008 R2 that does not merely return no rows — it fails the batch at compile time, so
the metric was switched off on both 2008 R2 instances with a `disabled_reason` explaining why. The
result was that the two unsupported, unpatched instances in the estate were the only ones never
asked whether their integrity had ever been proven.

These tests pin the 2008 R2 variant and the two properties of its rewrite that are silent when
wrong: an unparseable value must not be able to take the whole scan with it, and a date must not
be read through the session's `DATEFORMAT`.
"""

import json
from pathlib import Path

import pytest
from db_ops.lib.paths import resolve_tool_path
from conftest import shipped_config

DEFINITIONS = shipped_config("metric_definitions.json")
INSTANCES = shipped_config("db_instances.json")
LEGACY_SQL = resolve_tool_path("assets/metrics/sqlserver/legacy_2008r2/054_sqlserver_database_checkdb.sql")
MODERN_SQL = resolve_tool_path("assets/metrics/sqlserver/054_sqlserver_database_checkdb.sql")


@pytest.fixture(scope="module")
def checkdb():
    doc = json.loads(DEFINITIONS.read_bytes().decode("utf-8-sig"))
    return {m["metric_code"]: m for m in doc["metrics"]}["DATABASE_CHECKDB"]


@pytest.fixture(scope="module")
def sqlserver_variants(checkdb):
    return [v for v in checkdb["variants"] if v["db_type"] == "sqlserver"]


def test_the_version_split_exists_and_the_legacy_variant_is_matched_first(sqlserver_variants):
    """Variant selection is first-match-wins, so the bounded variant has to come before the open
    one — with 2012+ listed first, a major_version 10 target picks up TRY_CONVERT again."""
    assert [v["name"] for v in sqlserver_variants] == [
        "sqlserver_legacy_2008r2",
        "sqlserver_modern_2012_plus",
    ]
    assert sqlserver_variants[0]["max_major_version"] == 10
    assert sqlserver_variants[1]["min_major_version"] == 11


def test_every_variant_file_the_metric_names_exists(checkdb):
    for variant in checkdb["variants"]:
        if variant.get("file"):
            assert (resolve_tool_path("assets/metrics") / variant["file"]).is_file(), variant["file"]


def test_the_legacy_variant_uses_nothing_newer_than_2008_r2():
    """One 2012+ function is enough to fail the batch, and it fails at compile time — the metric
    reports nothing at all rather than reporting less. Comments are stripped first: the header
    names TRY_CONVERT to explain why the file exists, which is documentation, not a call."""
    executable = "\n".join(
        line.split("--", 1)[0] for line in LEGACY_SQL.read_text(encoding="utf-8").splitlines()
    ).upper()

    for banned in ("TRY_CONVERT", "TRY_CAST", "TRY_PARSE", "CONCAT(", "IIF(", "FORMAT("):
        assert banned not in executable, banned


def test_the_conversion_is_guarded_one_value_at_a_time():
    """`CASE WHEN ISDATE(x) = 1 THEN CONVERT(...)` does not guarantee the guard runs before the
    conversion, so a single unparseable value raises 241 and costs every database. Doing it with
    IF/SET inside the cursor is what makes the ordering real."""
    sql = LEGACY_SQL.read_text(encoding="utf-8")

    assert "IF @raw IS NOT NULL AND @raw <> '1900-01-01 00:00:00.000' AND ISDATE(@raw) = 1" in sql
    assert "SET @last_good_at = CONVERT(datetime, @raw, 121);" in sql
    # The converted value is carried on the temp table, so the reporting SELECT never converts.
    assert "last_good_at datetime NULL" in sql


def test_the_conversion_pins_a_style_instead_of_trusting_the_session():
    """dbi_dbccLastKnownGood is 'yyyy-mm-dd hh:mm:ss.mmm', which `datetime` reads through the
    session LANGUAGE/DATEFORMAT: under dmy the collector reads month and day swapped and reports
    an age wrong by up to eleven months, with no error anywhere."""
    sql = LEGACY_SQL.read_text(encoding="utf-8")

    assert "CONVERT(datetime, @raw, 121)" in sql
    assert "CONVERT(datetime, @raw)" not in sql


def test_the_legacy_variant_reports_the_same_two_findings_as_the_modern_one():
    """A version variant may change how, never what: a 2008 R2 instance that has never run CHECKDB
    has to land in the same grouped finding as every other instance, and the permission gap has to
    stay one instance-level row rather than one per database."""
    legacy = LEGACY_SQL.read_text(encoding="utf-8")
    modern = MODERN_SQL.read_text(encoding="utf-8")

    for token in ("CHECKDB_NEVER", "CHECKDB_STALE", "CHECKDB_UNREADABLE", "checkdb_coverage"):
        assert token in legacy and token in modern, token
    # The thresholds are the metric's meaning, not an implementation detail of one file.
    for threshold in ("@stale_warn_days int = 7", "@stale_crit_days int = 30"):
        assert threshold in legacy and threshold in modern, threshold


def test_the_legacy_variant_probes_permissions_once_before_the_per_database_loop():
    """DBCC DBINFO rights are granted instance-wide, so probing inside the loop reported the same
    missing GRANT once per database — 11 databases blamed for one grant."""
    sql = LEGACY_SQL.read_text(encoding="utf-8")

    probe = sql.index("@dbinfo_error = LEFT(")
    loop = sql.index("DECLARE db_cur CURSOR")
    assert probe < loop


def test_no_instance_still_disables_checkdb_for_the_missing_2008_r2_sql():
    """The override is the symptom; leaving it behind means the variant exists and nothing uses
    it. Overrides for other reasons (a login that is refused DBCC DBINFO) are a different finding
    and stay."""
    doc = json.loads(INSTANCES.read_bytes().decode("utf-8-sig"))

    for instance in doc["db_instances"]:
        override = ((instance.get("metrics") or {}).get("metric_overrides") or {}).get("DATABASE_CHECKDB")
        if not override:
            continue
        assert "TRY_CONVERT" not in (override.get("disabled_reason") or ""), instance.get("server_id")
