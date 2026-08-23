"""When SSH sends no credential at all, the error says so instead of blaming the server.

`auth_type` defaults to `key` for SSH. A `cmd_access` block that names a stored password but no
`auth_type` therefore offers paramiko nothing: no key file, and the password is never read. What
comes back is

    No authentication methods available

which reads as a server-side refusal — the operator goes and checks sshd config, the account, the
password, all of which are fine. The setting that is wrong is not mentioned, and neither is the
one-line fix.

The distinction the classifier can make, and the message could not: *the credential was rejected*
is a different problem from *there was no credential*.
"""
from __future__ import annotations

import pytest

paramiko = pytest.importorskip("paramiko")

from db_ops.common.ssh import SshAuthError, _connect_error


def _classify(*, offered_password: bool, offered_key: bool) -> SshAuthError:
    return _connect_error(
        paramiko,
        paramiko.AuthenticationException("No authentication methods available"),
        user="dbaops", host="10.0.0.9", port=22, timeout=10,
        offered_password=offered_password, offered_key=offered_key,
    )


def test_sending_nothing_names_auth_type_and_the_fix():
    message = str(_classify(offered_password=False, offered_key=False))
    assert "no credential was sent" in message
    assert "auth_type" in message
    assert '"password"' in message, "the fix is the value to set, so the message spells it"
    assert "key_file" in message, "key auth is the other way out, and is the default"


def test_a_rejected_password_is_still_a_rejected_password():
    message = str(_classify(offered_password=True, offered_key=False))
    assert "no credential was sent" not in message, (
        "a wrong password is not a configuration gap; saying so would send the reader to the "
        "wrong file")
    assert "authentication failed" in message


def test_both_are_still_authentication_errors():
    for offered in ((False, False), (True, False)):
        assert isinstance(_classify(offered_password=offered[0], offered_key=offered[1]),
                          SshAuthError)
