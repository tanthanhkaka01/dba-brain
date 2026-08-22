"""Backing up a Windows host that has no OpenSSH server.

``resolve_ssh_target`` refused anything but ``cmd_access.method = ssh``, which read as a statement
about the transport and was really a statement about what nobody had needed yet:
``hostcmd.run_script`` has dispatched WinRM the whole time, delegating to the same
:mod:`db_ops.common.remote_exec` the metrics collectors use against these hosts every cycle. The
cost of the check was that 192.0.2.248 — a Windows SQL Server reached over WinRM, like most of
this estate — could not be backed up at all. It failed at config resolution with
``needs cmd_access.method=ssh, got winrm``, before anything was attempted, and the only workarounds
were installing an OpenSSH server to satisfy a check rather than a limitation.

Half of this file is about the change working. The other half is about it changing nothing else:
fourteen backup entries were already running over SSH when this was made, and the resolver is the
one piece every one of them goes through.
"""

import pytest

from db_ops.backup_restore.backup import (
    SUPPORTED_BACKUP_ACCESS,
    _default_access_port,
    resolve_ssh_target,
)


def _instances(monkeypatch, *records):
    from db_ops.common import data_sources

    monkeypatch.setattr(data_sources, "load_db_instances", lambda *_a, **_k: list(records))
    monkeypatch.setattr(
        data_sources, "load_remote_credentials",
        lambda *_a, **_k: [{"credentials": [
            {"credential_name": "cred", "username": "someone", "password_ref": "SOME_REF"}]}],
    )


def _record(**overrides):
    record = {
        "server_id": "SRV",
        "ip": "10.0.0.9",
        "platform": "windows",
        "container_name": "",
        "cmd_access": {"enabled": True, "method": "winrm", "host": "10.0.0.9",
                       "credential_name": "cred"},
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------------------
# WinRM
# --------------------------------------------------------------------------------------

def test_a_winrm_windows_host_resolves_instead_of_being_refused(monkeypatch):
    _instances(monkeypatch, _record())

    target = resolve_ssh_target("SRV", label="job")

    assert target.access == "winrm"
    assert target.username == "someone" and target.password_ref == "SOME_REF"


def test_a_winrm_target_with_no_stated_port_does_not_get_22(monkeypatch):
    """Defaulting to 22 for every method was harmless while only SSH was allowed. A WinRM target
    would now speak WinRM at an SSH port and report the host unreachable — the least informative
    way to be wrong."""
    _instances(monkeypatch, _record())

    assert resolve_ssh_target("SRV", label="job").port == 5985


def test_tls_moves_the_default_winrm_port(monkeypatch):
    _instances(monkeypatch, _record(cmd_access={
        "enabled": True, "method": "winrm", "host": "10.0.0.9",
        "credential_name": "cred", "ssl": True}))

    target = resolve_ssh_target("SRV", label="job")

    assert target.ssl is True and target.port == 5986


def test_an_explicit_port_still_wins(monkeypatch):
    _instances(monkeypatch, _record(cmd_access={
        "enabled": True, "method": "winrm", "host": "10.0.0.9",
        "credential_name": "cred", "port": 5999}))

    assert resolve_ssh_target("SRV", label="job").port == 5999


def test_a_key_file_is_dropped_on_a_winrm_target(monkeypatch):
    """A key is an SSH idea. Carried through, it would make the spec builder suppress the password
    the WinRM transport actually needs — see spec_builder._ssh_password."""
    _instances(monkeypatch, _record(cmd_access={
        "enabled": True, "method": "winrm", "host": "10.0.0.9",
        "credential_name": "cred", "key_file": "left-over.key"}))

    assert resolve_ssh_target("SRV", label="job").key_file is None


def test_the_resolved_target_reaches_hostcmd_as_a_winrm_host(monkeypatch):
    """The point of the whole change: what the resolver decided has to survive into the object the
    transport dispatches on."""
    from db_ops.common.hostcmd import parse_host

    _instances(monkeypatch, _record())
    target = resolve_ssh_target("SRV", label="job")
    host = parse_host({"runtime": "windows", "host": target.host, "port": target.port,
                       "username": target.username, "password": "x",
                       "access": target.access, "ssl": target.ssl})

    assert host.is_winrm is True


# --------------------------------------------------------------------------------------
# ...and nothing else moves
# --------------------------------------------------------------------------------------

def test_an_ssh_target_resolves_exactly_as_before(monkeypatch):
    _instances(monkeypatch, _record(platform="linux", container_name="pg_lab", cmd_access={
        "enabled": True, "method": "ssh", "host": "10.0.0.9", "credential_name": "cred"}))

    target = resolve_ssh_target("SRV", label="job")

    assert target.access == "ssh" and target.port == 22 and target.ssl is False


def test_an_ssh_key_file_is_still_carried(monkeypatch):
    """Seven of the entries running when this changed are key-auth CLOUD jobs."""
    _instances(monkeypatch, _record(platform="linux", container_name="ora", cmd_access={
        "enabled": True, "method": "ssh", "host": "10.0.0.9", "credential_name": "cred",
        "key_file": "oracle-cloud.key"}))

    assert resolve_ssh_target("SRV", label="job").key_file == "oracle-cloud.key"


def test_local_is_still_refused(monkeypatch):
    """`local` would back up the db_ops container itself, under the target's name — the same
    mistake remote_exec.assert_local_host exists to stop for metrics."""
    _instances(monkeypatch, _record(cmd_access={
        "enabled": True, "method": "local", "host": "10.0.0.9", "credential_name": "cred"}))

    with pytest.raises(ValueError, match="local"):
        resolve_ssh_target("SRV", label="job")


def test_a_missing_method_is_still_refused(monkeypatch):
    _instances(monkeypatch, _record(cmd_access={
        "enabled": True, "host": "10.0.0.9", "credential_name": "cred"}))

    with pytest.raises(ValueError, match="<missing>"):
        resolve_ssh_target("SRV", label="job")


def test_disabled_cmd_access_is_still_refused(monkeypatch):
    _instances(monkeypatch, _record(cmd_access={
        "enabled": False, "method": "winrm", "host": "10.0.0.9", "credential_name": "cred"}))

    with pytest.raises(ValueError, match="not enabled"):
        resolve_ssh_target("SRV", label="job")


def test_only_the_two_transports_that_are_implemented_are_accepted():
    assert SUPPORTED_BACKUP_ACCESS == frozenset({"ssh", "winrm"})
    assert _default_access_port("ssh", ssl=False) == 22
    assert _default_access_port("winrm", ssl=False) == 5985
    assert _default_access_port("winrm", ssl=True) == 5986
