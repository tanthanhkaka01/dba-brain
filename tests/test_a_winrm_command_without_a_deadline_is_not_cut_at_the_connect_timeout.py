"""Over WinRM, "no deadline" means no deadline - as it does over SSH and locally.

The WinRM session turned a missing command timeout into the *connect* timeout, 30 s by default
(review 0.25.0, B5.3). Every WinRM caller that passed none - a SQL Server patch step, a host
operation - was cut off half a minute in, and `sqlcmd_run` carried its own "very large number" to get
round it. The connect timeout now bounds one HTTP round trip only; the command runs until it ends,
or until the deadline its caller actually set.
"""

from __future__ import annotations

import sys
import time
import types

import pytest

from db_ops.common import remote_exec as rx

CONNECT_SECONDS = 1
COMMAND_SECONDS = 2.5


@pytest.fixture
def slow_pypsrp(monkeypatch):
    captured: dict = {}

    class Client:
        def __init__(self, host, **kwargs):
            captured.update(kwargs)

        def execute_ps(self, _script):
            time.sleep(COMMAND_SECONDS)
            return ("done", "", 0)

    module = types.ModuleType("pypsrp.client")
    module.Client = Client
    package = types.ModuleType("pypsrp")
    package.client = module
    monkeypatch.setitem(sys.modules, "pypsrp", package)
    monkeypatch.setitem(sys.modules, "pypsrp.client", module)
    return captured


ACCESS = {"method": "winrm", "host": "192.0.2.108", "port": 5985, "timeout_seconds": CONNECT_SECONDS}
CREDENTIAL = {"username": "svc", "password": "pw"}


def test_a_command_with_no_deadline_outlives_the_connect_timeout(slow_pypsrp):
    result = rx.run_script(ACCESS, "Start-LongThing", credential=CREDENTIAL)

    assert result.ok and result.stdout == "done"
    assert slow_pypsrp["connection_timeout"] == CONNECT_SECONDS


def test_a_deadline_the_caller_sets_still_cuts_the_command(slow_pypsrp):
    with pytest.raises(rx.RemoteCommandTimeoutError, match="timed out after 1 seconds"):
        rx.run_script(ACCESS, "Start-LongThing", credential=CREDENTIAL, timeout_seconds=1)


def test_sqlcmd_no_longer_needs_its_own_large_number():
    from db_ops.common import sqlcmd_run

    assert not hasattr(sqlcmd_run, "_WINRM_UNBOUNDED_SECONDS")
