"""Tests for the instance/build facts the inventory report surfaces.

``001_sqlserver_instance_status.sql`` reports the engine's identity - product version, patch
level, edition, host, collation, start time - and ``002_*_database_status.sql`` reports each
database's compatibility level. None of it reached the report before: INSTANCE_STATUS was not
even loaded, and compatibility_level was read only from the SQL-Server-only DATABASE_CONFIG.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from db_ops.reports.inventory_health import (
    HEALTH_CODES,
    build_config_warnings,
    build_instance_health,
    parse_kv,
    parse_kv_any,
)
from db_ops.reports.inventory_report import _build_detail

REPO_ROOT = Path(__file__).resolve().parents[1]

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


# --------------------------------------------------------------------------- #
# Oracle and PostgreSQL, in their own words (0.28.0)
# --------------------------------------------------------------------------- #
# Until 0.28.0 an Oracle or PostgreSQL server's INSTANCE_STATUS said three things, beside nineteen
# for SQL Server - not because those engines say less, but because their collectors asked less.
# These are the messages the three collectors wrote on 2026-10-09 against a 26ai lab, an 8.1.7
# instance and a PostgreSQL 18 server, with the names replaced.
ORACLE_MESSAGE = (
    "Oracle instance status=OPEN; instance=FREE; machine=oralab01; version=23.0.0.0.0; "
    "edition=Free; banner=Oracle AI Database 26ai Free Release 23.26.3.0.0 - Develop Learn and "
    "Run for Free; database=FREE; unique_name=FREE; role=PRIMARY; open_mode=READ WRITE; "
    "log_mode=ARCHIVELOG; platform=Linux x86 64-bit; charset=AL32UTF8; rac=NO; logins=ALLOWED; "
    "archiver=STARTED; database_status=ACTIVE; instance_role=PRIMARY_INSTANCE; "
    "started=2026-10-02 09:38:03"
)
ORACLE_8I_MESSAGE = (
    "instance=orcl, status=OPEN, version=8.1.7.0.0, host=ORA8I01, machine=ORA8I01, "
    "edition=Standard, banner=Oracle8i Release 8.1.7.0.0 - Production, database=ORCL, "
    "open_mode=READ WRITE, log_mode=NOARCHIVELOG, charset=WE8ISO8859P1, rac=NO, logins=ALLOWED, "
    "archiver=STOPPED, started=2026-10-05 14:28:49"
)
ORACLE_8I_MESSAGE_UNTIL_0_28 = "instance=orcl, status=OPEN, version=8.1.7.0.0, host=ORA8I01"
POSTGRESQL_MESSAGE = (
    "PostgreSQL connection available. version=18.6 (Debian 18.6-1.pgdg13+2); "
    "build=PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2); platform=x86_64-pc-linux-gnu; role=primary; "
    "ip=192.0.2.10; port=5432; transport=TCP; ssl=off; connected_database=postgres; "
    "database_collation=en_US.utf8; encoding=UTF8; timezone=Etc/UTC; max_connections=100; "
    "wal_level=replica; archive_mode=on; started=2026-10-02 09:37:10; uptime_seconds=584549"
)

COLLECTORS = REPO_ROOT / "db_ops" / "metrics" / "collectors"


def _detail(message):
    return dict(_build_detail({"instance_health": build_instance_health(_code_map(message))}, {}))


def test_an_oracle_instance_says_who_and_what_it_is():
    instance = build_instance_health(_code_map(ORACLE_MESSAGE))

    assert instance["machine_name"] == "oralab01"
    assert instance["product_version"] == "23.0.0.0.0"
    assert instance["edition"] == "Free"
    assert instance["banner"].startswith("Oracle AI Database 26ai Free Release 23.26.3.0.0")
    assert (instance["database_name"], instance["unique_name"]) == ("FREE", "FREE")
    assert (instance["role"], instance["open_mode"]) == ("PRIMARY", "READ WRITE")
    assert (instance["log_mode"], instance["archiver"]) == ("ARCHIVELOG", "STARTED")
    assert instance["platform"] == "Linux x86 64-bit"
    assert instance["charset"] == "AL32UTF8"
    assert instance["started_at"] == "2026-10-02 09:38:03"


def test_a_postgresql_server_says_who_and_what_it_is():
    instance = build_instance_health(_code_map(POSTGRESQL_MESSAGE))

    assert instance["product_version"] == "18.6 (Debian 18.6-1.pgdg13+2)"
    assert instance["banner"] == "PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2)"
    assert instance["platform"] == "x86_64-pc-linux-gnu"
    assert instance["role"] == "primary"
    assert (instance["listen_ip"], instance["listen_port"]) == ("192.0.2.10", "5432")
    assert instance["charset"] == "UTF8"
    assert (instance["wal_level"], instance["archive_mode"]) == ("replica", "on")
    assert instance["started_at"] == "2026-10-02 09:37:10"


def test_nothing_is_borrowed_from_another_engines_vocabulary():
    """A PostgreSQL server has no machine name to ask for and no edition; Oracle no WAL level.

    Blank, not 'N/A' under SQL Server's name and not a guess: the page shows a row only for what
    the engine stated.
    """
    postgresql = build_instance_health(_code_map(POSTGRESQL_MESSAGE))
    oracle = build_instance_health(_code_map(ORACLE_MESSAGE))

    assert (postgresql["machine_name"], postgresql["edition"], postgresql["log_mode"]) == ("", "", "")
    assert (postgresql["cumulative_update"], postgresql["collation"]) == ("", "")
    assert (oracle["wal_level"], oracle["listen_ip"], oracle["collation"]) == ("", "", "")
    assert "Server collation" not in _detail(POSTGRESQL_MESSAGE)
    assert "WAL" not in _detail(ORACLE_MESSAGE) and "Log mode" not in _detail(POSTGRESQL_MESSAGE)


def test_a_postgresql_collation_is_shown_as_its_databases_not_the_servers():
    """A cluster has no single collation; the one collected is the connected database's."""
    assert _detail(POSTGRESQL_MESSAGE)["Database collation"] == "en_US.utf8 (database postgres)"


def test_each_engine_gets_rows_in_its_own_words():
    oracle, postgresql = _detail(ORACLE_MESSAGE), _detail(POSTGRESQL_MESSAGE)

    assert oracle["Role"] == "PRIMARY · READ WRITE"
    assert oracle["Log mode"] == "ARCHIVELOG · archiver STARTED"
    assert oracle["Character set"] == "AL32UTF8"
    assert oracle["Instance host"] == "oralab01"
    assert postgresql["Role"] == "primary"
    assert postgresql["WAL"] == "wal_level replica · archive_mode on"
    assert postgresql["Listening on"] == "192.0.2.10:5432 · TCP"
    assert postgresql["Server settings"] == "max_connections 100 · timezone Etc/UTC · ssl off"


def test_an_8i_instance_states_an_open_mode_and_no_role():
    """8i has no database role; its READ WRITE under "Role" would read as one."""
    detail = _detail(ORACLE_8I_MESSAGE)

    assert detail["Open mode"] == "READ WRITE" and "Role" not in detail
    assert detail["Edition"] == "Standard"
    assert detail["Log mode"] == "NOARCHIVELOG · archiver STOPPED"


def test_an_8i_row_collected_before_0_28_still_names_its_machine():
    """The 8i variant wrote `host=` and the report read `machine=`: collected, never shown."""
    instance = build_instance_health(_code_map(ORACLE_8I_MESSAGE_UNTIL_0_28))

    assert instance["machine_name"] == "ORA8I01"
    assert instance["banner"] == "" and instance["charset"] == ""


def test_the_sql_server_rows_are_the_ones_they_were():
    assert list(_detail(INSTANCE_MESSAGE)) == [
        "Engine build", "Edition", "Instance host", "Listening on", "Server collation",
        "Engine started"]


@pytest.mark.parametrize(("sql_file", "message"), [
    ("oracle/001_oracle_instance_status.sql", ORACLE_MESSAGE),
    ("oracle/legacy_8i/001_oracle_instance_status.sql", ORACLE_8I_MESSAGE),
    ("postgresql/001_postgresql_instance_status.sql", POSTGRESQL_MESSAGE),
])
def test_the_message_pinned_here_is_the_one_its_collector_writes(sql_file, message):
    """The fixtures above are only worth anything while the collectors still write those keys.

    Each key of the pinned message has to appear in the SQL as the literal that writes it, so a
    key renamed in a collector fails here instead of going blank on the page.
    """
    sql = (COLLECTORS / sql_file).read_text(encoding="utf-8")
    missing = [key for key in parse_kv_any(message) if f"{key}=" not in sql]

    assert missing == [], f"{sql_file} no longer writes: {missing}"


@pytest.mark.parametrize("sql_file", [
    "oracle/001_oracle_instance_status.sql",
    "oracle/legacy_8i/001_oracle_instance_status.sql",
    "postgresql/001_postgresql_instance_status.sql",
])
def test_no_comment_in_a_collector_holds_a_semicolon_or_a_quote(sql_file):
    """What each file's own header promises: no client has to parse a comment to find the end."""
    lines = (COLLECTORS / sql_file).read_text(encoding="utf-8").splitlines()
    comments = [line for line in lines if line.lstrip().startswith("--")]

    assert comments and not [line for line in comments if any(c in line for c in ";'\"")]
