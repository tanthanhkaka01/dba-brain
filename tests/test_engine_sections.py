"""Every page that lists the estate sections it the same way: SQL Server, Oracle, PostgreSQL, Host only.

The operator asked (2026-10-02) for the instance pickers and the SLA page to be split by engine. One
rule in ``lib`` decides the section, so a target cannot be "SQL Server" on one page and "Other" on
the next because two pages spelled ``postgres`` differently.
"""

from __future__ import annotations

from db_ops.lib import engine_sections


def test_every_spelling_of_an_engine_lands_in_one_section():
    assert engine_sections.section_of("sqlserver") == "sqlserver"
    assert engine_sections.section_of("MSSQL") == "sqlserver"
    assert engine_sections.section_of("postgres") == "postgresql"
    assert engine_sections.section_of("PostgreSQL") == "postgresql"
    assert engine_sections.section_of("oracle") == "oracle"


def test_a_target_with_no_database_is_host_only():
    assert engine_sections.section_of("host") == "host"
    assert engine_sections.section_of("") == "host"
    assert engine_sections.section_of(None) == "host"


def test_the_caller_saying_os_only_wins_over_a_db_type():
    assert engine_sections.section_of("sqlserver", os_only=True) == "host"


def test_an_engine_the_list_does_not_know_is_shown_as_other_and_never_dropped():
    assert engine_sections.section_of("db2") == "other"
    assert engine_sections.present_sections(["db2"]) == [("other", "Other")]


def test_sections_come_in_page_order_and_only_when_present():
    present = engine_sections.present_sections(["host", "postgresql", "sqlserver", "sqlserver"])
    assert present == [("sqlserver", "SQL Server"), ("postgresql", "PostgreSQL"), ("host", "Host only")]
    assert engine_sections.section_rank("sqlserver") < engine_sections.section_rank("oracle")
    assert engine_sections.section_rank("postgresql") < engine_sections.section_rank("host")


def test_a_section_has_a_label_for_the_page():
    assert engine_sections.section_label("host") == "Host only"
    assert engine_sections.section_label("nonsense") == "Other"
