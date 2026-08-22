"""`status = 'sleeping'` is an instant, not a duration — so it cannot decide an alert on its own.

SQL Server sets a session to `sleeping` the moment a statement finishes and the connection waits
for the client's next command. D365 F&O opens a TTS block and then issues many small statements
with application think-time between them, so at any sampled moment most sessions inside a
transaction read as sleeping with `idle_seconds = 0`. They are working, not abandoned.

The metric used to let blocking alone decide severity, which on 2026-08-10 raised a CRITICAL for
SPID 723 — `idle_seconds=0`, mid-INSERT, whose `last_request_end` was later than the report header
it appeared in. It was also a duplicate: `LOCK_BLOCKING_SESSIONS` had already reported the same
SPID, correctly, because a live session blocking someone is what that metric is for.

So idle time is a required condition here now. These tests pin the two directions of that change
and the blind spot it closes — a session busy enough that `idle_seconds` never rises, holding one
transaction open for half an hour.

The grading itself lives in T-SQL and cannot run in this offline suite, so what is pinned here is
the contract the SQL must keep: the thresholds it declares, that idle gates every blocking rule,
that transaction age is carried, and that the shared report policy is not silently grading these
rows on fields this variant does not emit.
"""

import json
import re
from pathlib import Path

import pytest
from db_ops.lib.paths import resolve_tool_path
from conftest import shipped_config

DEFINITIONS = shipped_config("metric_definitions.json")
MODERN_SQL = resolve_tool_path("assets/metrics/sqlserver/024_sqlserver_sleeping_open_transaction.sql")
ORACLE_SQL = resolve_tool_path("assets/metrics/oracle/024_oracle_sleeping_open_transaction.sql")


@pytest.fixture(scope="module")
def sql_text():
    return MODERN_SQL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def definition():
    doc = json.loads(DEFINITIONS.read_bytes().decode("utf-8-sig"))
    return {m["metric_code"]: m for m in doc["metrics"]}["LOCK_SLEEPING_OPEN_TRANSACTION"]


def _status_branches(sql: str) -> str:
    """The CASE expression that produces `status`, without the message-building text below it."""
    start = sql.index("END AS status")
    return sql[sql.rindex("CASE", 0, start):start]


def test_the_idle_floor_is_declared_and_shorter_than_a_human_walking_away(sql_text):
    """One minute is far longer than any AOS round trip and far shorter than an abandoned session.

    Pinning the value matters because raising it hides real abandoned blockers and lowering it
    brings back the false CRITICAL that motivated the change.
    """
    match = re.search(r"@idle_abandoned_seconds\s+int\s*=\s*(\d+)", sql_text)
    assert match, "the idle floor must stay a named, declared threshold"
    assert int(match.group(1)) == 60


def test_every_blocking_branch_is_gated_by_idle_time(sql_text):
    """This is the whole fix. A blocking branch without an idle gate re-raises SPID 723."""
    branches = _status_branches(sql_text)
    blocking_branches = [
        line for line in branches.splitlines()
        if "blocked_sessions" in line and line.strip().startswith(("WHEN", "AND"))
    ]
    assert blocking_branches, "expected the CASE to grade on blocked_sessions"

    # Each WHEN that escalates on blocking must carry either the idle floor or the transaction-age
    # rule; those are the only two things that distinguish an abandoned holder from a working one.
    whens = [chunk for chunk in branches.split("WHEN")[1:] if "blocked_sessions" in chunk]
    for chunk in whens:
        assert (
            "@idle_abandoned_seconds" in chunk or "@tran_age_critical_seconds" in chunk
        ), f"blocking branch with no idle or transaction-age gate:\n{chunk.strip()[:200]}"


def test_a_busy_session_holding_an_old_transaction_is_still_reachable(sql_text):
    """The blind spot: idle_seconds pinned at 0 because the session never stops working, while one
    transaction has been open for half an hour and its locks with it. No idle rule can see it."""
    match = re.search(r"@tran_age_critical_seconds\s+int\s*=\s*(\d+)", sql_text)
    assert match, "transaction age must be a declared threshold, not a literal in the CASE"
    assert int(match.group(1)) == 1800

    branches = _status_branches(sql_text)
    assert "transaction_begin_time" in branches, "transaction age must take part in grading"


def test_transaction_age_is_reported_so_a_row_can_be_judged_without_the_server(sql_text):
    """Two rows with the same idle_seconds can mean 'mid-TTS block' or 'holding since lunchtime'.
    Only the transaction age separates them, so it belongs in the message."""
    assert "tran_age_seconds=" in sql_text
    assert "tran_begin=" in sql_text


def test_one_session_cannot_become_several_rows(sql_text):
    """`dm_tran_session_transactions` returns a row per enlisted transaction, so a nested or
    multi-database transaction would fan one SPID out into several metric rows for the same
    finding — and the 500-row cap would then drop somebody else's."""
    assert "OUTER APPLY" in sql_text
    assert re.search(r"SELECT\s+MIN\(tat\.transaction_begin_time\)", sql_text), (
        "the transaction lookup must aggregate, not join"
    )


def test_the_shared_report_policy_does_not_grade_this_variant_on_fields_it_never_emits(
    sql_text, definition
):
    """The definition is shared by the SQL Server, legacy 2008 R2 and Oracle variants.

    Oracle deliberately emits `idle_minutes` and `is_blocking` for the policy to grade on; this
    variant emits `idle_seconds` and `blocked_sessions` and grades itself in SQL. That difference
    is fine, but it is invisible: editing the policy thresholds expecting them to change SQL
    Server behaviour would do nothing at all. This test states the split so the next reader is
    not misled by a silent no-op.
    """
    thresholds = definition["report_policy"]["severity_policy"].get("thresholds", [])
    policy_fields = {rule["field"] for rule in thresholds}
    policy_fields |= {
        cond["field"] for rule in thresholds for cond in rule.get("unless", [])
    }

    emitted_here = set(re.findall(r"', (\w+)=' \+", sql_text)) | set(
        re.findall(r"\+ ', (\w+)='", sql_text)
    )
    assert "idle_seconds" in emitted_here and "blocked_sessions" in emitted_here

    # None of the policy's grading fields come from this variant.
    assert not (policy_fields & emitted_here), (
        f"policy fields {sorted(policy_fields & emitted_here)} now come from the SQL Server "
        "variant too — the policy and the SQL would both grade, and they can disagree"
    )

    # They do come from Oracle, which is why the policy stays.
    oracle = ORACLE_SQL.read_text(encoding="utf-8")
    assert "idle_minutes=" in oracle and "is_blocking=" in oracle
