"""What can go wrong reaching a host over SSH, as four names.

Split out of ``common/ssh.py`` on 2026-08-15. Opening a paramiko client is an operation and
stayed there; the *vocabulary* had to come here because an app that only wants to say "and if the
SSH part fails, report it like this" was importing the whole transport to name an exception —
``sre/cli.py`` imported ``common.ssh`` for exactly one word.

**The three subclasses exist because they are three different next actions.** ``SshAuthError``
means the host answered and the credential is wrong (fix the credential); ``SshConnectError``
means nothing answered (fix the network, or the host is down); ``SshTimeoutError`` narrows that to
"it was still trying" (raise the timeout, or the host is overloaded). They are classified where
the paramiko exception type is still in hand, because once it is a string nobody can tell them
apart without pattern-matching a message — which is what this replaced.
"""

from __future__ import annotations

__all__ = ["SshAuthError", "SshConnectError", "SshError", "SshTimeoutError"]


class SshError(RuntimeError):
    """SSH-level failure: bad auth inputs, missing key, or connect failure."""


class SshAuthError(SshError):
    """The host answered and rejected the credentials."""


class SshConnectError(SshError):
    """The host could not be reached at all (port closed, no route, service down)."""


class SshTimeoutError(SshConnectError):
    """The connect attempt exceeded its timeout."""
