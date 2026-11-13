"""A change between two reads of an inventory metric is an event - and only a change is (1.324).

Who is in the host's administrator groups, which keys open root, whether a database's files are
encrypted: these metrics are inventories, and their rows are OK on purpose - a WARNING for every
member of ``sudo`` would fire on every host, on every read, for ever, and teach its reader to ignore
it. What somebody has to look at is the moment one of them changes: an account added to an admin
group, a new root key, password login turned on, a database's encryption turned off.

So a metric states which kinds of change it reports (``change_alert``), and the collector holds each
read to the one before it. These tests hold the rules that keep that honest: the first read is the
baseline; a read that failed reports nothing and is never a baseline; a section the collector could
not read is never "everything was removed"; a kind the metric does not state is not reported; and a
change row is an event, never part of the next comparison.
"""
from __future__ import annotations

import pytest
from conftest import shipped_config

from dbabrain.lib import change_alert
from dbabrain.metrics import collector
from dbabrain.metrics.models import MetricDefinition, MetricResult, MetricTarget
from dbabrain.metrics.storage import MetricStore

T1, T2, T3 = "2026-11-11T08:00:00Z", "2026-11-11T09:00:00Z", "2026-11-11T10:00:00Z"
HOST_KINDS = {"added": "ERROR", "changed": "ERROR", "removed": "WARNING"}


def _row(item, value="member", unit="account"):
    return {"metric_item": item, "metric_value": value, "metric_unit": unit}


def _kinds(found):
    return [(change["kind"], change["item"]) for change in found]


# --------------------------------------------------------------------------- the comparison itself
def test_an_account_added_to_an_admin_group_is_an_added_change():
    before = [_row("group:sudo:alice")]
    after = [_row("group:sudo:alice"), _row("group:sudo:mallory")]
    assert _kinds(change_alert.changes(before, after)) == [("added", "group:sudo:mallory")]


def test_an_account_taken_out_of_a_group_is_a_removed_change():
    before = [_row("group:sudo:alice"), _row("group:sudo:bob")]
    after = [_row("group:sudo:alice")]
    assert _kinds(change_alert.changes(before, after)) == [("removed", "group:sudo:bob")]


def test_a_setting_with_another_value_is_a_changed_change_naming_both_values():
    before = [_row("sshd:PasswordAuthentication", "no", "setting")]
    after = [_row("sshd:PasswordAuthentication", "yes", "setting")]
    found = change_alert.changes(before, after)
    assert found == [{"kind": "changed", "item": "sshd:PasswordAuthentication", "before": "no", "after": "yes"}]
    line = change_alert.change_message(found[0], previous_at=T1, current_at=T2)
    assert "sshd:PasswordAuthentication: no -> yes" in line and T1 in line and T2 in line


def test_two_reads_that_say_the_same_report_nothing():
    rows = [_row("group:sudo:alice"), _row("uid0:root", "uid 0"), _row("root_key:SHA256:abc", "ED25519", "key")]
    assert change_alert.changes(rows, list(reversed(rows))) == []


def test_a_section_that_could_not_be_read_is_never_everything_was_removed():
    # Yesterday root's keys were read; today sudo no longer answers without a password.
    before = [_row("root_key:SHA256:abc", "ED25519", "key"), _row("group:sudo:alice")]
    after = [_row("root_key", "unreadable", "unreadable"), _row("group:sudo:alice")]
    assert change_alert.changes(before, after) == []
    # ... and the day access comes back, the keys are not all "new".
    assert change_alert.changes(after, before) == []


def test_an_unreadable_section_hides_only_its_own_items():
    before = [_row("group:Administrators", "unreadable", "unreadable"), _row("rdp:enabled", "no", "setting")]
    after = [_row("group:Administrators:CORP\\mallory", "User"), _row("rdp:enabled", "yes", "setting")]
    assert _kinds(change_alert.changes(before, after)) == [("changed", "rdp:enabled")]


def test_what_the_read_was_made_with_is_not_inventory():
    before = [_row("read_as", "dbadmin", "summary")]
    after = [_row("read_as", "root", "summary")]
    assert change_alert.changes(before, after) == []


def test_a_change_block_with_an_unknown_kind_or_status_is_refused():
    assert change_alert.parse_change_alert({"added": "error", "removed": None}, context="M") == {"added": "ERROR"}
    assert change_alert.parse_change_alert(None, context="M") == {}
    with pytest.raises(change_alert.ChangeAlertConfigError, match="unknown kind"):
        change_alert.parse_change_alert({"aded": "ERROR"}, context="M")
    with pytest.raises(change_alert.ChangeAlertConfigError, match="must be one of"):
        change_alert.parse_change_alert({"added": "LOUD"}, context="M")


# --------------------------------------------------------------------------- the collector's hook
def _metric(kinds=None):
    return MetricDefinition(metric_code="SECURITY_HOST_ADMINS", db_type="multi", category="security",
                            default_importance=1, active=True, collector_type="cmd",
                            change_alert=dict(HOST_KINDS if kinds is None else kinds))


def _target():
    return MetricTarget(target_id="ACME-192-0-2-10/host", server_id="ACME-192-0-2-10", ip="192.0.2.10",
                        db_type="host", db_name="", credential_name="")


def _result(item, value="member", unit="account", *, at=T2, status="OK", message=None):
    return MetricResult(target_id="ACME-192-0-2-10/host", server_id="ACME-192-0-2-10", ip="192.0.2.10",
                        db_type="host", db_name="", metric_code="SECURITY_HOST_ADMINS", metric_item=item,
                        metric_value=value, metric_unit=unit, status=status, importance=1, message=message,
                        collected_at=at)


def _read(store, results):
    """One collection as the collector stores it: the rows, then whatever changed since the last."""
    changed = collector._change_results(metric=_metric(), target=_target(), importance=1,
                                        results=results, store=store)
    store.insert_results(run_id=1, results=results + changed)
    return changed


def test_the_first_read_is_the_baseline_and_reports_nothing(tmp_path):
    store = MetricStore(tmp_path / "store.sqlite")
    assert _read(store, [_result("group:sudo:alice", at=T1)]) == []


def test_an_added_account_is_one_error_row_naming_it_and_both_reads(tmp_path):
    store = MetricStore(tmp_path / "store.sqlite")
    _read(store, [_result("group:sudo:alice", at=T1)])
    changed = _read(store, [_result("group:sudo:alice"), _result("group:docker:mallory")])
    assert [(row.metric_item, row.status, row.metric_unit) for row in changed] == [
        ("change:added:group:docker:mallory", "ERROR", "change")]
    assert T1 in changed[0].message and T2 in changed[0].message
    # A finding about the target, not a broken collector: the coverage panel reads error_type.
    assert changed[0].error_type == "CHECK_FAILED"


def test_a_removed_account_is_a_warning_and_an_unchanged_read_is_silent(tmp_path):
    store = MetricStore(tmp_path / "store.sqlite")
    _read(store, [_result("group:sudo:alice", at=T1), _result("group:sudo:bob", at=T1)])
    changed = _read(store, [_result("group:sudo:alice")])
    assert [(row.metric_item, row.status) for row in changed] == [("change:removed:group:sudo:bob", "WARNING")]
    assert _read(store, [_result("group:sudo:alice", at=T3)]) == []


def test_a_change_is_reported_once_because_a_change_row_is_never_the_baseline(tmp_path):
    store = MetricStore(tmp_path / "store.sqlite")
    _read(store, [_result("group:sudo:alice", at=T1)])
    assert len(_read(store, [_result("group:sudo:alice"), _result("group:sudo:mallory")])) == 1
    # The next read still holds mallory: nothing is new, and the change row of T2 is not an item.
    assert _read(store, [_result("group:sudo:alice", at=T3), _result("group:sudo:mallory", at=T3)]) == []


def test_a_read_that_failed_reports_nothing_and_is_not_the_next_baseline(tmp_path):
    store = MetricStore(tmp_path / "store.sqlite")
    _read(store, [_result("group:sudo:alice", at=T1)])
    failed = [_result(None, None, None, status="WARNING", message="ssh: connect timed out")]
    assert _read(store, failed) == []
    # The host answers again with one more administrator: held to T1, the last good read.
    changed = _read(store, [_result("group:sudo:alice", at=T3), _result("group:sudo:mallory", at=T3)])
    assert [row.metric_item for row in changed] == ["change:added:group:sudo:mallory"]
    assert T1 in changed[0].message


def test_a_kind_the_metric_does_not_state_is_not_reported(tmp_path):
    # The encryption inventory states `changed` only: a database created or dropped is not an event.
    store = MetricStore(tmp_path / "store.sqlite")
    metric = _metric({"changed": "ERROR"})
    first = [_result("SalesDb", "encrypted", "tde", at=T1)]
    store.insert_results(run_id=1, results=first)
    second = [_result("SalesDb", "not encrypted", "tde"), _result("Scratch", "not encrypted", "tde")]
    changed = collector._change_results(metric=metric, target=_target(), importance=1, results=second, store=store)
    assert [(row.metric_item, row.status) for row in changed] == [("change:changed:SalesDb", "ERROR")]
    assert "encrypted -> not encrypted" in changed[0].message


def test_a_targets_severity_map_reaches_a_change_row_like_any_other_row(tmp_path):
    import dataclasses

    store = MetricStore(tmp_path / "store.sqlite")
    store.insert_results(run_id=1, results=[_result("group:sudo:alice", at=T1)])
    lab = dataclasses.replace(_target(), metrics_config={"metric_overrides": {
        "SECURITY_HOST_ADMINS": {"severity_map": {"ERROR": "LOGGING"}}}})
    changed = collector._change_results(
        metric=_metric(), target=lab, importance=1,
        results=[_result("group:sudo:alice"), _result("group:sudo:mallory")], store=store)
    assert [row.status for row in changed] == ["LOGGING"]


def test_the_two_shipped_inventories_state_what_they_report():
    from dbabrain.metrics.definitions import load_metric_definitions

    shipped = {metric.metric_code: metric
               for metric in load_metric_definitions(shipped_config("metric_definitions.json"))}
    assert shipped["SECURITY_HOST_ADMINS"].change_alert == HOST_KINDS
    assert shipped["SECURITY_DATA_ENCRYPTION"].change_alert == {"changed": "ERROR"}
    # No other metric compares reads: a graded metric alerts on its own rows.
    assert sorted(code for code, metric in shipped.items() if metric.change_alert) == [
        "SECURITY_DATA_ENCRYPTION", "SECURITY_HOST_ADMINS"]
