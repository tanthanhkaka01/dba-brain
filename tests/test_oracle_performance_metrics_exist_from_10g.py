"""The Oracle SLA policies measure something on a database newer than 8i (1.150).

`ORACLE_PERFORMANCE_24H` reads SHARED_POOL_FREE, LIBRARY_CACHE and BUFFER_CACHE_HIT, and
`ORACLE_TABLESPACE_CAPACITY_24H` read TABLESPACE_FREE_SPACE - all four existed for Oracle 8i only. On
every 9i-and-later database the performance policy measured nothing, and the capacity policy only
PROCESS_LIMIT.

From 10g the three ratios read `v$sysmetric`'s 60-second interval - current, where the 8i files
divide counters kept since startup - at check_oracle_health's published defaults (the operator:
do what other monitors do). Inside a PDB `v$sysmetric` answers nothing: the instance-wide two say
so at LOGGING (not there is not a failure), and from 18c the buffer cache ratio is the PDB's own,
from `v$con_sysmetric`. The capacity policy reads STORAGE_DATA_FILE_SPACE, graded per tablespace
since 1.138. Run on the `.251` 26ai PDB, and the root's path on copies fed synthetic values.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ORACLE = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "oracle"
CATALOGUES = ("data/metric_definitions.json", "data/metric_definitions.example.json",
              "dbabrain/metrics/catalogue/metric_definitions.json")
RATIOS = {
    "045_oracle_shared_pool_free.sql": ("Shared Pool Free %", 10, 5),
    "046_oracle_library_cache.sql": ("Library Cache Hit Ratio", 98, 95),
    "047_oracle_buffer_cache_hit.sql": ("Buffer Cache Hit Ratio", 98, 95),
}


def _code(name: str) -> str:
    lines = [line.split("--", 1)[0] for line in (ORACLE / name).read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


@pytest.mark.parametrize("name", sorted(RATIOS))
def test_each_ratio_is_the_last_minute_at_check_oracle_healths_defaults(name):
    metric, warning, critical = RATIOS[name]
    code = _code(name)
    assert f"FROM v$sysmetric WHERE metric_name = '{metric}' AND group_id = 2" in code
    assert f"SELECT {warning} AS warning_below_pct, {critical} AS critical_below_pct FROM dual" in code
    assert "WHEN m.free_pct < p.critical_below_pct THEN 'CRITICAL'" in code or \
           "WHEN m.hit_pct < p.critical_below_pct THEN 'CRITICAL'" in code


@pytest.mark.parametrize("name", sorted(RATIOS))
def test_inside_a_pdb_the_instance_figure_is_logged_where_it_lives(name):
    code = _code(name)
    assert re.search(r"'LOGGING', 'not readable in this container .*WHERE NOT EXISTS \(SELECT 1 FROM m\)", code)


def test_from_18c_the_buffer_cache_ratio_of_a_pdb_is_its_own():
    code = _code("047_oracle_buffer_cache_hit_18c.sql")
    assert "FROM v$con_sysmetric WHERE metric_name = 'Buffer Cache Hit Ratio' AND group_id = 18" in code
    assert "AND NOT EXISTS (SELECT 1 FROM v$sysmetric WHERE metric_name = 'Buffer Cache Hit Ratio' AND group_id = 2)" in code
    assert "v$con_sysmetric" not in _code("047_oracle_buffer_cache_hit.sql")  # 10g - 17 cannot name it


@pytest.mark.parametrize("name", ["047_oracle_buffer_cache_hit.sql", "047_oracle_buffer_cache_hit_18c.sql"])
def test_a_buffer_cache_ratio_of_a_minute_with_almost_no_reads_is_not_graded(name):
    """An idle PDB answers 0 for a minute it read nothing in, and read CRITICAL for it (1.179)."""
    code = _code(name)
    assert "idle AS ( SELECT 100 AS min_logical_reads_per_sec FROM dual )" in code
    assert ("WHEN NVL(m.logical_reads_per_sec, 0) < i.min_logical_reads_per_sec THEN 'OK' "
            "WHEN m.hit_pct < p.critical_below_pct THEN 'CRITICAL'") in code
    assert "metric_name = 'Logical Reads Per Sec'" in code


@pytest.mark.parametrize("relative", CATALOGUES)
def test_every_oracle_version_from_10g_has_a_variant_of_each(relative):
    data = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
    metrics = {m["metric_code"]: m for m in data["metrics"]}
    for code in ("SHARED_POOL_FREE", "LIBRARY_CACHE", "BUFFER_CACHE_HIT"):
        oracle = [v for v in metrics[code]["variants"] if v["db_type"] == "oracle" and v.get("file")]
        covered = set()
        for variant in oracle:
            low = variant.get("min_major_version") or 0
            high = variant.get("max_major_version") or 26
            covered.update(range(low, high + 1))
        assert set(range(10, 27)) <= covered, code
        assert any(v.get("name") == "oracle_9i_unsupported" and v.get("supported") is False
                   for v in metrics[code]["variants"]), code


def test_the_capacity_policy_reads_the_tablespace_metric_modern_oracle_has():
    data = json.loads((REPO_ROOT / "data" / "sla_policies.json").read_text(encoding="utf-8"))
    found = [p for p in data["sla_policies"] if p.get("policy_id") == "ORACLE_TABLESPACE_CAPACITY_24H"]
    assert found, "ORACLE_TABLESPACE_CAPACITY_24H"
    assert {"TABLESPACE_FREE_SPACE", "STORAGE_DATA_FILE_SPACE", "PROCESS_LIMIT"} <= set(found[0]["metric_codes"])
