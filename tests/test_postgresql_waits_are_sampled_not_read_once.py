"""PostgreSQL's waits are sampled inside one statement, and each sample is a new reading (1.162).

PERFORMANCE_WAIT_STATS had no PostgreSQL variant: PostgreSQL keeps no cumulative wait counters,
only pg_stat_activity's wait_event at the instant it is read, so the metric has to read it several
times and count. 1.162 was set aside on 2026-10-15 as "not buildable as a collector": 14 reads in
one statement, 0.5 s apart with pg_stat_clear_snapshot() between them, all returned the first one.
The snapshot was not the whole cause. A function in FROM is materialised once and *rewound* on
every later scan while its argument does not change, so the cleared snapshot was never read again.
Calling pg_stat_get_activity() with an argument that depends on the sample row makes every sample
call it anew - measured on the lab: a pg_sleep(4) session in samples 1 - 4 of 8 and gone after.

These tests hold that shape, because it is the kind of thing a tidy-up undoes - "use the view, it
is simpler" puts every sample back on the first one - and the grading ported from SQL Server's
014: an idle server is not graded, a lock wait is evidence and never a second siren.
"""

from __future__ import annotations

import re
from pathlib import Path

from dbabrain.metrics.definitions import DEFAULT_DEFINITIONS_PATH, load_metric_definitions

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL = (REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "postgresql"
       / "014_postgresql_wait_stats.sql").read_text(encoding="utf-8")
CODE = re.sub(r"\s+", " ", " ".join(line.split("--", 1)[0] for line in SQL.splitlines()))


def test_each_sample_calls_the_function_again_after_its_own_pause_and_clear():
    # The function itself, not the pg_stat_activity view whose constant argument is rewound.
    assert "FROM pg_stat_activity" not in CODE
    assert re.search(r"LEFT JOIN LATERAL pg_stat_get_activity\(CASE WHEN pause\.at IS NOT NULL "
                     r"THEN NULL::integer END\)", CODE)
    pause = re.search(r"CROSS JOIN LATERAL \( SELECT (.*?) \) AS pause", CODE).group(1)
    assert "pg_sleep(CASE WHEN g.i > 1" in pause and "pg_stat_clear_snapshot()" in pause


def test_the_sample_is_short_and_its_numbers_live_in_one_params_cte():
    params = re.search(r"WITH params AS \( SELECT (.*?) \)", CODE).group(1)
    assert "10 AS samples" in params
    assert "0.5::float8 AS interval_seconds" in params
    assert "2.0::numeric AS busy_floor_sessions" in params
    assert "50.0::numeric AS busy_share_pct" in params


def test_only_active_client_sessions_are_sampled_and_never_this_one():
    assert "a.backend_type = 'client backend'" in CODE
    assert "a.state = 'active'" in CODE
    assert "a.pid <> pg_backend_pid()" in CODE


def test_an_idle_server_is_not_graded_and_only_a_dominant_pressure_wait_warns():
    status = re.search(r"CAST\(CASE (.*?) END AS varchar\(16\)\) AS status", CODE).group(1)
    floor, pressure = re.findall(r"WHEN (.*?) THEN", status)
    # The floor first: under it, whatever waited is not a verdict.
    assert floor.endswith("< p.busy_floor_sessions")
    assert pressure.startswith("r.pressure IS NOT NULL") and pressure.endswith(">= p.busy_share_pct")
    assert status.endswith("ELSE 'OK'")
    assert "CRITICAL" not in CODE


def test_a_lock_wait_is_ranked_as_evidence_and_never_graded():
    pressure = re.search(r"CASE (WHEN c\.wait_type = 'IO'.*?) END AS pressure", CODE).group(1)
    assert "'Lock'" not in pressure
    assert "graded_by=LOCK_BLOCKING_SESSIONS" in CODE


def test_the_session_waiting_on_its_client_or_asleep_is_not_the_server_waiting():
    assert "WHERE c.wait_type NOT IN ('Client', 'Timeout', 'Extension', 'Activity', 'CPU')" in CODE


def test_an_idle_sample_says_it_was_idle_rather_than_healthy():
    assert "The server was idle, not healthy" in SQL
    assert "WHERE NOT EXISTS (SELECT 1 FROM waits)" in CODE


def test_the_catalogue_runs_it_on_postgresql_10_and_later():
    metric = next(item for item in load_metric_definitions(DEFAULT_DEFINITIONS_PATH)
                  if item.metric_code == "PERFORMANCE_WAIT_STATS")
    postgres = [variant for variant in metric.variants if variant.db_type == "postgresql"]
    assert len(postgres) == 1 and postgres[0].supported
    assert postgres[0].min_major_version == 10
    assert postgres[0].path.name == "014_postgresql_wait_stats.sql"
