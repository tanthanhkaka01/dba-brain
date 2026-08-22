"""The gate/evidence model: db_ops.common.evidence.

An operation that touches a production host is judged afterwards by what it recorded. These
tests pin the two distinctions that decide whether the record is worth anything: a *blocking*
failure stops the run while a warning does not, and an *overridable* failure can be accepted
deliberately without pretending it did not happen. The 2026-08-03 CU26 run is why they exist —
two gates that could not fail meaningfully made a completely successful patch exit non-zero,
which teaches every later reader to ignore the exit code.
"""

import json

from db_ops.common.evidence import FAIL, OK, WARN, GateReport


def test_a_run_with_only_warnings_still_passes():
    report = GateReport("restart", target="host-a")
    report.add("host.uptime", WARN, "up 281 days", blocking=False)
    report.add("host.reachable", OK, "answered")

    assert report.passed()
    assert report.status() == WARN
    assert report.to_dict()["ok"] is True


def test_a_blocking_failure_stops_the_run_and_names_itself():
    report = GateReport("apply-cu", target="host-a")
    report.add("host.reboot_pending", FAIL, "307 pending file renames")

    assert not report.passed()
    assert [gate.name for gate in report.blockers()] == ["host.reboot_pending"]
    assert report.to_dict()["blockers"] == ["host.reboot_pending"]


def test_a_non_blocking_failure_is_recorded_but_does_not_stop_the_run():
    report = GateReport("apply-cu")
    report.add("installer.sha256", FAIL, "hash mismatch", blocking=False)

    assert report.passed()
    assert report.status() == WARN


def test_an_override_accepts_only_the_gate_it_names():
    report = GateReport("apply-cu")
    report.add("sql.recent_full_backup", FAIL, "stale", override="allow-stale-backup")
    report.add("host.reboot_pending", FAIL, "pending", override="allow-pending-reboot")

    assert [gate.name for gate in report.blockers({"allow-stale-backup"})] == ["host.reboot_pending"]
    assert report.passed({"allow-stale-backup", "allow-pending-reboot"})


def test_an_accepted_failure_is_still_visible_in_the_evidence(tmp_path):
    """Accepting a blocker changes the verdict, never the record: an audit has to be able to
    see that the backup was stale AND that someone signed for it."""
    report = GateReport("apply-cu")
    report.add("sql.recent_full_backup", FAIL, "stale: APPDB_Prod", override="allow-stale-backup")

    payload = report.to_dict(["allow-stale-backup"])
    assert payload["ok"] is True
    assert payload["overrides"] == ["allow-stale-backup"]
    assert payload["gates"][0]["status"] == FAIL


def test_each_run_writes_its_own_evidence_file(tmp_path):
    """Evidence is append-only by construction: what the host looked like at 19:51 and at 19:56
    are different answers, and an incident review needs the earlier one."""
    first = GateReport("precheck", run_id="20260803_195155")
    first.add("host.reboot_pending", FAIL, "307 pending file renames")
    second = GateReport("precheck", run_id="20260803_195608")
    second.add("host.reboot_pending", OK, "no pending reboot recorded")

    first_path = first.write(tmp_path)
    second_path = second.write(tmp_path)

    assert first_path != second_path
    assert json.loads(first_path.read_text(encoding="utf-8"))["ok"] is False
    assert json.loads(second_path.read_text(encoding="utf-8"))["ok"] is True


def test_gates_are_echoed_as_they_run_not_only_at_the_end():
    """A 30-minute restart that speaks only at the end is indistinguishable from a hung one."""
    lines: list[str] = []
    report = GateReport("restart", echo=lines.append)
    report.add("restart.back_online", OK, "answering again after 92.0s")
    report.say("Waiting for the services to start...")

    assert lines == [
        "[ OK ] restart.back_online: answering again after 92.0s",
        "Waiting for the services to start...",
    ]
