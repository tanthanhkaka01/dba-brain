"""A failure carries its kind from where it is raised to the app that called - never only a sentence.

The 2026-10-02 assessment (``audits/20261002_audit_typed_requests_and_errors.md``) found ~114 named
error classes sharing nothing, 191 bare ``RuntimeError`` raises, and an answer envelope whose only
statement about a failure was ``error``: a sentence written for a person. A caller that had to tell
*the host did not answer* from *the request was wrong* matched words. ``lib.errors`` names the
kinds; ``common.cli`` answers ``error_kind``; ``lib.common_cli`` reads it back onto
``CommonCliError.kind``. Every base is still the built-in it replaces, so no ``except`` changes.
"""

from __future__ import annotations

import json

import pytest

from db_ops.common import cli
from db_ops.lib import common_cli, errors, response


def test_each_kind_is_still_the_built_in_it_replaces():
    assert issubclass(errors.RequestError, ValueError) and issubclass(errors.ConfigError, ValueError)
    for cls in (errors.NotConfigured, errors.Refused, errors.Unreachable, errors.OperationFailed):
        assert issubclass(cls, RuntimeError), cls
    assert {cls.kind for cls in (errors.RequestError, errors.ConfigError, errors.NotConfigured,
                                 errors.Refused, errors.Unreachable, errors.OperationFailed)} \
        == set(errors.KINDS) - {errors.KIND_INTERNAL}


def test_a_driver_that_could_not_connect_is_unreachable_whoever_raised_it():
    assert errors.kind_of(ConnectionRefusedError()) == errors.KIND_UNREACHABLE
    assert errors.kind_of(TimeoutError()) == errors.KIND_UNREACHABLE
    assert errors.kind_of(errors.Refused("no")) == errors.KIND_REFUSED


def test_an_unnamed_error_is_internal_not_a_guess():
    """A bare ValueError is not necessarily the caller's request - saying so would send them to
    fix a request that may be perfectly good."""
    assert errors.kind_of(ValueError("x")) == errors.KIND_INTERNAL
    assert errors.kind_of(KeyError("x")) == errors.KIND_INTERNAL


def test_the_envelope_carries_the_kind_and_only_a_listed_one():
    assert response.ok("x")["error_kind"] is None
    assert response.fail("x", "boom")["error_kind"] == errors.KIND_FAILED
    assert response.fail("x", "boom", kind=errors.KIND_UNREACHABLE)["error_kind"] == errors.KIND_UNREACHABLE
    assert response.fail("x", "boom", kind="made-up")["error_kind"] == errors.KIND_INTERNAL


def test_common_cli_answers_an_exception_with_its_kind(monkeypatch, capsys):
    def raise_unreachable(argv):
        raise errors.Unreachable("host 192.0.2.7 did not answer on 1433")

    monkeypatch.setattr(cli, "_dispatch", raise_unreachable)
    code = cli.main(["run-sql", "{}"])
    answer = json.loads(capsys.readouterr().out)
    assert code == 1 and answer["success"] is False
    assert answer["error_kind"] == errors.KIND_UNREACHABLE


def test_a_request_that_is_not_an_object_is_the_envelope_with_kind_request(capsys):
    code = cli.main(["run-sql", "[1, 2]"])
    answer = json.loads(capsys.readouterr().out)
    assert code == 1
    assert answer["success"] is False and answer["error_kind"] == errors.KIND_REQUEST
    assert "must be a JSON object" in answer["error"]


def _answer(**envelope) -> str:
    return json.dumps(envelope)


def test_the_app_reads_the_kind_back_and_raises_with_it():
    answer = common_cli.read_answer("run-sql", returncode=1, stderr="", stdout=_answer(
        success=False, operation="run-sql", message="x", error="login failed",
        error_kind="unreachable", data={}, metrics={}))
    success, data, error = answer                     # still unpacks as three
    assert (success, error) == (False, "login failed") and answer.kind == "unreachable"
    with pytest.raises(common_cli.CommonCliError) as raised:
        common_cli.data_or_raise("run-sql", answer)
    assert raised.value.kind == "unreachable"
    assert isinstance(raised.value, RuntimeError), "every existing except RuntimeError still catches it"


def test_an_answer_from_before_the_kind_existed_is_failed():
    answer = common_cli.read_answer("x", returncode=1, stderr="", stdout=_answer(
        success=False, operation="x", message="", error="old", data={}, metrics={}))
    assert answer.kind == errors.KIND_FAILED


import ast  # noqa: E402 - the guards below read the source; the tests above do not
from pathlib import Path  # noqa: E402

DB_OPS = Path(__file__).resolve().parents[1] / "db_ops"

#: Exception classes that are deliberately not an error kind: the console's HTTP refusal is control
#: flow carrying a status code, and the daemon's stop signal is a BaseException so nothing catches it.
NOT_A_KIND = {("webhost/app.py", "Refused"), ("jobs/daemon.py", "_DaemonStopped")}


def _exception_classes():
    for path in DB_OPS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(base) for base in node.bases]
                yield path.relative_to(DB_OPS).as_posix(), node.name, bases


def test_every_exception_class_has_a_kind():
    """A class deriving straight from a built-in carries no kind, so its failure answers `internal`."""
    builtins = {"RuntimeError", "ValueError", "OSError", "Exception", "BaseException"}
    unkinded = [f"{rel}:{name}({', '.join(bases)})" for rel, name, bases in _exception_classes()
                if bases and set(bases) <= builtins and (rel, name) not in NOT_A_KIND
                and rel != "lib/errors.py"]
    assert not unkinded, ("give each a base from lib.errors (keeping its built-in): " + "; ".join(unkinded))


def test_no_bare_runtime_error_is_raised():
    """191 were, until 2026-10-02. A bare RuntimeError answers `internal` whatever it meant."""
    sites = [f"{path.relative_to(DB_OPS).as_posix()}:{number}"
             for path in DB_OPS.rglob("*.py")
             for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
             if "raise RuntimeError(" in line]
    assert not sites, "raise the kind it is (lib.errors) instead: " + ", ".join(sites)


def test_a_module_error_keeps_the_built_in_it_was():
    """Re-parenting must not add an ancestor: an `except ValueError` that did not catch it before
    must not start to, and the other way round."""
    from db_ops.backup_restore.registration import RegistrationError
    from db_ops.common.remote_exec import RemoteCommandTimeoutError, RemoteConnectError
    from db_ops.lib.remote_host import RemoteError
    from db_ops.lib.shared_objects import SharedObjectError

    assert issubclass(RegistrationError, RuntimeError) and not issubclass(RegistrationError, ValueError)
    assert RegistrationError.kind == errors.KIND_REQUEST
    assert issubclass(SharedObjectError, ValueError) and SharedObjectError.kind == errors.KIND_CONFIG
    assert issubclass(RemoteError, OSError) and not issubclass(RemoteError, RuntimeError)
    assert RemoteConnectError.kind == errors.KIND_UNREACHABLE
    assert RemoteCommandTimeoutError.kind == errors.KIND_FAILED, "ran, then ran out of time"


def test_no_answer_at_all_is_internal():
    with pytest.raises(common_cli.CommonCliError) as raised:
        common_cli.read_answer("x", returncode=2, stdout="Traceback ...", stderr="")
    assert raised.value.kind == errors.KIND_INTERNAL
