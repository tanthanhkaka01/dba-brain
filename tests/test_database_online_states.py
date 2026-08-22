"""What "this database is up" means, on every engine and on both pages.

`DATABASE_STATUS` is collected for all three engines and each of them words the answer its own
way: SQL Server reports `sys.databases.state_desc` (`ONLINE`) and PostgreSQL follows it, while
Oracle reports `v$database.open_mode`, whose healthy value is `READ WRITE`. The reports compared
against the literal string `ONLINE`, so every Oracle instance in the fleet read `0/1 databases
online` — permanently, and graded WARNING on it — while the database was open and serving. A
standing false alarm is worse than a missing column: it is how an operator learns to stop reading
that column at all.

Nothing here collects anything. The value being judged is already a row in `metric_results`; this
is only the rule for reading it, and it lives in one place so the fleet page and the per-server
page cannot disagree about the same database.
"""

import re
from pathlib import Path

from db_ops.lib.inventory_render import ONLINE_DATABASE_STATES, is_database_online
from db_ops.reports.server_report import TEMPLATE_HTML, build_databases


def test_an_oracle_database_open_read_write_is_online():
    assert is_database_online("READ WRITE")
    assert is_database_online("read write")      # the reading must not depend on the casing
    assert is_database_online(" READ WRITE ")


def test_the_states_that_mean_the_database_cannot_be_used_are_not_online():
    for state in ("MOUNTED", "RESTORING", "RECOVERING", "SUSPECT", "OFFLINE", "EMERGENCY", ""):
        assert not is_database_online(state), state


def test_a_missing_state_is_not_read_as_online():
    """An absent value means the metric said nothing, not that the database is fine."""
    assert not is_database_online(None)


def test_the_per_server_database_table_counts_an_oracle_instance_as_online():
    section = build_databases({
        ("DATABASE_STATUS", "LEGACYDB"): {
            "metric_code": "DATABASE_STATUS", "metric_item": "LEGACYDB", "metric_value": "READ WRITE",
            "status": "OK", "collected_at": "2026-08-17T05:45:13Z",
            "message": "database=LEGACYDB, open_mode=READ WRITE, log_mode=NOARCHIVELOG, compatible=8.1.0",
        },
    })

    assert section["summary"]["count"] == 1
    assert section["summary"]["online"] == 1


def test_the_page_javascript_lists_the_same_states_the_python_counts():
    """The browser paints the row and Python counts the summary, from two copies of one rule.
    They drift silently — the count says 1/1 online above a row painted red — so the list is
    asserted rather than trusted."""
    html = Path(TEMPLATE_HTML).read_text(encoding="utf-8")
    match = re.search(r"const ONLINE_STATES = \[(.*?)\];", html, re.S)
    assert match, "server_report.html no longer declares ONLINE_STATES"
    in_page = {part.strip().strip('"') for part in match.group(1).split(",") if part.strip()}

    assert in_page == set(ONLINE_DATABASE_STATES)
