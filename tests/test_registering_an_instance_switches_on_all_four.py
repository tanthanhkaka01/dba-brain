"""One registration turns on four mechanisms, and nothing asserted it.

`instance-add` writes an inventory record with `enabled: true` and **no** `metrics` / `reports` /
`alerts` block at all. Four things then read that record — server metrics, the inventory report,
the index report and SLA — through a cascade where each flag defaults to the one before it:
`enabled` → `metrics` → `reports` → `alerts`. So a plain registration is collected *and* reported,
which is what makes "add an instance and it appears" true without asking the operator to learn a
second switch.

That property was true, measured on a node, and asserted nowhere. It has already drifted apart
once: a node ran for a day with **43** instances registered and **1** on the fleet page, because
the canonical inventory was seeded on the afternoon the node was stood up and nothing adopted what
came later. This file is the guard for that property.

It asserts the **property**, not the mechanism: what an operator gets from one registration, and
the one supported way to say the opposite.
"""

import json

from db_ops.common import instance_admin
from db_ops.common.data_sources.metric_targets import load_config_metric_targets
from db_ops.lib.inventory_render import adopt_new_servers, reportable_servers, seed_inventory
from db_ops.lib.target_flags import is_alerts_enabled, is_metrics_enabled, is_reports_enabled


def test_a_registration_writes_one_flag_and_not_four(tmp_path):
    """The record carries `enabled: true` and says nothing about metrics, reports or alerts. Four
    blocks written at registration time would be four places to keep in step, and the cascade
    exists so there is one."""
    instance_admin.add_instance({"server_id": "ACME-1", "db_type": "sqlserver", "ip": "192.0.2.10"},
                                data_dir=tmp_path)

    record = _record(tmp_path)
    assert record["enabled"] is True
    assert "metrics" not in record and "reports" not in record and "alerts" not in record


def test_all_four_read_true_from_that_one_flag(tmp_path):
    instance_admin.add_instance({"server_id": "ACME-1", "db_type": "sqlserver", "ip": "192.0.2.10"},
                                data_dir=tmp_path)

    record = _record(tmp_path)
    assert is_metrics_enabled(record) is True
    assert is_reports_enabled(record) is True
    assert is_alerts_enabled(record) is True


def test_a_new_registration_is_collected_from(tmp_path):
    """The metrics side of the same property, asked the way the collectors ask it."""
    instance_admin.add_instance({"server_id": "ACME-1", "db_type": "sqlserver", "ip": "192.0.2.10"},
                                data_dir=tmp_path)

    targets = load_config_metric_targets(data_dir=tmp_path, require_metrics_enabled=True)

    assert [target.server_id for target in targets] == ["ACME-1"]


def test_a_new_registration_reaches_an_inventory_that_predates_it():
    """The drift that cost a day: the canonical file is seeded once, the merge only updates what
    it already lists, and an instance registered afterwards had no way in."""
    canonical = {"servers": [{"server_id": "OLD-1", "ip": "192.0.2.1", "databases": []}]}

    adopted = adopt_new_servers(
        canonical, seed_inventory([{"server_id": "ACME-1", "ip": "192.0.2.10", "databases": []}]))

    assert adopted == ["ACME-1"]
    assert [server["server_id"] for server in canonical["servers"]] == ["OLD-1", "ACME-1"]


def test_an_enriched_entry_is_never_overwritten_by_adoption():
    """Add-only is the whole safety argument for adoption overriding a deletion."""
    canonical = {"servers": [{"server_id": "ACME-1", "ip": "192.0.2.10", "note": "the operator's"}]}

    adopted = adopt_new_servers(
        canonical, seed_inventory([{"server_id": "ACME-1", "ip": "10.9.9.9", "databases": []}]))

    assert adopted == []
    assert canonical["servers"][0]["note"] == "the operator's"


# ---------------------------------------------------------------------------
# The counterweight: the one supported way to say "registered, not reported"
# ---------------------------------------------------------------------------
def test_reports_off_keeps_a_target_collected_and_off_the_page():
    """Deleting the line from the canonical file is not that way — adoption puts it back. So the
    route has to live in the register, where everything else already looks."""
    record = {"server_id": "ACME-1", "db_type": "sqlserver", "ip": "192.0.2.10",
              "enabled": True, "reports": {"enabled": False}}

    assert is_metrics_enabled(record) is True
    assert is_reports_enabled(record) is False
    assert is_alerts_enabled(record) is False      # the cascade carries it on, it does not stop
    assert reportable_servers([{"server_id": "ACME-1", "databases": [record]}]) == []


def test_the_instance_switched_off_stops_everything():
    record = {"server_id": "ACME-1", "db_type": "sqlserver", "enabled": False}

    assert is_metrics_enabled(record) is False
    assert is_reports_enabled(record) is False
    assert is_alerts_enabled(record) is False


# ---------------------------------------------------------------------------
def _record(data_dir):
    payload = json.loads((data_dir / "db_instances.json").read_text(encoding="utf-8"))
    return payload["db_instances"][0]
