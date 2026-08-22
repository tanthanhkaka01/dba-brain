"""An observation the collector could not take is not an observation of the service.

The SLA evaluator counted every non-OK row as a service failure. On 192.0.2.250 all 26 bad
observations in `SQLSERVER_BACKUP_JOB_7D` were authentication failures recorded while the
collector could not connect — not one of them was a backup-job result. The verdict said backups
were breaching their objective; what was actually broken was a login.

That mistake also multiplies: one unreachable target fails every policy that touches it, so a
single collector incident is reported as a dozen independent SLO breaches across several domains.

The split has one edge that matters more than the rest. A policy whose samples were *all*
collection failures must read NO_DATA, never 100%. Removing the bad rows and declaring the
remainder compliant would be a worse lie than the one being fixed: it turns "we cannot see this
server" into "this server is healthy".
"""

from __future__ import annotations

from db_ops.sla import compliance


class _Row(dict):
    """A stored metric sample; the evaluator reads it like a mapping."""

    def __init__(self, status="OK", error_type="", collected_at="2026-08-05T00:00:00Z"):
        super().__init__(status=status, error_type=error_type, collected_at=collected_at,
                         metric_code="INSTANCE_STATUS", metric_value="1", metric_item="x",
                         target_id="srv/sqlserver/svc")


def test_the_four_collector_failures_are_recognised_as_data_quality():
    for error_type in ("AUTH_FAILED", "CONNECT_FAILED", "QUERY_FAILED", "PERMISSION_DENIED"):
        assert compliance._is_collection_failure(_Row(status="WARNING", error_type=error_type))


def test_a_genuine_check_finding_is_not_data_quality():
    """CHECK_FAILED is the service saying no. It must keep counting against the objective."""
    assert not compliance._is_collection_failure(_Row(status="WARNING", error_type="CHECK_FAILED"))
    assert not compliance._is_collection_failure(_Row(status="CRITICAL", error_type=""))
    assert not compliance._is_collection_failure(_Row(status="OK"))


def test_the_classification_is_case_and_whitespace_insensitive():
    assert compliance._is_collection_failure(_Row(status="WARNING", error_type="  auth_failed "))


def test_a_sample_from_before_the_column_existed_counts_as_a_measurement():
    """Older stored rows have no error_type. Reading them as collection failures would silently
    empty out historical windows; reading them as measurements is what they always were."""
    row = dict(status="WARNING", collected_at="2026-08-01T00:00:00Z")
    assert not compliance._is_collection_failure(row)


def test_collection_failures_do_not_count_against_the_objective():
    """The 26-auth-failure case: with them excluded the policy meets its objective, because the
    measurements that did happen were all good."""
    rows = [_Row(status="OK") for _ in range(4)]
    rows += [_Row(status="WARNING", error_type="AUTH_FAILED") for _ in range(26)]

    result = compliance._build_result(
        _policy(), "srv/sqlserver/svc", rows, "2026-08-01T00:00:00Z", "2026-08-05T00:00:00Z")

    assert result.good_count == 4
    assert result.bad_count == 0
    assert result.actual_percent == 100.0
    assert result.status == "PASSED"


def test_the_collection_loss_is_still_reported_as_a_data_quality_verdict():
    """Excluded is not ignored — silence about a target nobody can log into is the failure mode
    this whole change exists to avoid."""
    rows = [_Row(status="OK")] + [_Row(status="WARNING", error_type="CONNECT_FAILED")] * 3

    result = compliance._build_result(
        _policy(), "srv/sqlserver/svc", rows, "2026-08-01T00:00:00Z", "2026-08-05T00:00:00Z")

    assert result.data_quality_status == "COLLECTION_FAILED"
    assert result.coverage_percent == 25.0


def test_a_policy_whose_samples_were_all_collection_failures_is_no_data_not_compliant():
    """The edge that matters most: never turn "we cannot see it" into "it is healthy"."""
    rows = [_Row(status="WARNING", error_type="AUTH_FAILED") for _ in range(10)]

    result = compliance._build_result(
        _policy(), "srv/sqlserver/svc", rows, "2026-08-01T00:00:00Z", "2026-08-05T00:00:00Z")

    assert result.no_data or result.status in {"NO_DATA", "FAILED"}
    assert result.status != "PASSED"
    assert result.good_count == 0


def test_a_real_service_failure_still_fails():
    """The change must not make the evaluator lenient about the thing it is meant to catch."""
    rows = [_Row(status="OK")] + [_Row(status="CRITICAL", error_type="CHECK_FAILED")] * 9

    result = compliance._build_result(
        _policy(), "srv/sqlserver/svc", rows, "2026-08-01T00:00:00Z", "2026-08-05T00:00:00Z")

    assert result.bad_count == 9
    assert result.status == "FAILED"
    assert result.data_quality_status == "OK"


def _policy():
    """An explicit policy, not whichever one happens to be first in production config.

    Reading data/sla_policies.json made these tests depend on values tuned for the real estate:
    when every policy gained a minimum_sample_count of 20, cases built from four rows started
    reporting INSUFFICIENT_DATA and the assertions broke for a reason that had nothing to do with
    the data-quality split they exist to pin.
    """
    from db_ops.sla.models import SlaPolicy

    return SlaPolicy(
        policy_id="TEST_RATIO",
        name="test",
        metric_codes=("INSTANCE_STATUS",),
        objective_percent=99.0,
        window_hours=24,
        minimum_sample_count=1,
    )
