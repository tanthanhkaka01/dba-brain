"""Where a command runs is stated in the request, and `common` builds the command line for it.

The same engine turns up four ways on this estate - a Windows VM, an Ubuntu VM, a container on
Ubuntu, a pod on Kubernetes - and the difference is entirely in how you step into it. Written once
here so it is not written four times slightly differently: the quoting, the `sudo`, and the login
shell are each a thing that is silently wrong until a path has a space in it or a binary is not on
the bare exec PATH.

`runtime` is stated rather than inferred from a `container` field being present. Absent, that field
would mean both "run on the host" and "the caller forgot", and those must not look the same when
the command being wrapped is a restore.
"""

from __future__ import annotations

import base64

import pytest

from db_ops.common.hostcmd import DOCKER, K8S, LINUX, WINDOWS, HostCommandError, parse_host, wrap


def test_a_linux_host_runs_the_command_unchanged():
    assert wrap(parse_host({"runtime": LINUX, "host": "h"}), "ls /b") == "ls /b"


def test_docker_steps_into_the_container():
    host = parse_host({"runtime": DOCKER, "host": "h", "container": "ora_dg_lab-primary"})
    assert wrap(host, "rman target /") == (
        "docker exec -i ora_dg_lab-primary sh -lc 'rman target /'")


def test_docker_uses_a_login_shell():
    """rman and pg_controldata are on a login shell's PATH and not on the bare exec
    environment's - otherwise discovered once per caller as "command not found" for a binary
    that is plainly installed."""
    assert " sh -lc " in wrap(parse_host({"runtime": DOCKER, "container": "c"}), "rman")


def test_sudo_is_stated_not_guessed():
    plain = wrap(parse_host({"runtime": DOCKER, "container": "c"}), "x")
    elevated = wrap(parse_host({"runtime": DOCKER, "container": "c", "sudo": True}), "x")
    assert not plain.startswith("sudo ")
    assert elevated.startswith("sudo docker exec")


def test_k8s_names_its_namespace_and_pod():
    host = parse_host({"runtime": K8S, "pod": "pg-0", "namespace": "db", "pod_container": "pg"})
    command = wrap(host, "psql -c 'select 1'")
    assert "kubectl exec -i -n db pg-0 -c pg --" in command


def test_windows_runs_through_powershell_without_a_profile():
    """A user profile can change what a restore sees, so it is excluded on purpose."""
    command = wrap(parse_host({"runtime": WINDOWS, "host": "vm"}), "Get-ChildItem D:\bak")
    assert command.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")


def test_a_windows_command_survives_cmd_exe_because_it_is_encoded():
    """`-Command` with shell quoting does not survive the trip and this is not theoretical.

    The command passes through cmd.exe locally, or through whatever shell the Windows OpenSSH
    server runs, and neither understands `shlex.quote`'s POSIX single quotes: a PowerShell literal
    `'C:\\bak\\a.bkp'` arrived as `'''C:\\bak\\a.bkp'''` and PowerShell refused the whole script
    with "Unexpected token". Base64 has nothing either shell treats specially, so the payload
    cannot be reinterpreted on the way.
    """
    script = "Remove-Item -LiteralPath 'D:\\bak\\it''s a backup & more.bak' -Force"
    command = wrap(parse_host({"runtime": WINDOWS, "host": "vm"}), script)

    encoded = command.rsplit(" ", 1)[1]
    assert base64.b64decode(encoded).decode("utf-16-le") == script
    # Nothing a shell would act on is left in the command line itself.
    assert "'" not in encoded and "&" not in encoded


def test_a_path_with_a_space_survives_the_wrapping():
    """The quoting is the whole reason this is one function rather than four f-strings."""
    command = wrap(parse_host({"runtime": DOCKER, "container": "c"}), "ls '/b/my backups'")
    assert "my backups" in command


def test_docker_without_a_container_is_refused():
    """It would otherwise run on the host and report, quite truthfully, that the database is
    not there."""
    with pytest.raises(HostCommandError, match="container is required"):
        parse_host({"runtime": DOCKER, "host": "h"})


def test_k8s_without_a_pod_is_refused():
    with pytest.raises(HostCommandError, match="pod is required"):
        parse_host({"runtime": K8S, "host": "h"})


def test_an_unknown_runtime_is_refused_by_name():
    with pytest.raises(HostCommandError, match="runtime must be one of"):
        parse_host({"runtime": "vmware", "host": "h"})


def test_no_host_means_this_machine():
    """The worker uses the same command against its own filesystem."""
    assert parse_host({"runtime": LINUX}).is_local is True
    assert parse_host({"runtime": LINUX, "host": "h"}).is_local is False


# --------------------------------------------------------------------------- #
# How a host is REACHED is a different question from what runs on it
# --------------------------------------------------------------------------- #
def test_access_defaults_to_ssh_so_nothing_existing_changes():
    """Every host block written before `access` existed meant SSH, and still does."""
    host = parse_host({"runtime": LINUX, "host": "203.0.113.188", "username": "ubuntu"})

    assert host.access == "ssh"
    assert host.is_winrm is False
    assert host.port == 22


def test_a_windows_host_can_be_reached_by_winrm():
    """The conflation this fixes cost the whole Windows half of the estate: `runtime: windows`
    meant "a Windows host with an OpenSSH server", and exactly one box here has one. The other
    thirteen SQL Servers are reached by WinRM - what cmd_access.method has said all along."""
    host = parse_host({"runtime": WINDOWS, "access": "winrm", "host": "192.0.2.115",
                       "username": r"allianceone\erpadmin", "password": "x"})

    assert host.is_winrm is True
    assert host.is_windows is True
    assert host.port == 5985


def test_method_is_accepted_as_a_spelling_of_access():
    """db_instances.json's cmd_access already says `method`, so that block can be handed straight
    through instead of being translated by every caller."""
    assert parse_host({"runtime": WINDOWS, "method": "winrm", "host": "h"}).is_winrm is True


def test_the_winrm_port_follows_ssl():
    """5985 plain, 5986 over TLS. Getting it wrong is a connection refused that reads like a
    firewall rule rather than a default."""
    assert parse_host({"runtime": WINDOWS, "access": "winrm", "host": "h"}).port == 5985
    assert parse_host({"runtime": WINDOWS, "access": "winrm", "host": "h", "ssl": True}).port == 5986
    assert parse_host({"runtime": WINDOWS, "access": "winrm", "host": "h", "port": 5999}).port == 5999


def test_an_unknown_access_is_refused_by_name():
    with pytest.raises(HostCommandError, match="access must be one of"):
        parse_host({"runtime": WINDOWS, "access": "telnet", "host": "h"})


def test_winrm_delegates_to_remote_exec_rather_than_speaking_it_here(monkeypatch):
    """A second WinRM client would be a second set of quoting bugs to find, on the machines where
    being wrong means a production SQL Server. remote_exec has run against these same hosts on
    every metrics cycle for months."""
    from db_ops.common import hostcmd, remote_exec

    captured = {}

    class _Result:
        exit_code, stdout, stderr = 0, "RESULT=ok\n", ""

    def fake_run_script(access, script, *, env=None, timeout_seconds=None):
        captured.update({"access": access, "script": script, "env": env})
        return _Result()

    monkeypatch.setattr(remote_exec, "run_script", fake_run_script)
    monkeypatch.setattr(hostcmd, "open_client",
                        lambda host: pytest.fail("WinRM must not open an SSH client"))

    host = parse_host({"runtime": WINDOWS, "access": "winrm", "host": "192.0.2.115",
                       "username": "u", "password": "p"})
    result = hostcmd.run_script(host, "'hi'", env={"BACKUP_LEVEL": "full"})

    assert captured["access"]["method"] == "winrm"
    assert captured["access"]["platform"] == "windows"
    assert captured["env"] == {"BACKUP_LEVEL": "full"}
    # No `export FOO=` prelude and no base64: the WinRM session already lands in PowerShell on
    # that host, and wrapping would start a second one inside it.
    assert captured["script"] == "'hi'"
    assert result["stdout"] == "RESULT=ok\n"


# --------------------------------------------------------------------------- #
# Running a real script on a Windows host
# --------------------------------------------------------------------------- #
def test_a_windows_script_is_not_fed_on_stdin(monkeypatch):
    """`powershell -Command -` reads a script from stdin, returns 0, and produces nothing for
    anything multi-line: a here-string or a `function` block yields no output at all. For a backup
    script that is "did nothing, reported success" - the exact failure the RESULT=ok receipt exists
    to catch. Measured against 192.0.2.250 on 2026-08-07, both forms: exit 0, empty stdout.

    `-EncodedCommand` is not the way out either. The SQL Server backup script is 15 KB, which is
    ~41 KB of base64 against a 32767-character Windows command line.
    """
    from db_ops.common import hostcmd

    called = {}

    def fake_file_run(host, payload, *, timeout=None):
        called.update({"payload": payload, "timeout": timeout})
        return {"exit_code": 0, "stdout": "RESULT=ok\n", "stderr": ""}

    monkeypatch.setattr(hostcmd, "_run_windows_script_file", fake_file_run)
    monkeypatch.setattr(hostcmd, "open_client",
                        lambda host: pytest.fail("a Windows script must not be fed over exec_command"))

    host = parse_host({"runtime": WINDOWS, "host": "192.0.2.250", "username": "u",
                       "password": "p"})
    result = hostcmd.run_script(host, '$x = @"\nmulti\nline\n"@\n"RESULT=ok"', env={"A": "1"})

    assert result["stdout"] == "RESULT=ok\n"
    assert "$env:A = '1'" in called["payload"], "the env prelude travels with the script"


@pytest.mark.parametrize("reported,expected", [
    ("/C:/Users/appdbadmin/db_ops_abc.ps1", r"C:\Users\appdbadmin\db_ops_abc.ps1"),
    ("/D:/temp/x.ps1", r"D:\temp\x.ps1"),
    ("C:/already/windows.ps1", r"C:\already\windows.ps1"),
])
def test_an_sftp_path_is_translated_for_powershell(reported, expected):
    """Windows OpenSSH's SFTP speaks POSIX: `normalize()` answers `/C:/Users/x/y.ps1`, and `-File`
    on that fails with "The given path's format is not supported" - a message that reads like a
    permissions or quoting problem and is neither."""
    from db_ops.common.hostcmd import _windows_path

    assert _windows_path(reported) == expected
