"""`cmd_access.method: local` pointed at a remote host.

`local` runs the command right here — inside the worker container. Pointed at another machine it
does not fail: it succeeds and returns **this** machine's numbers under that host's name. The
target looks monitored and healthy while none of its OS data is its own.

Found in production on 2026-08-01: `ACME-192-0-2-111` was declared Ubuntu 22.04 with
`method: local`, and its `OS_INFO` reported the db_ops image (Ubuntu 24.04) with hostname
`dbcd646aaa1c` — a container id that changed on every run, because `docker compose run` creates a
new container each time. CPU, memory, disk and uptime for that server had been the collector's
own. The only visible symptom was `OS_EVENTLOG_CRITICAL` saying journalctl was missing, which is
true of the container and says nothing about the host.
"""

from __future__ import annotations

import socket

import pytest

from db_ops.common import remote_exec
from db_ops.common.remote_exec import RemoteExecError


def _access(host: str) -> dict:
    return {"enabled": True, "method": "local", "host": host, "shell": "bash"}


def test_local_pointed_at_another_machine_is_refused():
    with pytest.raises(RemoteExecError, match="another machine"):
        remote_exec.open_session(_access("192.0.2.111"))


def test_the_message_names_the_host_and_the_fix():
    """A config error has to say which entry is wrong and what to put there instead; "local
    execution failed" would send someone to look at the network."""
    with pytest.raises(RemoteExecError) as excinfo:
        remote_exec.open_session(_access("192.0.2.253"))

    message = str(excinfo.value)
    assert "192.0.2.253" in message
    assert "ssh" in message and "winrm" in message


@pytest.mark.parametrize("host", ["", "localhost", "127.0.0.1", "::1", "LOCALHOST", " 127.0.0.1 "])
def test_the_genuinely_local_spellings_still_work(host):
    """The daemon and the SRE app legitimately run local commands on the node itself; the guard
    must not break them."""
    session = remote_exec.open_session(_access(host))
    assert isinstance(session, remote_exec.LocalSession)


def test_this_machine_by_its_own_hostname_is_local():
    session = remote_exec.open_session(_access(socket.gethostname()))
    assert isinstance(session, remote_exec.LocalSession)


def test_ssh_and_winrm_are_untouched_by_the_guard(monkeypatch):
    """The guard applies only to `local`. A remote host is exactly what ssh/winrm are for."""
    monkeypatch.setattr(remote_exec, "SshSession", lambda access: ("ssh", access))
    monkeypatch.setattr(remote_exec, "WinrmSession", lambda access: ("winrm", access))

    ssh = remote_exec.open_session(
        {"enabled": True, "method": "ssh", "host": "192.0.2.111",
         "username": "u", "password": "p"})
    assert ssh[0] == "ssh"
