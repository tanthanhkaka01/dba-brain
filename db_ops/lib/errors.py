"""The kinds of error db_ops raises, named - so a caller decides by the kind, never by the sentence.

An outside review (2026-10-02, audit ``20261002_audit_typed_requests_and_errors.md``) found errors
"raised as RuntimeError with a string". Measured, most modules already raise a class of their own -
about 114 of them - but those classes share nothing: a caller that must tell *the host did not
answer* from *the SQL failed* from *the request was wrong* still had only the message to match, and
messages are written for people and change. Each module class keeps its name; it gains one of these
bases, which carries the **kind**, and each base is still the built-in it replaces, so every
existing ``except RuntimeError`` / ``except ValueError`` catches what it caught before.

The kinds are what a ``common.cli`` answer carries as ``error_kind`` (``lib.response.fail``), and
what ``lib.common_cli.CommonCliError.kind`` reads back on the app's side of ``transport``.
"""

from __future__ import annotations

#: The request is wrong: a field missing, of the wrong kind, out of range, or not one the command reads.
KIND_REQUEST = "request"
#: A state, not a failure (rules R23): nothing to do because nothing is set up yet.
KIND_NOT_CONFIGURED = "not_configured"
#: A safety rule declined: a confirmation, a space check, the identifier scan, a level.
KIND_REFUSED = "refused"
#: The host or the engine could not be reached: connect, login, name resolution, a port, a timeout
#: before any work began.
KIND_UNREACHABLE = "unreachable"
#: It ran, and the work failed: a SQL error, a remote command's non-zero exit, a restore that stopped.
KIND_FAILED = "failed"
#: A configuration file is invalid.
KIND_CONFIG = "config"
#: Anything else - a defect.
KIND_INTERNAL = "internal"

KINDS = (KIND_REQUEST, KIND_NOT_CONFIGURED, KIND_REFUSED, KIND_UNREACHABLE, KIND_FAILED, KIND_CONFIG, KIND_INTERNAL)


class DbOpsError(Exception):
    """The base every named db_ops error shares. ``kind`` is one of :data:`KINDS`."""

    kind = KIND_INTERNAL


class RequestError(DbOpsError, ValueError):
    kind = KIND_REQUEST


class ConfigError(DbOpsError, ValueError):
    kind = KIND_CONFIG


class NotConfigured(DbOpsError, RuntimeError):
    kind = KIND_NOT_CONFIGURED


class Refused(DbOpsError, RuntimeError):
    kind = KIND_REFUSED


class Unreachable(DbOpsError, RuntimeError):
    kind = KIND_UNREACHABLE


class OperationFailed(DbOpsError, RuntimeError):
    kind = KIND_FAILED


class InvalidConfig(DbOpsError, RuntimeError):
    """A configuration fault raised where a bare ``RuntimeError`` was raised before.

    :class:`ConfigError` is a ``ValueError``; a site that raised ``RuntimeError`` keeps that ancestry,
    or every ``except RuntimeError`` written for it would stop catching it.
    """

    kind = KIND_CONFIG


class InvalidRequest(DbOpsError, RuntimeError):
    """A request fault raised where a bare ``RuntimeError`` was raised before - see :class:`InvalidConfig`."""

    kind = KIND_REQUEST


def kind_of(exc: BaseException) -> str:
    """The kind of ``exc``: its own when it is a :class:`DbOpsError`, else what a built-in means.

    A socket that would not connect and a timeout are *unreachable* whoever raised them - the
    drivers raise the built-ins, not ours. Everything else that was not named is ``internal``:
    guessing ``request`` from a bare ``ValueError`` would tell the caller to fix a request that may
    be perfectly good.
    """
    if isinstance(exc, DbOpsError):
        return str(exc.kind)
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return KIND_UNREACHABLE
    return KIND_INTERNAL
