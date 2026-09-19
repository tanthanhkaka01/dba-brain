"""An upgrade happens on the worker HOST, and the control app has to reach it there.

`worker-run` wraps every command in `docker exec`, so the compose file, `docker pull` and the
bind-mounted folders were out of reach from the master: upgrading the published image meant an
interactive ssh session, which this master does not have (no stored host key, the login lives in the
secret store). `--on-host` runs the command on the host with the same login, and `--sudo` feeds that
login's password to sudo.
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

    def fake_run(client, command, *, sudo_password=None, check=True, quiet=False):
        sent["command"] = command
        sent["sudo_password"] = sudo_password
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
    assert remote["sudo_password"] is None


def test_on_host_runs_one_argument_as_a_shell_line(remote):
    _run(command=["--", "cd /opt/dbabrain && docker compose ps"], on_host=True)

    assert remote["command"] == "cd /opt/dbabrain && docker compose ps"
    assert remote["closed"] is True


def test_on_host_with_sudo_feeds_the_login_password_to_sudo(remote):
    _run(command=["--", "cp a b"], on_host=True, sudo=True)

    assert remote["command"] == "sudo -S -p '' sh -c 'cp a b'"
    assert remote["sudo_password"] == "pw"


def test_sudo_without_on_host_never_sends_the_password(remote):
    _run(sudo=True)

    assert remote["command"].startswith("docker exec ")
    assert remote["sudo_password"] is None
