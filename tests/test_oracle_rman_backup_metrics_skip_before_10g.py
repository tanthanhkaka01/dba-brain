"""The Oracle RMAN backup metrics skip an instance older than their views, on every target at once.

`BACKUP_AGE` and `BACKUP_LAST_RESULT` read `v$rman_backup_job_details`, which exists from 10g
Release 2. Their one Oracle variant claimed every release, so on 8i the statement failed with
ORA-00933 / ORA-00900 every cycle and each 8i record had to switch the two metrics off for itself -
first through `report_policy.disabled_metric_codes`, which since 0.31.0 no longer stops collection,
then through `metric_overrides` with a reason (2026-10-23, 0.31.0 sheet 1.189). A version bound in
the catalogue says it once: before 10g the metric is unsupported, with its reason, as `JOB_FAILED`
already is.
"""
from __future__ import annotations

import pytest

from dbabrain.lib.paths import PACKAGE_DIR
from dbabrain.lib.target_profile import TargetProfile, select_variant
from dbabrain.metrics.definitions import load_metric_definitions

CATALOGUE = PACKAGE_DIR / "metrics" / "catalogue" / "metric_definitions.json"


def _oracle_variants(code):
    metric = next(m for m in load_metric_definitions(CATALOGUE) if m.metric_code == code)
    return [v for v in metric.variants if v.db_type == "oracle"]


@pytest.mark.parametrize("code", ["BACKUP_AGE", "BACKUP_LAST_RESULT"])
def test_an_oracle_8i_runs_no_rman_sql(code):
    assert select_variant(_oracle_variants(code), TargetProfile(db_type="oracle", major_version=8)) is None


@pytest.mark.parametrize("code, file", [("BACKUP_AGE", "005_oracle_backup_age.sql"),
                                        ("BACKUP_LAST_RESULT", "022_oracle_last_backup_result.sql")])
def test_an_oracle_from_10g_still_runs_it(code, file):
    for major in (10, 19, 23):
        chosen = select_variant(_oracle_variants(code), TargetProfile(db_type="oracle", major_version=major))
        assert chosen is not None and chosen.path.name == file


@pytest.mark.parametrize("code", ["BACKUP_AGE", "BACKUP_LAST_RESULT"])
def test_the_gap_says_why(code):
    unsupported = [v for v in _oracle_variants(code) if not v.supported]
    assert len(unsupported) == 1 and unsupported[0].max_major_version == 9
    assert "10g Release 2" in unsupported[0].unsupported_reason
