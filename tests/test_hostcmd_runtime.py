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
    assert plain.startswith("docker exec") and "sudo" not in plain
    # Asked-for sudo is a fallback decided on the host, not a prefix: `sudo docker` with a sudo
    # that wants a password fails outright, while the SSH user is usually in the docker group.
    assert elevated.startswith('$(docker info >/dev/null 2>&1 && echo docker || echo "sudo docker") exec')


def test_the_sudo_fallback_runs_as_a_real_shell_would_read_it():
    """The expression is typed into the remote shell; checked here in one, where one exists."""
    import shutil
    import subprocess

    import pytest

    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no POSIX shell here")
    from db_ops.lib.shell import docker_cli

    out = subprocess.run([sh, "-c", f"docker() {{ return 0; }}; echo {docker_cli(True)}"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "docker"
    out = subprocess.run([sh, "-c", f"docker() {{ return 1; }}; echo {docker_cli(True)}"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "sudo docker"


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
                       "username": r"examplecorp\erpadmin", "password": "x"})

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


class _Session:
    """A `remote_exec` session that records what it was asked to run."""

    def __init__(self, access, captured):
        self.access, self.captured = access, captured

    def run_script(self, script, *, env=None, timeout_seconds=None):
        from db_ops.common import remote_exec

        self.captured.update({"access": self.access, "script": script, "env": env})
        return remote_exec.RemoteResult(method=self.access["method"], host=self.access.get("host", ""),
                                        command="<script>", exit_code=0, stdout="RESULT=ok\n", stderr="",
                                        duration_seconds=0.1)

    def close(self):
        self.captured["closed"] = True


def _record(monkeypatch):
    from db_ops.common import remote_exec

    captured: dict = {}
    monkeypatch.setattr(remote_exec, "open_session", lambda access: _Session(access, captured))
    return captured


def test_winrm_delegates_to_remote_exec_rather_than_speaking_it_here(monkeypatch):
    """A second WinRM client would be a second set of quoting bugs to find, on the machines where
    being wrong means a production SQL Server. remote_exec has run against these same hosts on
    every metrics cycle for months."""
    from db_ops.common import hostcmd

    captured = _record(monkeypatch)

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
    assert captured["closed"] is True


# --------------------------------------------------------------------------- #
# Running a real script on a host
# --------------------------------------------------------------------------- #
def test_every_script_runs_through_remote_exec_the_one_executor(monkeypatch):
    """This module had its own paramiko client, its own stdin-fed ``bash -s`` and its own Windows
    file upload - a second executor beside ``remote_exec`` (0.25.0, the operator: one way to run
    anything on a host). It names the host now and hands the script over whole; ``remote_exec``
    writes it to a private file there and runs it with stdin closed."""
    from db_ops.common import hostcmd

    captured = _record(monkeypatch)

    host = parse_host({"runtime": "docker", "container": "pg", "host": "192.0.2.31",
                       "username": "u", "key_file": "/keys/id"})
    hostcmd.run_script(host, "docker exec pg pg_basebackup", env={"A": "1"})

    assert captured["access"] == {"method": "ssh", "host": "192.0.2.31", "port": 22, "username": "u",
                                  "password": "", "shell": "bash", "platform": "linux",
                                  "auth_type": "key", "key_file": "/keys/id"}
    assert captured["script"] == "docker exec pg pg_basebackup", "on the host, not wrapped into the container"
    assert captured["env"] == {"A": "1"}


def test_a_windows_script_over_ssh_is_powershell_and_never_stdin(monkeypatch):
    """`powershell -Command -` reads a script from stdin, returns 0, and produces nothing for
    anything multi-line: a here-string or a `function` block yields no output at all. For a backup
    script that is "did nothing, reported success" - the exact failure the RESULT=ok receipt exists
    to catch. Measured against 192.0.2.250 on 2026-08-07, both forms: exit 0, empty stdout.

    `-EncodedCommand` is not the way out either: the SQL Server backup script is 15 KB, ~41 KB of
    base64 against a 32767-character Windows command line. ``remote_exec`` places it as a ``.ps1``
    and runs it with ``-File`` (``test_common_remote_exec.py``).
    """
    from db_ops.common import hostcmd

    captured = _record(monkeypatch)

    host = parse_host({"runtime": WINDOWS, "host": "192.0.2.250", "username": "u", "password": "p"})
    result = hostcmd.run_script(host, '$x = @"\nmulti\nline\n"@\n"RESULT=ok"', env={"A": "1"})

    assert (captured["access"]["method"], captured["access"]["shell"]) == ("ssh", "powershell")
    assert captured["access"]["auth_type"] == "password"
    assert captured["env"] == {"A": "1"}, "the env prelude is remote_exec's, in the script's own shell"
    assert result["stdout"] == "RESULT=ok\n"


@pytest.mark.parametrize("reported,expected", [
    ("/C:/Users/appdbadmin/db_ops_abc.ps1", r"C:\Users\appdbadmin\db_ops_abc.ps1"),
    ("/D:/temp/x.ps1", r"D:\temp\x.ps1"),
    ("C:/already/windows.ps1", r"C:\already\windows.ps1"),
])
def test_an_sftp_path_is_translated_for_powershell(reported, expected):
    """Windows OpenSSH's SFTP speaks POSIX: `normalize()` answers `/C:/Users/x/y.ps1`, and `-File`
    on that fails with "The given path's format is not supported" - a message that reads like a
    permissions or quoting problem and is neither."""
    from db_ops.common.remote_exec import windows_path

    assert windows_path(reported) == expected
