"""What an install without the WinRM extra is told when WinRM does not work.

`remote_exec` has two WinRM backends: `pypsrp` when it is installed, and otherwise a local
`powershell -Command Invoke-Command` wrapper. They are not equivalent, and until 2026-09-05
nothing said so anywhere. On one estate an install made without `[winrm]` drove every WinRM call
through the fallback, which could not authenticate to a WORKGROUP host and returned the host's own
words — `0x8009030e ... A specified logon session does not exist ... add the server name to the
TrustedHosts list`. Every one of those sentences points at the host. The host was fine: the same
command, the same credential and the same target answered `exit 0` the moment `pypsrp` was
installed, while that instance's backup had been failing for two days and its OS metrics had been
recording `WARNING: Command exited with code 1` every cycle.

So the hint is attached where the wrong conclusion gets drawn, and only there.
"""

from db_ops.common.remote_exec import RemoteResult, _name_the_missing_backend


def _result(exit_code=1, stderr="", stdout=""):
    return RemoteResult(method="winrm", host="192.0.2.10", command="Invoke-Command",
                        exit_code=exit_code, stdout=stdout, stderr=stderr, duration_seconds=0.4)


AUTH_FAILURE = (
    '<S S="Error">[192.0.2.10] Connecting to remote server 192.0.2.10 failed with the following '
    'error message : WinRM cannot process the request. The following error with error code '
    '0x8009030e occurred while using Negotiate authentication: A specified logon session does '
    'not exist.</S>'
)


def test_an_authentication_failure_names_the_backend_that_was_missing():
    hinted = _name_the_missing_backend(_result(stderr=AUTH_FAILURE), host="192.0.2.10")

    assert "pypsrp` is not installed" in hinted.stderr
    # Named through db_ops.lib.packaging, so it is right under either distribution name.
    assert "[winrm]" in hinted.stderr and "pip install" in hinted.stderr
    assert AUTH_FAILURE in hinted.stderr, "the host's own words are kept, not replaced"


def test_the_hint_says_which_host_it_is_about():
    """A backup run touches several hosts; a hint that names none of them sends the reader
    looking through logs to find out which call it belongs to."""
    hinted = _name_the_missing_backend(_result(stderr=AUTH_FAILURE), host="192.0.2.10")

    assert "WinRM to 192.0.2.10" in hinted.stderr


def test_a_command_that_worked_is_left_alone():
    """The fallback is not broken; it is only weaker. A working call gets no lecture."""
    result = _result(exit_code=0, stdout="Server-TAP")

    assert _name_the_missing_backend(result, host="192.0.2.10") is result


def test_a_failure_that_is_not_about_authentication_is_left_alone():
    """`exit 1` from the script itself is the script's answer. Blaming the transport for it would
    teach the reader to ignore the hint on the day it is true."""
    result = _result(stderr="BACKUP DATABASE is terminating abnormally. Msg 3013")

    assert _name_the_missing_backend(result, host="192.0.2.10") is result


def test_the_markers_are_matched_case_insensitively():
    """Windows spells these several ways across versions and locales; the error text is not a
    contract."""
    result = _result(stderr="WinRM cannot Process The Request")

    assert "pypsrp" in _name_the_missing_backend(result, host="192.0.2.10").stderr
