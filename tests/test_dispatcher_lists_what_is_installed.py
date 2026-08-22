"""`db-ops` must advertise only the apps this build actually has.

The public `v0.1.0` ships seven of the fourteen components, so the app table is a promise the
distribution cannot keep. Measured on 2026-08-22 rather than predicted: the first thin wheel listed
all twelve apps and could run five. A reader who types the line the help text just offered them
gets `ModuleNotFoundError: db_ops.sre.cli`, which reads as a broken install — and the next thing
they do is reinstall, which does not help.

Detected rather than hardcoded, so one code path is correct in both trees: a full checkout lists
twelve, the thin wheel lists five, and neither needs to know which it is.

Two details this had to learn, and the second cost a traceback in the exact place it was added to
prevent one:

- `find_spec` **locates** a module without executing it. That is what keeps the dispatch lazy —
  importing twelve apps to print a help message would make `--help` depend on having an ODBC
  driver, so a slim install would crash while explaining how to use the tool.
- `find_spec` **raises** `ModuleNotFoundError` when the *parent* package is the missing one, rather
  than returning `None`. So the obvious guard — `if find_spec(module) is None` — produces the very
  traceback it was written to replace, and only for the packages that are actually absent.
"""

from __future__ import annotations

import importlib.util

import pytest

from db_ops import cli


def test_this_checkout_has_every_app(capsys) -> None:
    """The full tree is the control case: nothing is filtered out here."""
    assert cli.installed_apps() == cli.APPS


def test_a_missing_app_is_not_listed(monkeypatch) -> None:
    """The thin distribution's case, forced: `sre` is absent, so it must not be offered."""
    monkeypatch.setattr(importlib.util, "find_spec", _absent({"db_ops.sre.cli"}))

    present = cli.installed_apps()

    assert "sre" not in present
    assert "metrics" in present, "removing one app must not remove the rest"


def test_the_help_text_lists_only_what_is_installed(monkeypatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec",
                        _absent({"db_ops.sre.cli", "db_ops.reports.cli"}))

    usage = cli._usage()

    assert "sre" not in usage and "reports" not in usage
    assert "metrics" in usage and "telegram" in usage


def test_running_a_missing_app_explains_instead_of_raising(monkeypatch, capsys) -> None:
    """A sentence, not a traceback — the difference decides what the reader does next."""
    monkeypatch.setattr(importlib.util, "find_spec", _absent({"db_ops.sre.cli"}))

    code = cli.main(["sre", "--help"])

    assert code == 2
    message = capsys.readouterr().err
    assert "not in this build" in message
    assert "Traceback" not in message
    assert "metrics" in message, "say what it does have, or the reader has to guess"


def test_a_parent_package_that_is_absent_does_not_escape_as_an_error(monkeypatch) -> None:
    """`find_spec` raises rather than returning None when the parent is missing.

    Pinned because the first version of this guard called `find_spec` bare and produced exactly the
    `ModuleNotFoundError` it existed to prevent — and only for the apps that were genuinely gone,
    so a full checkout could never have shown it.
    """
    def raising(name: str, *args, **kwargs):
        if name.startswith("db_ops.sre"):
            raise ModuleNotFoundError("No module named 'db_ops.sre'")
        return object()

    monkeypatch.setattr(importlib.util, "find_spec", raising)

    present = cli.installed_apps()

    assert "sre" not in present
    assert len(present) == len(cli.APPS) - 1


def test_the_usage_stays_ascii(monkeypatch) -> None:
    """It prints to whatever console the operator has, and the Windows one is cp1252."""
    cli._usage().encode("cp1252")


def _absent(missing: set[str]):
    """A `find_spec` that reports *missing* as absent the way Python actually does.

    Returning `None` is what happens when the module is missing but its parent exists; raising is
    what happens when the parent itself is gone. Both are exercised, because the thin distribution
    produces the second and the first version of this code only handled the first.
    """
    real = importlib.util.find_spec

    def fake(name: str, *args, **kwargs):
        if name in missing:
            return None
        return real(name, *args, **kwargs)

    return fake
