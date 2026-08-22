"""`error_type` must say whether the MONITORING failed or the TARGET did.

The split is load-bearing: `event_policy.COLLECTOR_FAILURE_ERROR_TYPES` is what
`MetricStore.fetch_metric_freshness` uses to decide whether a metric is succeeding, and that
feeds the "Metric coverage" panel on the server report. Get it wrong and the monitoring lies
about itself — a healthy metric reported as a broken collector, which is the one failure mode
that makes every other reading untrustworthy.

It used to be guessed by keyword-matching the message of any non-OK row, so a *successful*
collection whose finding happened to contain the wrong word was filed as a collector failure.
Measured on the live store: 200 `LOG_RECENT_CRITICAL` rows became `CONNECT_FAILED` because the
metric's own SQL comment contains "timeout", and 27 `INSTANCE_STATUS` rows became `AUTH_FAILED`
because the finding they were reporting was failed logins on the target.
"""

from db_ops.lib.event_policy import COLLECTOR_FAILURE_ERROR_TYPES
from db_ops.metrics.collector import _metric_result


class _Metric:
    metric_code = "LOG_RECENT_CRITICAL"
    category = "errorlog"
    collector_type = "sql"


class _Target:
    target_id = "t1"
    server_id = "ACME-1"
    ip = "10.0.0.1"
    db_type = "sqlserver"
    db_name = "master"


def _result(**kwargs):
    return _metric_result(_Metric(), _Target(), 3, "2026-08-03T00:00:00Z", **kwargs)


def test_a_successful_finding_is_never_filed_as_a_collector_failure():
    """The exact live case: the metric ran fine and reported stack dumps, but its own message
    mentions "timeout", so it was classified CONNECT_FAILED."""
    row = _result(status="WARNING",
                  message="errors_24h=2727, last_or_running_sql=-- bounded to avoid a timeout")

    assert row.error_type == "CHECK_FAILED"
    assert row.error_type not in COLLECTOR_FAILURE_ERROR_TYPES


def test_a_finding_that_reports_failed_logins_is_not_an_auth_failure_of_the_collector():
    row = _result(status="WARNING", message="aws_admin: 10073 failed login attempts in 24h")

    assert row.error_type == "CHECK_FAILED"


def test_a_real_collector_failure_still_classifies_by_its_exception_text():
    """The exception path passes collector_failed=True, and only there is the message trusted to
    name the failure — that is the one place it genuinely describes the collector."""
    row = _result(status="ERROR", message="SQL failed: login timeout expired",
                  collector_failed=True)

    assert row.error_type == "CONNECT_FAILED"
    assert row.error_type in COLLECTOR_FAILURE_ERROR_TYPES


def test_an_auth_failure_from_the_exception_path_is_still_auth_failed():
    row = _result(status="ERROR", message="Login failed for user 'db_ops'", collector_failed=True)

    assert row.error_type == "AUTH_FAILED"


def test_an_ok_row_carries_no_error_type_at_all():
    assert _result(status="OK", message="SQL returned no rows.").error_type is None
