"""A remote command is shown on the master's console - and must never show the passphrase in it.

`worker-run` echoes each line it sends (`[remote] $ ...`) and names it again when it fails. On
2026-10-05 the upgrade procedure's own `restore-add @... --key-base64 <key>` came back verbatim, the
base64 key in clear on the console and in any log of that session (0.27.0 item 1.92). The same lines
carry the key a second way, `-e DB_OPS_SECRET_KEY="$(echo <key> | base64 -d)"`, inside quotes.

What is shown is redacted; what is run is not - the remote side still needs the value.
"""

from __future__ import annotations

import pytest

from db_ops.control import _support
from db_ops.lib.remote_host import RemoteError, RemoteRun
from db_ops.lib.secret_text import redact_key_arguments

# Built, not written: a key-shaped literal here is what the secret scan exists to catch, and a
# test placeholder that trips it trains people to allowlist (release_process.md §2.1).
KEY = "-".join(("made", "up", "passphrase", "of", "this", "test"))


@pytest.mark.parametrize("line", [
    f"python -m x restore-add @req.json --key-base64 {KEY}",
    f"python -m x restore-add @req.json --key_base64 {KEY}",
    f"python -m x daemon --key-base64={KEY}",
    f"python -m x daemon --key {KEY}",
    f'python -m x daemon --key "{KEY} with spaces"',
    f"docker compose run -e DB_OPS_SECRET_KEY=\"$(echo {KEY} | base64 -d)\" dbabrain check",
    f"env DB_OPS_KEY_BASE64={KEY} python -m x check",
    f"env DB_OPS_SECRET_KEY='{KEY}' python -m x check",
])
def test_every_way_a_line_carries_the_passphrase_is_hidden(line):
    shown = redact_key_arguments(line)

    assert KEY not in shown
    assert "***" in shown


def test_a_variable_named_without_a_value_leaves_the_next_word_alone():
    """`-e DB_OPS_SECRET_KEY` passes the caller's value without naming it; `dbabrain` is the
    service, and hiding it would make the line unreadable for no gain."""
    line = "docker compose run -e DB_OPS_SECRET_KEY dbabrain check"

    assert redact_key_arguments(line) == line


def test_a_flag_that_only_begins_like_the_key_flag_is_not_touched():
    line = "ssh-add --key-file /home/dev/.ssh/id_ed25519 --keyring login"

    assert redact_key_arguments(line) == line


class _Host:
    def __init__(self, run=None, error=None):
        self.sent = []
        self._run, self._error = run, error

    def run(self, command, *, sudo=False):
        self.sent.append(command)
        if self._error:
            raise self._error
        return self._run


def test_the_echo_hides_the_passphrase_and_the_host_still_receives_it(capsys):
    host = _Host(run=RemoteRun(exit_code=0, stdout="ok\n", stderr=""))
    command = f"docker compose run dbabrain restore-add @r.json --key-base64 {KEY}"

    _support.ssh_run(host, command)

    shown = capsys.readouterr().out
    assert KEY not in shown and "--key-base64 ***" in shown
    assert host.sent == [command], "what is run is the line itself"


def test_a_failed_command_is_named_without_its_passphrase():
    host = _Host(run=RemoteRun(exit_code=3, stdout="", stderr="boom"))

    with pytest.raises(SystemExit) as caught:
        _support.ssh_run(host, f"daemon --key-base64 {KEY}", quiet=True)

    assert KEY not in str(caught.value)
    assert "exit 3" in str(caught.value)


def test_a_host_error_quoting_the_command_is_hidden_too():
    host = _Host(error=RemoteError(f"could not run: daemon --key-base64 {KEY}"))

    with pytest.raises(SystemExit) as caught:
        _support.ssh_run(host, f"daemon --key-base64 {KEY}", quiet=True)

    assert KEY not in str(caught.value)
