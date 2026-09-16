"""An absent backup policy must read as "unjudged", never as "compliant".

Found on 2026-09-14 by comparing the same report on two nodes. Both held *identical* backup
evidence for `192.0.2.115`: a FULL backup ten hours old and a LOG backup from 30 March, 168 days
behind. The node with `data/backup_policy.json` reported nine servers in Transaction-log RPO
violation. The node without it reported `4/4 DB within policy`, a green Compliant badge, and no
Priority Attention card of any kind.

Nothing was broken in the grading. With no policy document every `(database, type)` rule resolves
to `{}`, `_is_required` answers False, every type is `NOT_REQUIRED`, every database's worst state
is `OK`, and `compliant == eligible` falls out of that honestly. The defect was that "no rule
required this" and "no rule exists" produced the same sentence - so the most dangerous possible
configuration state, a monitoring tool with its backup policy missing, rendered as the healthiest
possible answer.

The node was one of two 0.16.0 roots stood up by hand, and it was missing ten catalogued config
files. `backup_policy.json` was the one that turned an absence into a false statement, which is
why `init` now writes it and why `sync-config` names every catalogued file a node does not have.
"""

from __future__ import annotations

from db_ops.common import data_sources
from db_ops.lib import backup_policy
from db_ops.reports.inventory_report import _build_backup, build_triage

from conftest import shipped_config

SERVER = "ACME-192-0-2-115"

_SHIPPED_POLICY = data_sources.load_backup_policy(shipped_config("backup_policy.json"))

#: One database, a fresh FULL and a LOG backup 168 days old - the shape that was reported green.
_ROWS = [
    {"metric_code": "BACKUP_LAST_RESULT", "metric_item": "APPDB / FULL", "metric_value": "10",
     "status": "OK", "collected_at": "2026-09-14T01:00:00Z",
     "message": "database=APPDB, recovery_model=FULL, backup_type=D, "
                "backup_finish_date=2026-09-13 22:40:29"},
    {"metric_code": "BACKUP_LAST_RESULT", "metric_item": "APPDB / LOG", "metric_value": "4028",
     "status": "OK", "collected_at": "2026-09-14T01:00:00Z",
     "message": "database=APPDB, recovery_model=FULL, backup_type=L, "
                "backup_finish_date=2026-03-30 12:00:02"},
]


def _model(policy):
    """One fleet-page server model: the Backup block built for real, every other signal quiet.

    Only ``backup`` matters here, but ``build_triage`` reads a whole model, so the rest is present
    and empty rather than absent - a card firing because a key was missing would be the test
    proving itself.
    """
    result = backup_policy.evaluate_backup_policy(_ROWS, server_id=SERVER, policy=policy)
    return {"role": "ACME-192-0-2-115", "ip": "192.0.2.115", "status": "ok", "inv": "",
            "ple": None, "total": 1, "disks": [], "ha": {}, "security": {},
            "cfg": {"warns": 0}, "os_health": {}, "freshness_detail": {},
            "backup": _build_backup({
                "databases": [{"db_type": "sqlserver"}],
                "backup_evidence": {
                    "FULL": {"latest_finish": "2026-09-13 22:40:29", "latest_age_hours": 10.0},
                    "LOG": {"latest_finish": "2026-03-30 12:00:02", "latest_age_hours": 4028.7},
                },
                "backup_policy": result})}


def test_an_empty_policy_document_is_not_a_configured_policy():
    assert backup_policy.policy_is_configured(_SHIPPED_POLICY) is True
    assert backup_policy.policy_is_configured({}) is False
    assert backup_policy.policy_is_configured(None) is False
    # A document with the wrapper keys and nothing inside them is the same absence, and it is the
    # shape a half-written file actually has.
    assert backup_policy.policy_is_configured({"defaults": {"types": {}}, "overrides": []}) is False


def test_a_deliberately_permissive_policy_still_counts_as_configured():
    """The counterweight. A policy may legitimately require nothing of one server; that is an
    answer, and it must not be mistaken for the file being gone."""
    permissive = {"defaults": {"types": {"FULL": {"required": False},
                                         "DIFF": {"required": False},
                                         "LOG": {"required_for_recovery_models": []}}}}

    assert backup_policy.policy_is_configured(permissive) is True

    summary = backup_policy.evaluate_backup_policy(_ROWS, server_id=SERVER,
                                                   policy=permissive)["summary"]
    assert summary["configured"] is True
    assert summary["status"] == "OK"
    assert backup_policy.coverage_text(summary) == "No policy match"


def test_the_shipped_policy_still_calls_a_168_day_old_log_backup_critical():
    """The control: with a policy present this evidence is a violation, and the whole point of
    every other test here is that the *same* rows must not come out green without one."""
    summary = backup_policy.evaluate_backup_policy(_ROWS, server_id=SERVER,
                                                   policy=_SHIPPED_POLICY)["summary"]

    assert summary["configured"] is True
    assert summary["status"] == "CRITICAL"
    assert summary["byType"]["LOG"]["state"] == "VIOLATED"


def test_without_a_policy_the_same_rows_are_unknown_and_nothing_is_compliant():
    result = backup_policy.evaluate_backup_policy(_ROWS, server_id=SERVER, policy={})

    summary = result["summary"]
    assert summary["configured"] is False
    assert summary["status"] == "UNKNOWN"
    assert summary["compliant"] == 0
    # eligible still counts what evidence was found: "we can see this database and cannot grade
    # it" is the statement, not "there is nothing here".
    assert summary["eligible"] == 1
    assert result["databases"][0]["status"] == "UNKNOWN"
    assert summary["byType"]["LOG"]["state"] == "UNKNOWN"


def test_the_reason_names_the_file_the_operator_has_to_go_and_find():
    summary = backup_policy.evaluate_backup_policy(_ROWS, server_id=SERVER, policy={})["summary"]

    assert "data/backup_policy.json" in summary["reason"]
    assert "unjudged" in summary["reason"]
    assert "within policy" not in summary["reason"]


def test_the_backup_column_distinguishes_no_rule_matched_from_no_policy_at_all():
    unconfigured = backup_policy.evaluate_backup_policy(_ROWS, server_id=SERVER, policy={})

    assert backup_policy.coverage_text(unconfigured["summary"]) == "No policy configured"


def test_the_fleet_model_reports_a_blind_spot_rather_than_a_pass():
    graded = _model(_SHIPPED_POLICY)["backup"]
    ungraded = _model({})["backup"]

    assert graded["logStale"] is True
    assert graded["policyConfigured"] is True

    assert ungraded["policyConfigured"] is False
    assert ungraded["status"] == "UNKNOWN"
    # Not True: without a policy there is no RPO to be past. The card that must fire is the one
    # about the missing policy, and claiming a violation we cannot prove would be the same class
    # of error in the other direction.
    assert ungraded["logStale"] is False
    # This is the string the defect actually printed over a 168-day-old log backup.
    assert "DB within policy" not in ungraded["note"]


def test_priority_attention_names_the_missing_policy_instead_of_saying_nothing():
    """The silence was the finding. A page with no cards reads as a clean estate, and that is
    exactly what the node without a policy published."""
    cards = build_triage([_model({})])

    card = next((c for c in cards if "not being graded" in c["title"]), None)
    assert card is not None, [c["title"] for c in cards]
    assert card["sev"] == "warn"
    assert "data/backup_policy.json" in card["body"]
    assert "192.0.2.115" in card["tags"]
    # And no RPO card, because with no policy there is no RPO to violate.
    assert not any("RPO violated" in c["title"] for c in cards)


def test_a_graded_server_raises_no_missing_policy_card():
    cards = build_triage([_model(_SHIPPED_POLICY)])

    assert not any("not being graded" in c["title"] for c in cards)
    assert any("RPO violated" in c["title"] for c in cards)


def test_init_actually_writes_a_backup_policy_into_a_fresh_root(tmp_path):
    """The other half of the fix, and it has to be checked by RUNNING init.

    The first version of this test asserted `"data/backup_policy.json" in PACKAGED_DEFAULTS` and
    stopped there. It passed, the file shipped in the wheel, `packaged_default` found it - and
    `init` still did not write it, because `_files()` repeated the list of names by hand and only
    the map had been updated. A fresh 0.17.0 root came up with no backup policy: the exact hole
    this release exists to close, reproduced by the release itself and found by standing a node up
    rather than by the suite.

    So this reads the FILE ON DISK after an init, and the assertion below reads every shipped
    default the same way - one list, checked by using it.
    """
    from db_ops import scaffold

    scaffold.initialise(tmp_path, app_name="dbabrain")

    written = {name for name, _ in scaffold._files("dbabrain")}
    unwritten = sorted(set(scaffold.PACKAGED_DEFAULTS) - written)
    assert not unwritten, (
        f"{unwritten} ship in the package and init does not write them. `_files()` must derive "
        "its list from PACKAGED_DEFAULTS, never repeat it.")

    policy_file = tmp_path / "data" / "backup_policy.json"
    assert policy_file.is_file(), "init wrote no backup policy"

    shipped = data_sources.load_backup_policy(policy_file)
    assert backup_policy.policy_is_configured(shipped) is True
    summary = backup_policy.evaluate_backup_policy(_ROWS, server_id=SERVER,
                                                   policy=shipped)["summary"]
    assert summary["status"] == "CRITICAL", "a fresh root must grade this evidence, not pass it"
