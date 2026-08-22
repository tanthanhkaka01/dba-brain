"""Where a command lands, and which PowerShell it is written for.

Two facts about a host that `platform` alone cannot carry, and both were discovered the expensive
way. A command has to know whether it runs *on* a machine or *inside* a container on it — `hostcmd`
knew and `host_ops` did not, so `backup-database` could reach a containerised engine and `run-cmd`
could only reach the machine hosting it. And a PowerShell script has to know which cmdlets exist:
`Get-CimInstance` and `ConvertTo-Json` arrived with PowerShell 3.0 / NT 6.2, and below that the
fact script does not return less, it fails at cmdlet lookup with a message that reads like a
permissions problem.
"""

import pytest

from db_ops.common import host_ops
from db_ops.common.host_ops import HostTarget, os_drift, wrap_for_runtime
from db_ops.lib.target_profile import SOURCE_CONFIG, SOURCE_REQUEST, TargetProfile


def _target(*, platform="linux", runtime="", runtime_source=SOURCE_REQUEST, **runtime_target):
    profile = TargetProfile(platform=platform, runtime=runtime,
                            sources={"runtime": runtime_source} if runtime else {})
    return HostTarget(server_id="t", host="10.0.0.1", platform=platform,
                      access={"method": "ssh"}, profile=profile, runtime_target=runtime_target)


# --------------------------------------------------------------------------- #
# Where the command lands
# --------------------------------------------------------------------------- #
def test_a_plain_host_command_is_left_exactly_as_it_was():
    """The path every existing caller is on. It must not move."""
    assert wrap_for_runtime(_target(), "df -h /") == "df -h /"


def test_a_stated_docker_runtime_steps_into_the_container():
    wrapped = wrap_for_runtime(_target(runtime="docker", container="pg_ha-primary"), "psql --version")
    assert wrapped == "docker exec -i pg_ha-primary sh -lc 'psql --version'"


def test_an_inferred_runtime_does_not_move_the_command():
    """`container_name` on an instance is enough to *describe* a target and deliberately not
    enough to *redirect* a command — otherwise every existing run-cmd against a containerised
    instance would silently stop running on its host. `hostcmd`'s header made this call first:
    "runtime is stated rather than guessed"."""
    inferred = _target(runtime="docker", runtime_source=SOURCE_CONFIG, container="pg_ha-primary")
    assert wrap_for_runtime(inferred, "psql --version") == "psql --version"


def test_a_docker_runtime_without_a_container_is_refused():
    """Without the name the command runs on the host and reports, quite truthfully, that the
    database is not there — a success that answers the wrong question."""
    with pytest.raises(host_ops.HostOpsError, match='needs a "container"'):
        wrap_for_runtime(_target(runtime="docker"), "psql --version")


def test_k8s_names_its_pod_and_namespace():
    wrapped = wrap_for_runtime(
        _target(runtime="k8s", pod="pg-0", namespace="db", pod_container="postgres"), "psql -c 'select 1'")
    assert wrapped.startswith("kubectl exec -i -n db pg-0 -c postgres -- sh -lc ")


# --------------------------------------------------------------------------- #
# The same, under PowerShell quoting
# --------------------------------------------------------------------------- #
def test_a_windows_host_gets_powershell_quoting_not_posix():
    """The Linux form quotes with shlex — POSIX rules a PowerShell host does not read. The 2026-08-10
    incident behind `hostcmd.wrap`'s -EncodedCommand comment is the same class: a POSIX-quoted
    literal arrived at PowerShell as `'''C:\\bak'''` and the whole script was refused."""
    wrapped = wrap_for_runtime(
        _target(platform="windows", runtime="docker", container="mssql"), "sqlcmd -Q 'SELECT 1'")
    assert wrapped == "docker exec -i 'mssql' sh -lc 'sqlcmd -Q ''SELECT 1'''"


def test_a_windows_container_can_name_its_own_shell():
    """Which shell exists inside is a property of the image, not of the host: a Linux container on
    a Windows Docker host still wants `sh`, a Windows container wants `cmd /c`, and the host OS
    cannot answer that. So it is configured, not derived."""
    wrapped = wrap_for_runtime(
        _target(platform="windows", runtime="docker", container="win-mssql", container_shell="cmd /c"),
        "sqlcmd -Q \"SELECT 1\"")
    assert wrapped == "docker exec -i 'win-mssql' cmd /c 'sqlcmd -Q \"SELECT 1\"'"


def test_windows_k8s_is_composed_the_same_way():
    wrapped = wrap_for_runtime(
        _target(platform="windows", runtime="k8s", pod="pg-0", namespace="db"), "hostname")
    assert wrapped == "kubectl exec -i -n 'db' 'pg-0' -- sh -lc 'hostname'"


# --------------------------------------------------------------------------- #
# Which PowerShell the script is written for
# --------------------------------------------------------------------------- #
def test_the_legacy_script_uses_only_cmdlets_powershell_2_has():
    """The whole point of the variant. `Get-CimInstance` and `ConvertTo-Json` are 3.0; `Get-Service`
    gained `StartType` only in 4.0, which is why services come from `Win32_Service`."""
    script = host_ops._WINDOWS_FACTS_PS2
    for modern in ("Get-CimInstance", "ConvertTo-Json", "StartType", "[ordered]"):
        assert modern not in script, f"{modern} does not exist on PowerShell 2.0"
    for needed in ("Get-WmiObject Win32_OperatingSystem", "Get-WmiObject Win32_LogicalDisk",
                   "Get-WmiObject Win32_Service", "ConvertToDateTime"):
        assert needed in script


def test_both_windows_scripts_answer_the_same_questions():
    """A caller asking "is this host safe to touch" must get the same shape on either dialect, or
    every consumer downstream needs to know which script ran."""
    modern, legacy = host_ops._WINDOWS_FACTS_PS, host_ops._WINDOWS_FACTS_PS2
    for fact in ("whoami", "is_admin", "hostname", "last_boot", "remote_time",
                 "__SERVICES__", "PendingFileRenameOperations", "Component Based Servicing"):
        assert fact in modern and fact in legacy


def test_the_legacy_script_emits_records_that_the_shared_parser_reads():
    """It emits line records rather than JSON for the reason already written above the Linux
    script: a hand-rolled JSON printer breaks on the first value containing a quote, and
    PowerShell 2.0 has no `ConvertTo-Json`. One parser, so the two cannot drift."""
    stdout = "\n".join([
        "hostname\tOLD-SRV",
        "os\tMicrosoft Windows Server 2008 R2 Standard 6.1.7601",
        "whoami\tCORP\\svc",
        "is_admin\ttrue",
        "uptime_seconds\t172800",
        "last_boot\t2026-08-17T09:00:00",
        "remote_time\t2026-08-19T09:00:00",
        "disk\tC:\t10485760\t83886080",
        "service\tMSSQLSERVER\tRunning\tAuto",
        "pending_file_rename_count\t2",
        "reboot_reason\tPendingFileRenameOperations (2 entries)",
    ])

    facts = host_ops._parse_record_facts(stdout, "", platform="windows")

    assert facts["platform"] == "windows"
    assert facts["hostname"] == "OLD-SRV" and facts["whoami"] == "CORP\\svc"
    assert facts["is_admin"] is True
    assert facts["uptime_days"] == 2.0
    assert facts["disks"] == [{"mount": "C:", "free_gb": 10.0, "total_gb": 80.0}]
    assert facts["services"] == [{"name": "MSSQLSERVER", "status": "Running", "start_type": "Auto"}]
    assert facts["reboot_pending"]["required"] is True
    assert facts["reboot_pending"]["pending_file_rename_count"] == 2
    # `is_service_up` reads WMI's `State` the same as systemd's `active`.
    assert host_ops.is_service_up(facts["services"][0]["status"]) is True


def test_linux_still_reports_no_pending_renames_rather_than_an_absent_key():
    facts = host_ops._parse_record_facts("hostname\tweb-1", "", platform="linux")
    assert facts["reboot_pending"]["pending_file_rename_count"] == 0


# --------------------------------------------------------------------------- #
# Does the inventory still describe the machine
# --------------------------------------------------------------------------- #
def test_an_out_of_date_os_caption_is_reported_with_both_values():
    """`ACME-192-0-2-245` was recorded as Windows NT 6.2 and answers Windows Server 2016
    10.0.14393 — two generations out. The record is not decoration: it decides the PowerShell
    dialect and whether the host can be managed at all, and until 2026-08-19 nothing compared it
    with the host."""
    profile = TargetProfile(platform="windows", os_text="Windows NT 6.2 (Build 9200)",
                            os_major=6, os_minor=2)
    drift = os_drift(profile, "Microsoft Windows Server 2016 Standard 10.0.14393")
    assert "NT 6.2" in drift and "NT 10.0" in drift


def test_the_same_machine_written_two_ways_is_not_drift():
    """Compared on the NT version, not on the caption text — `Windows NT 6.2 (Build 9200)` and
    `Microsoft Windows Server 2012 Standard 6.2.9200` are one machine. A textual diff would cry
    wolf on every host and drown the one that matters."""
    profile = TargetProfile(platform="windows", os_text="Windows NT 6.2 (Build 9200)",
                            os_major=6, os_minor=2)
    assert os_drift(profile, "Microsoft Windows Server 2012 Standard 6.2.9200") == ""


def test_nothing_to_compare_is_silence_not_a_warning():
    assert os_drift(None, "Microsoft Windows Server 2019 10.0.17763") == ""
    assert os_drift(TargetProfile(platform="windows"), "Microsoft Windows Server 2019 10.0.17763") == ""
    assert os_drift(TargetProfile(platform="linux", os_text="Linux (Ubuntu 22.04)"), "Ubuntu 24.04") == ""
