"""Tests for the instance/build facts the inventory report surfaces.

``001_sqlserver_instance_status.sql`` reports the engine's identity - product version, patch
level, edition, host, collation, start time - and ``002_*_database_status.sql`` reports each
database's compatibility level. None of it reached the report before: INSTANCE_STATUS was not
even loaded, and compatibility_level was read only from the SQL-Server-only DATABASE_CONFIG.
"""
from __future__ import annotations

import pytest

from db_ops.reports.inventory_health import (
    HEALTH_CODES,
    build_config_warnings,
    build_instance_health,
    parse_kv,
    parse_kv_any,
)

# The real shape emitted by 001: leading prose, ';' separators, 'N/A' placeholders, and an
# edition containing a colon, a comma-free parenthesis and spaces.
INSTANCE_MESSAGE = (
    "SQL Server connection is available. server=ERPDB01; ip=192.0.2.113; port=1433; "
    "transport=TCP; protocol=TSQL; auth=NTLM; version=16.0.4265.3; level=RTM; CU=CU26; "
    "update=KB5093420; edition=Enterprise Edition: Core-based Licensing (64-bit); "
    "engine=Enterprise-compatible; instance=MSSQLSERVER; machine=ERPDB01; physical=N/A; "
    "clustered=No; pid=4512; collation=SQL_Latin1_General_CP1_CI_AS; started=2026-07-01 03:12:44"
)


def _code_map(message=INSTANCE_MESSAGE, **rows):
    code_map = {
        ("INSTANCE_STATUS", "ERPDB01"): {
            "metric_item": "ERPDB01", "metric_value": "ONLINE", "status": "OK",
            "message": message, "collected_at": "2026-07-30T08:00:00Z",
        }
    }
    code_map.update(rows)
    return code_map


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_instance_status_is_loaded_at_all():
    """It was absent, which is why none of these fields could ever appear."""
    assert "INSTANCE_STATUS" in HEALTH_CODES


def test_semicolon_separated_pairs_are_parsed():
    kv = parse_kv_any(INSTANCE_MESSAGE)
    assert kv["version"] == "16.0.4265.3"
    assert kv["CU"] == "CU26"
    assert kv["update"] == "KB5093420"
    assert kv["port"] == "1433"


def test_a_value_containing_a_colon_and_parentheses_survives():
    assert parse_kv_any(INSTANCE_MESSAGE)["edition"] == "Enterprise Edition: Core-based Licensing (64-bit)"


def test_leading_prose_is_not_mistaken_for_a_pair():
    kv = parse_kv_any(INSTANCE_MESSAGE)
    assert "available" not in kv
    assert kv["server"] == "ERPDB01"


def test_comma_separated_messages_still_parse():
    """DATABASE_STATUS uses ',' while INSTANCE_STATUS uses ';'. One parser has to take both."""
    kv = parse_kv_any("database=SALESDB, state=ONLINE, read_only=0, compatibility_level=160")
    assert kv["database"] == "SALESDB"
    assert kv["compatibility_level"] == "160"


def test_a_value_does_not_run_past_its_separator():
    """The OS parser stops only at commas, so on a ';' message it swallowed the whole line."""
    kv = parse_kv_any("a=1; b=2; c=3")
    assert kv == {"a": "1", "b": "2", "c": "3"}


# --------------------------------------------------------------------------- #
# The instance block
# --------------------------------------------------------------------------- #
def test_build_reports_version_and_patch_separately():
    """'RTM' alone reads as unpatched; RTM-CU26 is 26 cumulative updates in."""
    instance = build_instance_health(_code_map())
    assert instance["product_version"] == "16.0.4265.3"
    assert instance["product_level"] == "RTM"
    assert instance["cumulative_update"] == "CU26"
    assert instance["update_reference"] == "KB5093420"
    assert instance["build"] == "16.0.4265.3 RTM"
    assert instance["patch"] == "CU26 KB5093420"


def test_na_placeholders_become_blank():
    """The collector writes 'N/A' where a property is unavailable; the report must not show it."""
    assert build_instance_health(_code_map())["physical_name"] == ""


def test_host_edition_and_endpoint_are_carried():
    instance = build_instance_health(_code_map())
    assert instance["edition"].startswith("Enterprise Edition")
    assert instance["engine_edition"] == "Enterprise-compatible"
    assert instance["machine_name"] == "ERPDB01"
    assert instance["clustered"] == "No"
    assert instance["collation"] == "SQL_Latin1_General_CP1_CI_AS"
    assert instance["started_at"] == "2026-07-01 03:12:44"
    assert (instance["listen_ip"], instance["listen_port"]) == ("192.0.2.113", "1433")


def test_no_instance_row_yields_nothing_rather_than_blanks():
    assert build_instance_health({}) == {}


def test_an_engine_reporting_only_a_version_still_works():
    """PostgreSQL's INSTANCE_STATUS carries far fewer fields; it must degrade, not break."""
    instance = build_instance_health(_code_map("version=18.4 (Debian 18.4-1.pgdg13+1)"))
    assert instance["product_version"].startswith("18.4")
    assert instance["edition"] == ""
    assert instance["cumulative_update"] == ""


# --------------------------------------------------------------------------- #
# Compatibility level below the engine's native level
# --------------------------------------------------------------------------- #
def _database(name, compat):
    return {
        "metric_item": name, "metric_value": "ONLINE", "status": "OK",
        "message": f"database={name}, state=ONLINE, read_only=0, compatibility_level={compat}",
        "collected_at": "2026-07-30T08:00:00Z",
    }


def test_databases_below_the_engine_native_level_are_flagged():
    """Found on the real fleet: 2008 R2 instances still running databases at level 80."""
    code_map = _code_map("version=10.50.1600.1; level=RTM")
    code_map[("DATABASE_STATUS", "Liberty")] = _database("Liberty", 80)
    code_map[("DATABASE_STATUS", "Maintenance")] = _database("Maintenance", 90)
    warnings = [w for w in build_config_warnings(code_map) if "compatibility level" in w]
    assert len(warnings) == 2
    assert any("80 is below the engine native 100" in w and "Liberty" in w for w in warnings)
    assert any("90 is below the engine native 100" in w for w in warnings)


def test_databases_at_the_native_level_are_not_flagged():
    code_map = _code_map("version=16.0.4265.3; level=RTM")
    code_map[("DATABASE_STATUS", "SALESDB")] = _database("SALESDB", 160)
    assert not [w for w in build_config_warnings(code_map) if "compatibility level" in w]


def test_databases_are_grouped_by_level_not_listed_one_warning_each():
    """The finding is "these were never raised after an upgrade", not a per-database fault."""
    code_map = _code_map("version=16.0.4265.3; level=RTM")
    for name in ("a", "b", "c", "d"):
        code_map[("DATABASE_STATUS", name)] = _database(name, 130)
    warnings = [w for w in build_config_warnings(code_map) if "compatibility level" in w]
    assert len(warnings) == 1
    assert "4 databases" in warnings[0]


def test_an_unknown_engine_version_flags_nothing():
    """No native level is known for PostgreSQL/Oracle here, so it must stay silent."""
    code_map = _code_map("version=18.4 (Debian)")
    code_map[("DATABASE_STATUS", "postgres")] = _database("postgres", 80)
    assert not [w for w in build_config_warnings(code_map) if "compatibility level" in w]
