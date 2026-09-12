"""An install with no targets has nothing to be compliant about.

`db-ops init` writes a schedule that includes `APP-SLA-VALIDATE`, and on a root that has only ever
run `init` there are five required policies, no inventory, and therefore no data. Until 2026-09-10
that produced `status: FAILED` and exit 1 — so a correct new install logged a failing app command
every cycle, on its first day, with nothing wrong.

This project already has the rule: **"not configured" is a state, not a failure**, and a default
schedule that errors every cycle teaches its reader to ignore the log.

The discriminator is deliberately **configured targets**, not "are all the results NO_DATA". On a
running estate every result going NO_DATA means collection has stopped — an outage this must keep
reporting as FAILED. Having no targets at all is a different fact, and only the inventory can tell
them apart.
"""

from __future__ import annotations

import json

from db_ops.sla import compliance


def _inventory(tmp_path, records):
    (tmp_path / "db_instances.json").write_text(
        json.dumps({"db_instances": records}), encoding="utf-8")
    return tmp_path


def test_an_empty_inventory_is_reported_as_no_targets(tmp_path):
    _inventory(tmp_path, [])
    assert compliance._no_targets_configured(tmp_path) is True


def test_an_inventory_whose_only_target_is_disabled_counts_as_none(tmp_path):
    _inventory(tmp_path, [{"server_id": "X", "enabled": False}])
    assert compliance._no_targets_configured(tmp_path) is True


def test_one_enabled_target_is_enough_to_be_configured(tmp_path):
    _inventory(tmp_path, [{"server_id": "X", "enabled": True}])
    assert compliance._no_targets_configured(tmp_path) is False


def test_a_target_that_omits_enabled_is_treated_as_enabled(tmp_path):
    # The field is optional in the scaffold, and defaulting it to "off" would silently mute a
    # configured estate.
    _inventory(tmp_path, [{"server_id": "X"}])
    assert compliance._no_targets_configured(tmp_path) is False


def test_an_unreadable_inventory_does_not_claim_there_are_no_targets(tmp_path):
    # Failing open here would mute a real outage on an estate whose inventory momentarily cannot
    # be read. The safe direction is to keep reporting FAILED.
    (tmp_path / "db_instances.json").write_text("{ not json", encoding="utf-8")
    assert compliance._no_targets_configured(tmp_path) is False


def test_a_missing_inventory_file_is_no_targets(tmp_path):
    assert compliance._no_targets_configured(tmp_path) is True


def test_no_data_exits_zero_while_a_real_failure_still_exits_one():
    """The exit code is what the daemon records as a failing app command."""
    from db_ops.sla.cli import exit_code_for

    assert exit_code_for("NO_DATA") == 0
    assert exit_code_for("PASSED") == 0
    # The signal this app exists to give must survive the change above.
    assert exit_code_for("FAILED") == 1
    assert exit_code_for("AT_RISK") == 1


def test_allow_fail_still_silences_everything():
    from db_ops.sla.cli import exit_code_for

    assert exit_code_for("FAILED", allow_fail=True) == 0


def test_an_unknown_status_is_treated_as_a_failure_rather_than_waved_through():
    from db_ops.sla.cli import exit_code_for

    assert exit_code_for("") == 1
    assert exit_code_for("SOMETHING_NEW") == 1
