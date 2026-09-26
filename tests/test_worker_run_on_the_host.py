"""An upgrade happens on the worker HOST, and the control app has to reach it there.

`worker-run` wraps every command in `docker exec`, so the compose file, `docker pull` and the
bind-mounted folders were out of reach from the master: upgrading the published image meant an
interactive ssh session, which this master does not have (no stored host key, the login lives in the
secret store). `--on-host` runs the command on the host with the same login, and `--sudo` runs it
as root: `run-cmd` puts the line under `sudo -S` with that login's password on stdin (0.24.0 - it
was built here, with the password fed by hand, until `control` stopped importing `common`).
"""

from __future__ import annotations

import pytest

from db_ops.control import worker_exec


@pytest.fixture
def remote(monkeypatch):
    sent = {}

    class Client:
        def close(self):
            sent["closed"] = True

    monkeypatch.setattr(worker_exec, "ssh_connect", lambda *args, **kwargs: Client())

    def fake_run(client, command, *, sudo=False, check=True, quiet=False):
        sent["command"] = command
        sent["sudo"] = sudo
        return 0

    monkeypatch.setattr(worker_exec, "ssh_run", fake_run)
    return sent


def _run(**over):
    kwargs = {"host": "192.0.2.10", "user": "dev", "password": "pw", "command": ["--", "ls", "-la"]}
    kwargs.update(over)
    return worker_exec.run_worker_command(**kwargs)


def test_by_default_the_command_runs_inside_the_container(remote):
    assert _run() == 0
    assert remote["command"] == "docker exec dbabrain ls -la"
    assert remote["sudo"] is False


def test_on_host_runs_one_argument_as_a_shell_line(remote):
    _run(command=["--", "cd /opt/dbabrain && docker compose ps"], on_host=True)

    assert remote["command"] == "cd /opt/dbabrain && docker compose ps"
    assert remote["closed"] is True


def test_on_host_with_sudo_runs_the_line_as_root(remote):
    _run(command=["--", "cp a b"], on_host=True, sudo=True)

    assert remote["command"] == "cp a b"
    assert remote["sudo"] is True


def test_sudo_without_on_host_never_sends_the_password(remote):
    _run(sudo=True)

    assert remote["command"].startswith("docker exec ")
    assert remote["sudo"] is False
