"""The shared remote-access layer: db_ops.common.remote_exec.

Reaching a VM (ssh / winrm / local) and running a command on it used to be re-implemented
per app. These tests pin the contract every app now depends on: a **JSON access object**
in, a :class:`RemoteResult` out, one set of auth rules, and errors that say whether the
credential or the network is at fault.
"""

import socket
import sys
import threading
import time
import types

import pytest

from db_ops.common import remote_exec as rx


# ---------------------------------------------------------------------------
# Fakes: paramiko (ssh) and pypsrp (winrm)
# ---------------------------------------------------------------------------
def _fake_paramiko(monkeypatch, *, stdout=b"", stderr=b"", exit_code=0, connect_raises=None):
    """Install a fake paramiko and return the dict recording what it was asked to do."""
    captured: dict = {}

    class _Channel:
        def recv_exit_status(self):
            return exit_code

        def shutdown_write(self):
            captured["shutdown_write"] = True

    class _File:
        def __init__(self, data: bytes = b""):
            self._data = data
            self.channel = _Channel()

        def read(self) -> bytes:
            return self._data

        def write(self, data: bytes) -> None:
            captured["stdin"] = captured.get("stdin", b"") + data

        def flush(self) -> None:
            captured["flushed"] = True

    class _Client:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, hostname, **kwargs):
            captured["hostname"] = hostname
            captured["connect_kwargs"] = kwargs
            if connect_raises is not None:
                raise connect_raises

        def exec_command(self, command, **kwargs):
            captured.setdefault("commands", []).append(command)
            captured["command"] = command
            # What the script file held at the moment it ran - it is removed afterwards.
            captured["files_at_run"] = {name: dict(entry) for name, entry in files.items()}
            return _File(), _File(stdout), _File(stderr)

        def open_sftp(self):
            return _Sftp()

        def close(self):
            captured["closed"] = True

    files: dict[str, dict] = captured.setdefault("files", {})

    class _Handle:
        def __init__(self, name: str):
            self.name = name

        def write(self, data: bytes) -> None:
            files[self.name]["bytes"] += data
            # A byte written while the file is still readable by others is the leak to catch.
            files[self.name]["written_at_mode"] = files[self.name]["mode"]

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

    class _Sftp:
        def open(self, name, mode):
            if "x" in mode and name in files:
                raise OSError("exists")
            files[name] = {"mode": 0o644, "bytes": b"", "open_mode": mode}
            return _Handle(name)

        def chmod(self, name, mode):
            files[name]["mode"] = mode

        def normalize(self, name):
            return captured.get("home", "/home/u") + "/" + name

        def remove(self, name):
            captured.setdefault("removed", []).append(name)
            files.pop(name, None)

        def close(self):
            return None

    ssh_exception = types.ModuleType("paramiko.ssh_exception")
    ssh_exception.NoValidConnectionsError = OSError

    paramiko = types.ModuleType("paramiko")
    paramiko.SSHClient = _Client
    paramiko.AutoAddPolicy = lambda: None
    paramiko.AuthenticationException = PermissionError
    paramiko.ssh_exception = ssh_exception

    monkeypatch.setitem(sys.modules, "paramiko", paramiko)
    monkeypatch.setitem(sys.modules, "paramiko.ssh_exception", ssh_exception)
    return captured


def _fake_pypsrp(monkeypatch, *, result=("[]", "", 0)):
    captured: dict = {}

    class _Client:
        def __init__(self, host, **kwargs):
            captured["host"] = host
            captured.update(kwargs)

        def execute_ps(self, script):
            captured["script"] = script
            return result

    client_module = types.ModuleType("pypsrp.client")
    client_module.Client = _Client
    package = types.ModuleType("pypsrp")
    package.client = client_module
    monkeypatch.setitem(sys.modules, "pypsrp", package)
    monkeypatch.setitem(sys.modules, "pypsrp.client", client_module)
    return captured


# ---------------------------------------------------------------------------
# The JSON access object
# ---------------------------------------------------------------------------
def test_a_cmd_access_object_from_db_instances_needs_no_translation():
    """The JSON already stored in db_instances.json is the input — that is the whole point
    of one shared layer. Whatever the config leaves out gets the documented default."""
    access = rx.RemoteAccess.from_json(
        {"enabled": True, "method": "ssh", "host": "10.0.0.5", "platform": "linux"},
        credential={"credential_name": "worker", "username": "ubuntu", "password_ref": "PW"},
        secrets={"PW": "s3cret"},
    )

    assert access.method == "ssh"
    assert access.port == 22                # ssh default
    assert access.shell == "bash"           # from platform=linux
    assert access.username == "ubuntu"      # merged in from the credential object
    assert access.auth_type == "key"        # ssh default
    assert access.timeout_seconds == 30


def test_winrm_defaults_follow_ssl_and_are_always_powershell():
    plain = rx.RemoteAccess.from_json({"method": "winrm", "host": "h"}, credential={"username": "u", "password": "p"})
    secure = rx.RemoteAccess.from_json({"method": "winrm", "host": "h", "ssl": True},
                                       credential={"username": "u", "password": "p"})

    assert (plain.port, plain.ssl) == (5985, False)
    assert (secure.port, secure.ssl) == (5986, True)
    assert plain.shell == secure.shell == "powershell"


def test_an_access_object_is_safe_to_log():
    """to_dict() is what ends up in a log line or an event payload, so the password must
    never be in it — only whether one was resolved."""
    access = rx.RemoteAccess.from_json(
        {"method": "ssh", "host": "h", "auth_type": "password"},
        credential={"username": "u", "password": "hunter2"},
    )

    assert access.password == "hunter2"
    assert access.to_dict()["password"] == "***"
    assert "hunter2" not in repr(access)


def test_an_unknown_method_or_shell_is_rejected_by_name():
    with pytest.raises(rx.RemoteExecError, match="Unsupported remote access method 'telnet'"):
        rx.RemoteAccess.from_json({"method": "telnet", "host": "h"})
    with pytest.raises(rx.RemoteExecError, match="Unsupported shell 'zsh'"):
        rx.RemoteAccess.from_json({"method": "ssh", "host": "h", "shell": "zsh"})
    with pytest.raises(rx.RemoteExecError, match="needs a 'host'"):
        rx.RemoteAccess.from_json({"method": "ssh"})


def test_a_password_resolves_explicit_then_from_the_secrets_handed_over(monkeypatch):
    """Owner decision G3.5: no environment lookup. `password_env` is the old spelling of a ref."""
    monkeypatch.setenv("DB_OPS_TEST_REF", "in the environment, never read")

    explicit = {"password": "literal", "password_ref": "DB_OPS_TEST_REF"}
    assert rx.resolve_secret_value(explicit) == "literal"

    store = {"DB_OPS_TEST_REF": "from-store"}
    assert rx.resolve_secret_value({"password_ref": "DB_OPS_TEST_REF"}, secrets=store) == "from-store"
    assert rx.resolve_secret_value({"password_env": "DB_OPS_TEST_REF"}, secrets=store) == "from-store"
    with pytest.raises(rx.RemoteExecError, match="not among the secrets"):
        rx.resolve_secret_value({"password_ref": "DB_OPS_TEST_REF"})
    with pytest.raises(rx.RemoteExecError, match="two different secrets"):
        rx.resolve_secret_value({"password_ref": "A", "password_env": "B"}, secrets={"A": "1", "B": "2"})

    # Nothing named at all is not an error: SSH key auth legitimately has no password.
    assert rx.resolve_secret_value({}) == ""


def test_ssh_key_auth_never_fails_on_an_unresolvable_passphrase(monkeypatch, tmp_path):
    """The key is the credential; a password_ref that goes nowhere must not break the
    connection the way it would for password auth."""
    key = tmp_path / "id_test"
    key.write_text("KEY", encoding="utf-8")

    access = rx.RemoteAccess.from_json(
        {"method": "ssh", "host": "h", "auth_type": "key", "key_file": str(key), "password_ref": "MISSING_REF"},
    )

    assert access.key_file == str(key)
    assert access.password == ""


# ---------------------------------------------------------------------------
# Running commands over SSH
# ---------------------------------------------------------------------------
def test_a_command_over_ssh_returns_rc_stdout_stderr(monkeypatch):
    captured = _fake_paramiko(monkeypatch, stdout=b"active\n", stderr=b"warn\n", exit_code=0)

    result = rx.run_command(
        {"method": "ssh", "host": "10.0.0.5", "username": "ubuntu", "auth_type": "password", "password": "pw"},
        "systemctl is-active docker",
    )

    assert captured["command"] == "systemctl is-active docker"
    assert captured["connect_kwargs"]["port"] == 22
    assert (result.exit_code, result.stdout, result.stderr) == (0, "active\n", "warn\n")
    assert result.ok and result.host == "10.0.0.5" and result.method == "ssh"


def test_an_argv_is_quoted_and_a_cwd_becomes_a_cd_prefix(monkeypatch):
    captured = _fake_paramiko(monkeypatch)

    with rx.open_session({"method": "ssh", "host": "h", "username": "u", "auth_type": "password",
                          "password": "pw"}) as session:
        session.run(["docker", "compose", "-f", "my file.yml", "up"], cwd="/opt/db ops")

    assert captured["command"] == "cd '/opt/db ops' && docker compose -f 'my file.yml' up"


def test_a_bash_script_is_a_private_file_run_with_stdin_closed_and_carries_its_env(monkeypatch):
    """Nothing of ours reaches the remote environment on its own — SSH does not forward env
    vars — so collector inputs travel inside the script text.

    As a **file** since 0.25.0: fed to ``bash -s`` on stdin, any command in the script that reads
    stdin read the rest of the script instead, and the run ended early with exit 0 (1.65)."""
    captured = _fake_paramiko(monkeypatch, stdout=b"[]")

    rx.run_script(
        {"method": "ssh", "host": "h", "username": "u", "auth_type": "password", "password": "pw", "shell": "bash"},
        "echo hi",
        env={"OS_SERVICE_NAMES": "It's here"},
    )

    [(name, placed)] = captured["files_at_run"].items()
    path = f"/home/u/{name}"
    assert name.startswith(rx.SCRIPT_FILE_PREFIX) and name.endswith(".sh")
    assert placed["open_mode"] == "wx", "created exclusively - never another run's file"
    assert placed["written_at_mode"] == 0o600, "private before a byte of it is written"
    assert placed["bytes"] == b"export OS_SERVICE_NAMES='It'\\''s here'\necho hi"
    assert captured["command"] == f"trap 'rm -f {path}' EXIT; bash {path} < /dev/null"
    assert captured.get("stdin") is None, "the script is not the shell's stdin any more"
    assert captured["removed"] == [name] and not captured["files"]


def test_a_powershell_script_is_a_file_run_with_file(monkeypatch):
    """A file, not ``-EncodedCommand``: base64 of UTF-16 runs out of the 32,767-character command
    line past ~8 KB of script, and stdin is read and silently ignored for anything multi-line.
    Windows OpenSSH's SFTP answers a POSIX path, which ``-File`` refuses - it is turned back."""
    captured = _fake_paramiko(monkeypatch, stdout=b"[]")
    captured["home"] = "/C:/Users/u"

    rx.run_script(
        {"method": "ssh", "host": "h", "username": "u", "auth_type": "password", "password": "pw",
         "platform": "windows"},
        "Get-Date",
    )

    [(name, placed)] = captured["files_at_run"].items()
    assert name.endswith(".ps1")
    assert placed["bytes"] == b"\xef\xbb\xbfGet-Date", "UTF-8 with a BOM, or PowerShell 5.1 reads ANSI"
    assert captured["command"] == ("powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                                   f"-File 'C:\\Users\\u\\{name}'")
    assert captured.get("stdin") is None
    assert captured["removed"] == [name]


def test_a_command_is_unbounded_unless_the_caller_bounds_it(monkeypatch):
    """The connect timeout must never become the command timeout.

    Regression: they shared one number, so every remote command was capped at the 30s
    connect timeout. `docker compose up` pulling a 1.5 GB SQL Server image died at 30s with
    "SSH command timed out", and so did the Oracle Data Guard build — while a small
    PostgreSQL image slipped under the cap and looked fine.
    """
    captured = _fake_paramiko(monkeypatch)
    access = {"method": "ssh", "host": "h", "username": "u", "auth_type": "password",
              "password": "pw", "timeout_seconds": 30}

    with rx.open_session(access) as session:
        session.run(["docker", "compose", "up", "-d"])
        # The connect still uses its own short timeout...
        assert captured["connect_kwargs"]["timeout"] == 30
        # ...but the command is not given one at all.
        assert session._timeout(None) is None

        # A caller that wants a bound says so, per call...
        assert session._timeout(5) == 5

    # ...or on the access object, for every command in the session.
    with rx.open_session({**access, "command_timeout_seconds": 900}) as bounded:
        assert bounded._timeout(None) == 900
        assert bounded.access.timeout_seconds == 30      # connect timeout is untouched


def test_a_bounded_command_that_overruns_still_reports_a_timeout(monkeypatch):
    """Unbounded by default must not mean unbounded when asked — metrics relies on this."""
    class _Timeout:
        def __init__(self, *a, **k):
            pass

    captured = _fake_paramiko(monkeypatch)

    with rx.open_session({"method": "ssh", "host": "h", "username": "u",
                          "auth_type": "password", "password": "pw"}) as session:
        monkeypatch.setattr(
            session._connect(), "exec_command",
            lambda *a, **k: (_ for _ in ()).throw(socket.timeout("timed out")),
        )
        with pytest.raises(rx.RemoteTimeoutError, match="timed out after 7 seconds"):
            session.run("sleep 60", timeout_seconds=7)


def test_a_sudo_password_goes_over_stdin_never_onto_the_remote_argv(monkeypatch):
    """`ps` on the remote host shows the command line of every process; a password there
    would be readable by any local user."""
    captured = _fake_paramiko(monkeypatch)

    with rx.open_session({"method": "ssh", "host": "h", "username": "u", "auth_type": "password",
                          "password": "pw"}) as session:
        session.run_sudo("apt-get update", sudo_password="root-pw")

    assert captured["command"] == "sudo -S -p '' sh -c 'apt-get update'"
    assert "root-pw" not in captured["command"]
    assert captured["stdin"] == b"root-pw\n"


def test_check_turns_a_non_zero_exit_into_an_error_carrying_the_output(monkeypatch):
    _fake_paramiko(monkeypatch, stdout=b"", stderr=b"no such container\n", exit_code=1)

    access = {"method": "ssh", "host": "h", "username": "u", "auth_type": "password", "password": "pw"}
    result = rx.run_command(access, "docker inspect nope")
    assert not result.ok

    with pytest.raises(rx.RemoteExecError, match="no such container") as excinfo:
        rx.run_command(access, "docker inspect nope", check=True)
    assert excinfo.value.exit_code == 1


def test_a_rejected_credential_and_an_unreachable_host_are_different_errors(monkeypatch):
    """These need different fixes — rotate the credential, or open the port — so the caller
    must not have to pattern-match a message to tell them apart."""
    access = {"method": "ssh", "host": "h", "username": "u", "auth_type": "password", "password": "pw"}

    _fake_paramiko(monkeypatch, connect_raises=PermissionError("bad password"))
    with pytest.raises(rx.RemoteAuthError, match="authentication failed"):
        rx.run_command(access, "uptime")

    _fake_paramiko(monkeypatch, connect_raises=ConnectionRefusedError("refused"))
    with pytest.raises(rx.RemoteConnectError, match="port 22 is open"):
        rx.run_command(access, "uptime")

    _fake_paramiko(monkeypatch, connect_raises=TimeoutError("slow"))
    with pytest.raises(rx.RemoteTimeoutError, match="timed out"):
        rx.run_command(access, "uptime")


# ---------------------------------------------------------------------------
# Running commands over WinRM
# ---------------------------------------------------------------------------
def test_winrm_qualifies_a_bare_username_with_the_host(monkeypatch):
    """An unqualified name would be resolved against the *collector's* domain; qualifying it
    makes a local account on the target authenticate as itself."""
    captured = _fake_pypsrp(monkeypatch, result=("ok", "", 0))

    result = rx.run_script(
        {"method": "winrm", "host": "192.0.2.108", "port": 5985, "timeout_seconds": 12},
        "Get-SmbShare",
        credential={"username": "svc_db", "password": "pw"},
    )

    assert captured["username"] == "192.0.2.108\\svc_db"
    assert captured["port"] == 5985 and captured["connection_timeout"] == 12
    assert result.stdout == "ok" and result.ok


def test_winrm_streams_that_are_not_strings_are_coerced_to_text(monkeypatch):
    """pypsrp hands back a stream *object* for stderr; a metric row stores text."""

    class _Streams:
        def __str__(self):
            return "stream detail"

    _fake_pypsrp(monkeypatch, result=("not json", _Streams(), 0))

    result = rx.run_script({"method": "winrm", "host": "h"}, "whatever",
                           credential={"username": "u", "password": "p"})

    assert result.stdout == "not json"
    assert result.stderr == "stream detail"


def test_a_winrm_command_that_outruns_its_timeout_is_reported_as_a_timeout(monkeypatch):
    """None of pypsrp's own timeouts bound how long a *command* runs.

    ``connection_timeout``/``read_timeout`` bound one HTTP round trip and ``operation_timeout``
    bounds one WSMan Receive, so ``execute_ps`` polls for as long as the remote script keeps
    going. A metric declaring ``timeout: 60`` was measured holding a collect pass for 490
    seconds this way, and because the pass walked every server in one queue, every other
    machine's metrics went unsampled behind it. The wait has to be bounded here, by wall clock.
    """
    started = threading.Event()

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def execute_ps(self, script):
            started.set()
            time.sleep(30)  # far past the timeout below; the caller must not wait for it
            return ("never read", "", 0)

    client_module = types.ModuleType("pypsrp.client")
    client_module.Client = _Client
    package = types.ModuleType("pypsrp")
    package.client = client_module
    monkeypatch.setitem(sys.modules, "pypsrp", package)
    monkeypatch.setitem(sys.modules, "pypsrp.client", client_module)

    began = time.monotonic()
    with pytest.raises(rx.RemoteTimeoutError, match="timed out after 1 seconds on 10.1.1.1"):
        rx.run_script({"method": "winrm", "host": "10.1.1.1"}, "Start-Sleep 30",
                      credential={"username": "u", "password": "p"}, timeout_seconds=1)

    assert started.is_set(), "the script should have been started before the timeout fired"
    # The point of the fix: control comes back on time instead of after the command finishes.
    assert time.monotonic() - began < 10


def test_winrm_reports_the_host_when_the_transport_itself_fails(monkeypatch):
    class _Client:
        def __init__(self, *args, **kwargs):
            raise OSError("WSMan cannot process the request")

    client_module = types.ModuleType("pypsrp.client")
    client_module.Client = _Client
    package = types.ModuleType("pypsrp")
    package.client = client_module
    monkeypatch.setitem(sys.modules, "pypsrp", package)
    monkeypatch.setitem(sys.modules, "pypsrp.client", client_module)

    with pytest.raises(rx.RemoteExecError, match="WinRM execution failed on 10.1.1.1:5985"):
        rx.run_script({"method": "winrm", "host": "10.1.1.1"}, "Get-Date",
                      credential={"username": "u", "password": "p"})


# ---------------------------------------------------------------------------
# The Invoke-Command builder (backup/restore composes, then runs it itself)
# ---------------------------------------------------------------------------
def test_the_invoke_command_wrapper_is_built_in_one_place():
    script = rx.build_invoke_command_script(
        host="VM_IP",
        username="Administrator",
        password="p'w",
        script_body=["    param($SqlcmdPath, $Sql)", "    & $SqlcmdPath -Q $Sql"],
        arguments=["C:\\sqlcmd.exe", "SELECT 1;"],
        open_timeout_ms=30000,
        operation_timeout_ms=2147483647,
    )

    # -ComputerName stays right after Invoke-Command: the restore target-context guards
    # find the destination by that prefix.
    assert "Invoke-Command -ComputerName 'VM_IP' -Credential $credential -SessionOption $sessionOption -ScriptBlock {" in script
    assert "$sessionOption = New-PSSessionOption -OpenTimeout 30000 -OperationTimeout 2147483647" in script
    assert "ConvertTo-SecureString 'p''w' -AsPlainText -Force" in script   # doubled quote, not broken out of
    assert script.rstrip().endswith("} -ArgumentList 'C:\\sqlcmd.exe', 'SELECT 1;'")


def test_without_a_credential_the_wrapper_omits_it_entirely():
    script = rx.build_invoke_command_script(host="VM_IP", script_body="    Get-Date")

    assert "Invoke-Command -ComputerName 'VM_IP' -ScriptBlock {" in script
    assert "$credential" not in script
    assert "SessionOption" not in script


def test_the_builder_returns_a_runnable_powershell_argv():
    argv = rx.build_invoke_command_argv(host="VM_IP", script_body="    Get-Date")

    assert argv[1:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
    assert "Invoke-Command -ComputerName 'VM_IP'" in argv[-1]


# ---------------------------------------------------------------------------
# local, so "no remoting" is just another method
# ---------------------------------------------------------------------------
def test_local_is_the_same_api_as_a_remote_host():
    result = rx.run_command({"method": "local"}, [sys.executable, "-c", "print('hello')"])

    assert result.ok and result.stdout.strip() == "hello"
    assert result.method == "local"

    failed = rx.run_command({"method": "local"}, [sys.executable, "-c", "raise SystemExit(3)"])
    assert failed.exit_code == 3
    assert failed.to_dict()["exit_code"] == 3
