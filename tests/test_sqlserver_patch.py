"""Applying and verifying a SQL Server cumulative update: db_ops.common.sqlserver_patch.

Every assertion here is a defect the 2026-08-03 CU26 run produced or narrowly avoided
(``docs/reports/20260803_ord129-sqlserver-2022-cu26-execution-report.md``):

* the registry gate must read ``PatchLevel``, not ``Version`` — ``Version`` is the build
  originally installed and never moves on a CU, so comparing it failed **every** successful
  patch on **every** instance;
* setup exit code ``3010`` is a success with one outstanding action (the restart), not a
  failure and never a reason to run setup again;
* the patch re-runs every precheck gate before touching the host, and stops if one blocks.
"""

import json
from types import SimpleNamespace

import pytest

from db_ops.common import host_ops, sqlserver_patch as patch
from db_ops.common.evidence import FAIL, OK, GateReport
from db_ops.common.remote_exec import RemoteResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.scripts: list[str] = []
        self.access = SimpleNamespace(platform="windows", username="admin", password="pw",
                                      is_powershell=True)

    def run_script(self, script, *, shell=None, timeout_seconds=None, env=None, check=False):
        self.scripts.append(str(script))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return RemoteResult(method="ssh", host="10.0.0.5", command="script", exit_code=0, stdout=reply)

    def run(self, command, **kwargs):
        return self.run_script(command)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class FakeCursor:
    """Answers by SQL fragment, so a test states the fact and not the query text."""

    def __init__(self, answers):
        self._answers = answers
        self.description = []
        self._rows = []

    def execute(self, sql):
        for fragment, (columns, rows) in self._answers.items():
            if fragment in sql:
                self.description = [(name,) for name in columns]
                self._rows = rows
                return
        raise AssertionError(f"unexpected SQL in this test: {sql[:120]}")

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, answers):
        self._cursor = FakeCursor(answers)
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


_SERVER_ROW = (
    ["server_name", "instance_name", "product_version", "product_level", "update_level",
     "update_reference", "edition", "is_clustered", "is_hadr"],
    [["APPDB-DB\\APPDB", "APPDB", "16.0.4265.3", "RTM", "CU26", "KB5093420",
      "Developer Edition (64-bit)", "0", "0"]],
)
_DATABASES = (["name", "state_desc"], [["master", "ONLINE"], ["APPDB_Prod", "ONLINE"]])


def _probe(patch_level="16.0.4265.3", version="16.0.1000.6"):
    return json.dumps(
        {
            "registry_version": version,
            "registry_patch_level": patch_level,
            "registry_edition": "Developer Edition",
            "setup_running": "",
        }
    )


def _facts(services=("MSSQL$APPDB", "SQLAgent$APPDB", "SQLBrowser")):
    return json.dumps(
        {
            "whoami": "APPDB-DB\\appdbadmin",
            "is_admin": True,
            "hostname": "APPDB-DB",
            "os": "Microsoft Windows Server 2019 Standard",
            "last_boot": "2026-08-03T20:06:56",
            "uptime_days": 0.01,
            "remote_time": "2026-08-03T20:07:06",
            "disks": [{"mount": "C:", "free_gb": 42.5, "total_gb": 120.0}],
            "services": [{"name": name, "status": "Running", "start_type": "Automatic"}
                         for name in services],
            "reboot_pending": {"required": False, "reasons": [], "pending_file_rename_count": 0},
        }
    )


@pytest.fixture()
def data_dir(tmp_path):
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
                        "cmd_access": {"method": "ssh", "auth_type": "password",
                                       "credential_name": "win_admin"},
                    },
                    {
                        "server_id": "TEST-LINUX",
                        "ip": "10.0.0.7",
                        "db_type": "postgresql",
                        "platform": "linux",
                        "cmd_access": {"method": "ssh", "auth_type": "password",
                                       "credential_name": "win_admin"},
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
                        "host": "10.0.0.5",
                        "credentials": [{"credential_name": "win_admin", "username": "admin",
                                         "password_ref": "PW"}],
                    },
                    {
                        "host": "10.0.0.7",
                        "credentials": [{"credential_name": "win_admin", "username": "tuser",
                                         "password_ref": "PW"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Naming rules
# ---------------------------------------------------------------------------
def test_instance_naming_is_derived_not_asked_for():
    assert patch.sqlserver_registry_key("16.0.4265.3", "APPDB") == "MSSQL16.APPDB"
    # A default instance is registered as MSSQLSERVER, not as an empty name.
    assert patch.sqlserver_registry_key("15.0.4335.1", "") == "MSSQL15.MSSQLSERVER"
    assert patch.sqlserver_service_names("APPDB") == ["MSSQL$APPDB", "SQLAgent$APPDB", "SQLBrowser"]
    assert patch.sqlserver_service_names("") == ["MSSQLSERVER", "SQLSERVERAGENT", "SQLBrowser"]
    assert patch.setup_log_root("16.0.4265.3").endswith(r"\160\Setup Bootstrap\Log")


def test_builds_compare_as_numbers_not_as_text():
    """'16.0.4265.3' > '16.0.1000.6' is false as strings and true as builds."""
    assert patch.version_tuple("16.0.4265.3") > patch.version_tuple("16.0.1000.6")
    assert patch.version_tuple("16.0.1000.6") < patch.version_tuple("16.0.4265.3")


def test_the_unattended_patch_names_the_instance_it_patches():
    """A host can hold several instances; patching the wrong one is silent."""
    arguments = patch.patch_arguments("APPDB")

    assert "/InstanceName=APPDB" in arguments
    assert "/Action=Patch" in arguments
    assert "/IAcceptSQLServerLicenseTerms" in arguments
    assert patch.patch_arguments("")[3] == "/InstanceName=MSSQLSERVER"


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
def test_exit_code_3010_is_a_success_with_an_outstanding_restart():
    status, verdict = patch.patch_exit_verdict(3010)

    assert status == OK
    assert "SUCCEEDED" in verdict
    assert "Do NOT re-run" in verdict


def test_exit_code_zero_passes_and_anything_else_fails():
    assert patch.patch_exit_verdict(0)[0] == OK
    status, verdict = patch.patch_exit_verdict(1, log_root=r"C:\...\Log")
    assert status == FAIL
    assert "Summary.txt" in verdict


# ---------------------------------------------------------------------------
# verify-build
# ---------------------------------------------------------------------------
def test_verify_build_compares_the_registry_patch_level_not_the_installed_version(data_dir, monkeypatch):
    """The defect this whole gate was rewritten for: an RTM install patched to CU26 keeps
    registry Version 16.0.1000.6 forever. Comparing that against the target build reported FAIL
    on every successful CU on every instance, and made a clean run exit non-zero."""
    monkeypatch.setattr(
        patch, "_connect",
        lambda request, *, data_dir=None, timeout_seconds=15: (
            FakeConnection({"SERVERPROPERTY": _SERVER_ROW, "sys.databases": _DATABASES}),
            {"instance_name": "APPDB"},
        ),
    )
    monkeypatch.setattr(host_ops, "open_host_session",
                        lambda *a, **k: FakeSession([_facts(), _probe()]))

    result = patch.verify_build(
        {"target": "TEST-10-0-0-5", "expected_build": "16.0.4265.3", "evidence": False},
        data_dir=data_dir,
    )

    assert result["ok"] is True
    registry_gate = next(g for g in result["gates"] if g["name"] == "post.registry_build")
    assert registry_gate["status"] == OK
    assert "16.0.1000.6 by design" in registry_gate["detail"]
    build_gate = next(g for g in result["gates"] if g["name"] == "post.build")
    assert build_gate["status"] == OK and "CU26" in build_gate["detail"]


def test_verify_build_fails_when_the_instance_is_on_another_build(data_dir, monkeypatch):
    monkeypatch.setattr(
        patch, "_connect",
        lambda request, *, data_dir=None, timeout_seconds=15: (
            FakeConnection({"SERVERPROPERTY": _SERVER_ROW, "sys.databases": _DATABASES}),
            {"instance_name": "APPDB"},
        ),
    )
    monkeypatch.setattr(host_ops, "open_host_session",
                        lambda *a, **k: FakeSession([_facts(), _probe(patch_level="16.0.1000.6")]))

    result = patch.verify_build(
        {"target": "TEST-10-0-0-5", "expected_build": "16.0.4900.1", "evidence": False},
        data_dir=data_dir,
    )

    assert result["ok"] is False
    assert "post.build" in result["blockers"]
    assert "post.registry_build" in result["blockers"]


# ---------------------------------------------------------------------------
# precheck / apply-cu
# ---------------------------------------------------------------------------
def test_a_cumulative_update_is_refused_on_a_non_windows_target(data_dir):
    result = patch.precheck({"target": "TEST-LINUX", "evidence": False}, data_dir=data_dir)

    assert result["ok"] is False
    assert "host.platform" in result["blockers"]


def test_apply_cu_reruns_every_gate_and_stops_before_touching_the_host(data_dir, monkeypatch):
    """A precheck from an hour ago proves nothing about a host that has since started a Windows
    Update, so the gates run again inside apply-cu — and a blocker stops it there."""
    opened: list[str] = []

    def blocking_precheck(request, *, data_dir=None, echo=None, report=None):
        report.add("host.reboot_pending", FAIL, "307 pending file renames",
                   override="allow-pending-reboot")
        report.note("instance", {"instance_name": "APPDB", "current_build": "16.0.1000.6"})
        return {}

    monkeypatch.setattr(patch, "precheck", blocking_precheck)
    monkeypatch.setattr(host_ops, "open_host_session",
                        lambda *a, **k: opened.append("session") or FakeSession(["{}"]))

    result = patch.apply_cu(
        {
            "target": "TEST-10-0-0-5",
            "installer": r"D:\Softwares\SQLServer2022-KB5093420-x64.exe",
            "expected_build": "16.0.4265.3",
            "confirm": True,
            "evidence": False,
        },
        data_dir=data_dir,
    )

    assert result["ok"] is False
    assert "patch.aborted" in [gate["name"] for gate in result["gates"]]
    assert opened == []


def test_apply_cu_will_not_run_unconfirmed(data_dir, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        patch, "precheck",
        lambda request, *, data_dir=None, echo=None, report=None: (
            report.add("sql.connect", OK, "16.0.1000.6"),
            report.note("instance", {"instance_name": "APPDB", "current_build": "16.0.1000.6"}),
            {},
        )[-1],
    )
    monkeypatch.setattr(host_ops, "open_host_session",
                        lambda *a, **k: opened.append("session") or FakeSession(["{}"]))

    result = patch.apply_cu(
        {
            "target": "TEST-10-0-0-5",
            "installer": r"D:\Softwares\SQLServer2022-KB5093420-x64.exe",
            "evidence": False,
        },
        data_dir=data_dir,
    )

    assert result["ok"] is False
    assert "confirm" in result["blockers"]
    assert opened == []


def test_the_patch_prompt_says_the_cu_cannot_be_uninstalled(data_dir, monkeypatch, capsys):
    """A CU cannot be removed from an RTM instance; rollback means restoring a snapshot. That is
    the fact an operator needs at the moment of deciding, not in a runbook they read last week.
    The run also proves exit 3010 is reported as a success with one outstanding action."""
    monkeypatch.setattr(
        patch, "precheck",
        lambda request, *, data_dir=None, echo=None, report=None: (
            report.note("instance", {"instance_name": "APPDB", "current_build": "16.0.1000.6"}), {}
        )[-1],
    )
    monkeypatch.setattr(
        host_ops, "open_host_session",
        lambda *a, **k: FakeSession([json.dumps(
            {"exit_code": 3010, "started_at": "2026-08-03T19:57:06",
             "finished_at": "2026-08-03T20:01:56", "duration_minutes": 4.8,
             "run_directory": r"C:\Windows\Temp\db_ops_cu_x"}
        )]),
    )
    monkeypatch.setattr(host_ops.confirm, "is_interactive", lambda: True)
    # Applying a CU stops and patches the instance, so it is level 100 in
    # `data/emergency_operations.json`: "yes", then the target's own id typed out.
    patch_answers = iter(["yes", "TEST-10-0-0-5"])
    monkeypatch.setattr(host_ops.confirm, "read_answer",
                        lambda prompt, stream=None: next(patch_answers))

    result = patch.apply_cu(
        {
            "target": "TEST-10-0-0-5",
            "installer": r"D:\Softwares\SQLServer2022-KB5093420-x64.exe",
            "expected_build": "16.0.4265.3",
            "kb": "KB5093420",
            "confirm": True,
            "evidence": False,
        },
        data_dir=data_dir,
    )
    shown = capsys.readouterr().err

    assert "CANNOT be uninstalled" in shown
    assert "16.0.1000.6 -> 16.0.4265.3 (KB5093420)" in shown
    exit_gate = next(gate for gate in result["gates"] if gate["name"] == "patch.exit_code")
    assert exit_gate["status"] == OK and "Do NOT re-run" in exit_gate["detail"]
    assert any(gate["name"] == "patch.restart_required" for gate in result["gates"])
    assert result["ok"] is True


def test_apply_cu_needs_an_installer_path(data_dir):
    with pytest.raises(patch.SqlServerPatchError) as excinfo:
        patch.apply_cu({"target": "TEST-10-0-0-5", "confirm": True}, data_dir=data_dir)

    assert "installer is required" in str(excinfo.value)


def test_a_dry_run_prints_the_exact_command_it_would_execute(data_dir, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        patch, "precheck",
        lambda request, *, data_dir=None, echo=None, report=None: (
            report.note("instance", {"instance_name": "APPDB", "current_build": "16.0.1000.6"}), {}
        )[-1],
    )
    monkeypatch.setattr(host_ops, "open_host_session",
                        lambda *a, **k: opened.append("session") or FakeSession(["{}"]))

    result = patch.apply_cu(
        {
            "target": "TEST-10-0-0-5",
            "installer": r"D:\Softwares\SQLServer2022-KB5093420-x64.exe",
            "expected_build": "16.0.4265.3",
            "dry_run": True,
            "confirm": True,
            "evidence": False,
        },
        data_dir=data_dir,
    )

    dry_run = next(gate for gate in result["gates"] if gate["name"] == "patch.dry_run")
    assert "/Action=Patch" in dry_run["detail"]
    assert "/InstanceName=APPDB" in dry_run["detail"]
    assert opened == []
