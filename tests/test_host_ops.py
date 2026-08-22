"""Operating on a host: db_ops.common.host_ops.

The restart-and-wait, service-wait and host-facts logic used to live inside one single-purpose
patch script, where a task that only needed a restart could not reach it. These tests pin the
behaviours that made that script's own report worth writing:

* waiting for the transport to answer is **not** waiting for the host to be ready — the
  services wait polls instead of sampling once, which is what reported healthy SQL Server
  services as down on both restarts of the 2026-08-03 CU26 run;
* a host operation that changes the machine refuses to run unconfirmed;
* Windows and Linux produce the **same** fact shape, so a caller asks one set of questions.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from db_ops.common import host_ops
from db_ops.common.evidence import FAIL, OK, WARN
from db_ops.common.remote_exec import RemoteResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeSession:
    """A remote session whose replies are scripted, so nothing here touches a network."""

    def __init__(self, replies, *, platform="windows", username="admin"):
        self.replies = list(replies)
        self.commands: list[str] = []
        self.access = SimpleNamespace(
            platform=platform, username=username, password="pw", is_powershell=platform == "windows"
        )

    def _next(self, command: str) -> RemoteResult:
        self.commands.append(command)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, tuple):
            stdout, exit_code = reply
        else:
            stdout, exit_code = reply, 0
        return RemoteResult(method="ssh", host="10.0.0.5", command=command,
                            exit_code=exit_code, stdout=stdout)

    def run_script(self, script, *, shell=None, timeout_seconds=None, env=None, check=False):
        return self._next(str(script))

    def run(self, command, **kwargs):
        return self._next(str(command))

    def run_sudo(self, command, **kwargs):
        return self._next(f"sudo {command}")

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _windows_facts(services=(), pending=0):
    return json.dumps(
        {
            "whoami": "APPDB-DB\\appdbadmin",
            "is_admin": True,
            "hostname": "APPDB-DB",
            "os": "Microsoft Windows Server 2019 Standard 10.0.17763",
            "last_boot": "2026-08-03T20:06:56",
            "uptime_days": 0.01,
            "remote_time": "2026-08-03T20:07:06",
            "disks": [{"mount": "C:", "free_gb": 42.5, "total_gb": 120.0}],
            "services": list(services),
            "reboot_pending": {
                "required": bool(pending),
                "reasons": [f"PendingFileRenameOperations ({pending} entries)"] if pending else [],
                "pending_file_rename_count": pending,
            },
        }
    )


@pytest.fixture()
def data_dir(tmp_path):
    """A minimal data/ folder: one Windows instance reachable over SSH with a named credential."""
    (tmp_path / "db_instances.json").write_text(
        json.dumps(
            {
                "db_instances": [
                    {
                        "server_id": "TEST-10-0-0-5",
                        "ip": "10.0.0.5",
                        "port": 1433,
                        "db_type": "sqlserver",
                        "platform": "windows",
                        "instance_name": "APPDB",
                        "cmd_access": {
                            "method": "ssh",
                            "auth_type": "password",
                            "credential_name": "win_admin",
                        },
                    },
                    {
                        "server_id": "TEST-NO-ACCESS",
                        "ip": "10.0.0.9",
                        "db_type": "postgresql",
                        "platform": "linux",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "users.json").write_text(
        json.dumps(
            {
                "database_credentials": [],
                "remote_credentials": [
                    {
                        "server_id": "TEST-10-0-0-5",
                        "host": "10.0.0.5",
                        "credentials": [
                            {"credential_name": "win_admin", "username": "admin", "password_ref": "PW_REF"}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
def test_windows_and_linux_report_the_same_fact_shape(data_dir):
    windows = host_ops.read_facts(
        FakeSession([_windows_facts(services=[{"name": "MSSQL$APPDB", "status": "Running",
                                               "start_type": "Automatic"}])]),
        platform="windows",
        services=["MSSQL$APPDB"],
    )
    linux = host_ops.read_facts(
        FakeSession(
            [
                "hostname\tpg-01\n"
                "os\tUbuntu 22.04.4 LTS\n"
                "uptime_seconds\t172800\n"
                "is_admin\tfalse\n"
                "disk\t/\t52428800\t104857600\n"
                "service\tpostgresql\tactive\tenabled\n"
            ],
            platform="linux",
        ),
        platform="linux",
        services=["postgresql"],
    )

    assert set(windows) >= {"hostname", "os", "uptime_days", "disks", "services", "reboot_pending"}
    assert set(linux) >= {"hostname", "os", "uptime_days", "disks", "services", "reboot_pending"}
    assert linux["uptime_days"] == 2.0
    # df reports kibibytes; a caller comparing against a GB threshold must not have to know that.
    assert linux["disks"] == [{"mount": "/", "free_gb": 50.0, "total_gb": 100.0}]
    assert host_ops.is_service_up(windows["services"][0]["status"])
    assert host_ops.is_service_up(linux["services"][0]["status"])


def test_a_single_windows_service_still_arrives_as_a_list():
    """PowerShell's ConvertTo-Json collapses a one-element array to a bare object, so a host with
    exactly one matching service used to come back shaped differently from every other host."""
    raw = json.loads(_windows_facts())
    raw["services"] = {"name": "MSSQL$APPDB", "status": "Running", "start_type": "Automatic"}
    facts = host_ops.read_facts(FakeSession([json.dumps(raw)]), platform="windows")

    assert facts["services"] == [{"name": "MSSQL$APPDB", "status": "Running", "start_type": "Automatic"}]


def test_a_powershell_error_is_reported_as_what_came_back_not_as_a_json_error():
    session = FakeSession([("", 1)])
    session.replies = [("", 1)]
    with pytest.raises(host_ops.HostOpsError) as excinfo:
        host_ops.read_facts(session, platform="windows")

    assert "returned no facts" in str(excinfo.value)


def test_a_service_that_does_not_exist_is_reported_as_not_found():
    """"Not installed" and "stopped" have different fixes, so they must not read the same."""
    states = host_ops.service_states(
        FakeSession([_windows_facts()]), ["MSSQL$GONE"], platform="windows"
    )

    assert states == {"MSSQL$GONE": "not-found"}


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------
def test_waiting_for_services_polls_instead_of_sampling_once():
    """sshd answers long before SQL Server does. Sampling once when the port opens is what
    reported `not running: MSSQL$APPDB` on both restarts of a completely successful CU run."""
    session = FakeSession(
        [
            _windows_facts(services=[{"name": "MSSQL$APPDB", "status": "Stopped", "start_type": "Automatic"}]),
            _windows_facts(services=[{"name": "MSSQL$APPDB", "status": "StartPending", "start_type": "Automatic"}]),
            _windows_facts(services=[{"name": "MSSQL$APPDB", "status": "Running", "start_type": "Automatic"}]),
        ]
    )
    slept: list[float] = []

    states, not_up, waited = host_ops.wait_for_services(
        session, ["MSSQL$APPDB"], platform="windows", timeout_seconds=600, poll_seconds=15,
        sleep=slept.append,
    )

    assert not_up == []
    assert states == {"MSSQL$APPDB": "Running"}
    assert slept == [15, 15]
    assert waited >= 0


def test_a_services_timeout_says_how_long_it_actually_waited():
    """The old code slept 30 seconds and sampled once, then reported failure as if it were
    instant. Naming the budget is the difference between "it is broken" and "it is slow"."""
    session = FakeSession(
        [_windows_facts(services=[{"name": "MSSQL$APPDB", "status": "Stopped", "start_type": "Automatic"}])]
    )
    # A one-second budget keeps the test instant; the behaviour under test is that the budget is
    # honoured and reported, not how long it is.
    states, not_up, waited = host_ops.wait_for_services(
        session, ["MSSQL$APPDB"], platform="windows", timeout_seconds=1, poll_seconds=1,
        sleep=lambda _seconds: None,
    )

    assert not_up == ["MSSQL$APPDB"]
    assert states["MSSQL$APPDB"] == "Stopped"
    assert waited >= 0


def test_waiting_for_a_port_distinguishes_down_from_up(monkeypatch):
    """Without the down-wait, a host that has not begun shutting down is indistinguishable from
    one that already came back, and the restart is declared finished in two seconds."""
    answers = iter([True, True, False, False, True])

    class _Socket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    def create_connection(address, timeout=None):
        if next(answers):
            return _Socket()
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", create_connection)
    slept: list[float] = []

    went_down, _ = host_ops.wait_for_port(
        "10.0.0.5", 22, up=False, timeout_seconds=60, poll_seconds=5, sleep=slept.append
    )
    came_back, _ = host_ops.wait_for_port(
        "10.0.0.5", 22, up=True, timeout_seconds=60, poll_seconds=5, sleep=slept.append
    )

    assert went_down is True
    assert came_back is True


# ---------------------------------------------------------------------------
# Target resolution and policy
# ---------------------------------------------------------------------------
def test_a_host_resolves_by_server_id_or_by_ip(data_dir):
    """An operator holding a runbook has the ip; a scheduled task has the server_id."""
    by_id = host_ops.resolve_host("TEST-10-0-0-5", data_dir=data_dir)
    by_ip = host_ops.resolve_host("10.0.0.5", data_dir=data_dir)

    assert by_id.host == by_ip.host == "10.0.0.5"
    assert by_id.platform == "windows"
    assert by_id.access["method"] == "ssh"
    assert (by_id.credential or {})["username"] == "admin"


def test_a_host_without_cmd_access_says_so_instead_of_failing_later(data_dir):
    with pytest.raises(host_ops.HostOpsError) as excinfo:
        host_ops.resolve_host("TEST-NO-ACCESS", data_dir=data_dir)

    assert "no usable cmd_access" in str(excinfo.value)


def test_the_access_object_is_redacted_before_it_reaches_evidence(data_dir):
    target = host_ops.resolve_host(
        "10.0.0.5",
        data_dir=data_dir,
        access={"method": "ssh", "host": "10.0.0.5", "username": "admin", "password": "hunter2"},
    )

    assert target.to_dict()["access"]["password"] == "***"
    assert "hunter2" not in json.dumps(target.to_dict())


def test_policy_layers_request_over_file_over_builtin(data_dir):
    (data_dir / "maintenance_policy.json").write_text(
        json.dumps(
            {
                "maintenance_policy": {
                    "defaults": {"services_timeout_seconds": 900},
                    "servers": {"TEST-10-0-0-5": {"poll_seconds": 5}},
                }
            }
        ),
        encoding="utf-8",
    )

    policy = host_ops.load_maintenance_policy(
        data_dir, server_id="TEST-10-0-0-5", overrides={"poll_seconds": 30}
    )

    assert policy["services_timeout_seconds"] == 900     # file default
    assert policy["poll_seconds"] == 30                  # the request wins over the per-server value
    assert policy["up_timeout_seconds"] == 1800          # built-in, untouched


# ---------------------------------------------------------------------------
# The maintenance window
# ---------------------------------------------------------------------------
def test_the_maintenance_window_gate_reads_the_change_request_form():
    from datetime import datetime

    from db_ops.common.evidence import GateReport

    inside = GateReport("restart")
    host_ops.check_maintenance_window(
        inside, {"start": "2026-08-03 19:00", "end": "2026-08-03 21:00"},
        now=datetime(2026, 8, 3, 19, 51),
    )
    outside = GateReport("restart")
    host_ops.check_maintenance_window(
        outside, {"start": "2026-08-03 19:00", "end": "2026-08-03 21:00"},
        now=datetime(2026, 8, 3, 22, 10),
    )

    assert inside.gates[0].status == OK
    assert outside.gates[0].status == FAIL
    assert not outside.passed()


def test_ignoring_the_window_records_the_breach_instead_of_hiding_it():
    from datetime import datetime

    from db_ops.common.evidence import GateReport

    report = GateReport("restart")
    host_ops.check_maintenance_window(
        report, {"start": "2026-08-03 19:00", "end": "2026-08-03 21:00"}, ignore=True,
        now=datetime(2026, 8, 3, 22, 10),
    )

    assert report.gates[0].status == FAIL      # the fact is recorded
    assert report.passed()                     # ... and the operator chose to proceed


def test_a_recurring_window_is_evaluated_by_the_shared_time_window_rules():
    """`time_window` stays the only authority on from_*/to_* comparisons, including wrapping."""
    from datetime import datetime

    from db_ops.common.evidence import GateReport

    report = GateReport("restart")
    host_ops.check_maintenance_window(
        report, {"from_hour": 22, "to_hour": 6}, now=datetime(2026, 8, 3, 23, 30)
    )

    assert report.gates[0].status == OK


# ---------------------------------------------------------------------------
# Restart and service control
# ---------------------------------------------------------------------------
def test_a_restart_is_refused_without_confirmation(data_dir, monkeypatch):
    session = FakeSession([_windows_facts()])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)

    result = host_ops.restart_host(
        {"target": "TEST-10-0-0-5", "evidence": False}, data_dir=data_dir
    )

    assert result["ok"] is False
    assert "confirm" in result["blockers"]
    assert not any("shutdown" in command for command in session.commands)


def test_a_restart_asks_a_human_before_it_reboots_anything(data_dir, monkeypatch):
    """`confirm: true` says the payload meant it; the typed answers say a person is looking at
    THIS host right now. A reboot needs both — intent without presence is how the right command
    reaches the wrong machine.

    A restart is level 100 (`data/emergency_operations.json`), so it costs two answers and the
    second one is the target's own id. Two `yes` answers in a row are one answer typed twice.
    """
    session = FakeSession([_windows_facts(services=[{"name": "MSSQL$APPDB", "status": "Running",
                                                     "start_type": "Automatic"}])])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)
    monkeypatch.setattr(host_ops, "wait_for_port", lambda *a, **k: (True, 1.0))
    monkeypatch.setattr(host_ops.confirm, "is_interactive", lambda: True)
    asked: list[str] = []
    answers = iter(["yes", "TEST-10-0-0-5"])
    monkeypatch.setattr(host_ops.confirm, "read_answer",
                        lambda prompt, stream=None: asked.append(prompt) or next(answers))

    result = host_ops.restart_host(
        {"target": "TEST-10-0-0-5", "services": ["MSSQL$APPDB"], "confirm": True,
         "reason": "clear pending file renames", "evidence": False},
        data_dir=data_dir,
    )

    assert len(asked) == 2, "a restart must ask twice, the second time for the target id"
    assert result["ok"] is True
    assert any("shutdown.exe /r" in command for command in session.commands)
    assert result["facts"]["authorization"]["by"] == "prompt"
    assert result["facts"]["authorization"]["confirmations"] == 2


def test_a_restart_is_refused_when_the_second_answer_is_another_yes(data_dir, monkeypatch):
    """The muscle-memory case the second answer exists for, and the replay case it also blocks."""
    session = FakeSession([_windows_facts(services=[])])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)
    monkeypatch.setattr(host_ops, "wait_for_port", lambda *a, **k: (True, 1.0))
    monkeypatch.setattr(host_ops.confirm, "is_interactive", lambda: True)
    monkeypatch.setattr(host_ops.confirm, "read_answer", lambda prompt, stream=None: "yes")

    result = host_ops.restart_host(
        {"target": "TEST-10-0-0-5", "confirm": True, "evidence": False}, data_dir=data_dir,
    )

    assert result["ok"] is False
    assert not any("shutdown.exe /r" in command for command in session.commands)


def test_answering_anything_but_yes_leaves_the_host_alone(data_dir, monkeypatch):
    session = FakeSession([_windows_facts()])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)
    monkeypatch.setattr(host_ops.confirm, "is_interactive", lambda: True)
    monkeypatch.setattr(host_ops.confirm, "read_answer", lambda prompt, stream=None: "y")

    result = host_ops.restart_host(
        {"target": "TEST-10-0-0-5", "confirm": True, "evidence": False}, data_dir=data_dir
    )

    assert result["ok"] is False
    assert "confirm" in result["blockers"]
    assert not any("shutdown" in command for command in session.commands)


def test_an_unattended_restart_must_say_that_nobody_is_watching(data_dir, monkeypatch):
    """With no terminal, `confirm: true` alone is refused: a scheduled job that forgot to declare
    itself must fail rather than reboot production at 03:00."""
    session = FakeSession([_windows_facts()])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)
    monkeypatch.setattr(host_ops, "wait_for_port", lambda *a, **k: (True, 1.0))
    monkeypatch.setattr(host_ops.confirm, "is_interactive", lambda: False)

    refused = host_ops.restart_host(
        {"target": "TEST-10-0-0-5", "confirm": True, "evidence": False}, data_dir=data_dir
    )
    allowed = host_ops.restart_host(
        {"target": "TEST-10-0-0-5", "confirm": True, "assume_yes": True, "evidence": False},
        data_dir=data_dir,
    )

    assert refused["ok"] is False and "confirm" in refused["blockers"]
    assert allowed["ok"] is True
    assert allowed["facts"]["authorization"]["by"] == "assume_yes"


def test_a_dry_run_is_never_asked_to_confirm(data_dir, monkeypatch):
    """Rehearsing is not performing. Prompting for something that will not happen is how people
    learn to type `yes` without reading."""
    session = FakeSession([_windows_facts()])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)
    monkeypatch.setattr(host_ops.confirm, "is_interactive", lambda: True)
    asked: list[str] = []
    monkeypatch.setattr(host_ops.confirm, "read_answer",
                        lambda prompt, stream=None: asked.append(prompt) or "yes")

    result = host_ops.restart_host(
        {"target": "TEST-10-0-0-5", "confirm": True, "dry_run": True, "evidence": False},
        data_dir=data_dir,
    )

    assert result["ok"] is True
    assert asked == []


def test_a_dry_run_proves_the_target_without_restarting_it(data_dir, monkeypatch):
    session = FakeSession([_windows_facts(pending=307)])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)

    result = host_ops.restart_host(
        {"target": "TEST-10-0-0-5", "confirm": True, "dry_run": True, "evidence": False},
        data_dir=data_dir,
    )

    assert result["ok"] is True
    assert any(gate["name"] == "restart.dry_run" for gate in result["gates"])
    assert not any("shutdown" in command for command in session.commands)
    # The pending-reboot state is still reported: it is the reason the restart was planned.
    target_gate = next(gate for gate in result["gates"] if gate["name"] == "restart.target")
    assert "307 entries" in target_gate["detail"]


def test_stopping_a_service_needs_the_same_confirmation_as_a_restart(data_dir, monkeypatch):
    """Stopping a database service is exactly as disruptive as restarting the machine."""
    session = FakeSession([_windows_facts(services=[{"name": "MSSQL$APPDB", "status": "Running",
                                                     "start_type": "Automatic"}])])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)

    result = host_ops.service_control(
        {"target": "TEST-10-0-0-5", "services": ["MSSQL$APPDB"], "action": "stop", "evidence": False},
        data_dir=data_dir,
    )

    assert result["ok"] is False
    assert "confirm" in result["blockers"]
    assert not any("Stop-Service" in command for command in session.commands)


def test_reading_service_status_needs_no_confirmation(data_dir, monkeypatch):
    session = FakeSession([_windows_facts(services=[{"name": "MSSQL$APPDB", "status": "Running",
                                                     "start_type": "Automatic"}])])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)

    result = host_ops.service_control(
        {"target": "TEST-10-0-0-5", "services": ["MSSQL$APPDB"], "evidence": False},
        data_dir=data_dir,
    )

    assert result["ok"] is True
    assert result["facts"]["states_before"] == {"MSSQL$APPDB": "Running"}


def test_linux_service_control_goes_through_sudo_with_the_password_on_stdin(data_dir):
    """`ps` shows every argument on a Linux box, so a sudo password never travels on the argv."""
    command = host_ops._service_command("restart", "postgresql", platform="linux")
    assert command == "systemctl restart postgresql"

    session = FakeSession(["ok"], platform="linux", username="tuser")
    host_ops._run_privileged(session, command, platform="linux")
    assert session.commands == ["sudo systemctl restart postgresql"]


def test_host_facts_reports_but_never_blocks(data_dir, monkeypatch):
    """A read-only command that can fail a run teaches people to stop running it."""
    session = FakeSession([_windows_facts(pending=307)])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)

    result = host_ops.host_facts({"target": "TEST-10-0-0-5", "evidence": False}, data_dir=data_dir)

    assert result["ok"] is True
    pending_gate = next(gate for gate in result["gates"] if gate["name"] == "host.reboot_pending")
    assert pending_gate["status"] == WARN
    assert pending_gate["blocking"] is False


def test_evidence_is_written_next_to_the_run_when_asked(data_dir, monkeypatch, tmp_path):
    session = FakeSession([_windows_facts()])
    monkeypatch.setattr(host_ops, "open_host_session", lambda *a, **k: session)

    result = host_ops.host_facts(
        {"target": "TEST-10-0-0-5", "evidence": str(tmp_path / "evidence")}, data_dir=data_dir
    )

    written = json.loads((tmp_path / "evidence" / "facts" / f"{result['run_id']}.json").read_text(encoding="utf-8"))
    assert written["target"].startswith("TEST-10-0-0-5")
    assert written["gates"]
