"""One command, twelve apps — and the dispatcher stays a lookup, not a second parser.

Every app has always been reachable as `python -m db_ops.<app>.cli`, which is what the daemon
runs and what the documentation shows. That is fine for a checkout and useless as a first
impression: an installed toolkit that answers nothing when you type its name has not really been
installed.

So the root CLI dispatches. Two properties hold it in shape:

**It routes and does not interpret.** The moment it starts reading an app's arguments there are
two parsers for one command, and they disagree the first time either changes. `main` looks the
first word up in a table and hands the rest over untouched.

**It imports the app it was asked for, and no other.** Importing all twelve to print a help
message would make `--help` depend on having an ODBC driver installed — on a slim install the
help would crash while explaining how to use the tool.
"""

from __future__ import annotations

import sys

import pytest

from db_ops import cli


def test_every_app_directory_is_reachable_by_name() -> None:
    """A component nobody can invoke is a component nobody finds."""
    from pathlib import Path

    packages = {
        path.name
        for path in (Path(__file__).resolve().parents[1] / "db_ops").iterdir()
        if path.is_dir() and not path.name.startswith("__") and (path / "__init__.py").exists()
    }
    # `lib` is imported, never run — it has no CLI by rule (ORD 14), and `logging_ops` is a
    # library too. Everything else answers to a name.
    expected = {name for name in packages if name not in {"lib", "logging_ops"}}
    reachable = {module.split(".")[1] for module in cli.APPS.values()}

    assert expected <= reachable, f"not reachable by name: {sorted(expected - reachable)}"


def test_the_table_points_at_modules_that_exist_and_can_run() -> None:
    """Every app this build actually has must be runnable by the name the table gives it.

    Asked of `installed_apps()` rather than `APPS`: the public distribution ships seven of the
    fourteen, and importing an entry it does not have raises `ModuleNotFoundError` — which says the
    install is broken when it is not. `APPS` stays the full table on purpose; it is the product's
    map, and the dispatcher already answers a missing name with a sentence.
    """
    import importlib

    present = cli.installed_apps()
    assert present, "no app is reachable at all, which cannot be right in any build"
    for name, module_path in present.items():
        module = importlib.import_module(module_path)
        assert callable(getattr(module, "main", None)), f"{name} -> {module_path} has no main()"


def test_help_needs_no_app_and_no_driver(capsys) -> None:
    """The failure this prevents: `--help` importing twelve apps and dying on a missing driver."""
    assert cli.main(["--help"]) == 0

    printed = capsys.readouterr().out
    assert "usage: db-ops <app>" in printed
    # The apps this build has, not a fixed three: the help text lists what is installed, so naming
    # `backup-restore` here asserted the private tree's shape rather than the behaviour under test.
    for name in cli.installed_apps():
        assert name in printed


def test_no_arguments_is_the_same_as_help(capsys) -> None:
    assert cli.main([]) == 0
    assert "usage: db-ops <app>" in capsys.readouterr().out


def test_the_version_is_the_package_version(capsys) -> None:
    from db_ops import __version__

    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_an_unknown_app_says_so_and_lists_the_real_ones(capsys) -> None:
    assert cli.main(["nosuchapp"]) == 2

    printed = capsys.readouterr().err
    assert "unknown app" in printed and "nosuchapp" in printed
    assert "metrics" in printed, "a rejection that does not say what is valid teaches nothing"


def test_underscores_and_hyphens_both_work() -> None:
    """The modules use underscores and the help prints hyphens, so a reader may type either."""
    assert "backup-restore" in cli.APPS
    assert cli.APPS["backup-restore"] == "db_ops.backup_restore.cli"


def test_the_app_receives_its_arguments_untouched(monkeypatch) -> None:
    """Routing, not parsing: whatever follows the app name is the app's business."""
    seen: list[list[str]] = []

    class _Fake:
        @staticmethod
        def main(argv):
            seen.append(list(argv))
            return 7

    monkeypatch.setattr("importlib.import_module", lambda path: _Fake)

    assert cli.main(["metrics", "collect", "--dry-run", "--config", "x.json"]) == 7
    assert seen == [["collect", "--dry-run", "--config", "x.json"]]


def test_the_console_script_reads_the_real_arguments(monkeypatch, capsys) -> None:
    """setuptools calls `main()` with nothing, so the default has to be `sys.argv`."""
    monkeypatch.setattr(sys, "argv", ["db-ops", "--version"])

    assert cli.main() == 0
    assert capsys.readouterr().out.strip()


def test_the_usage_text_is_ascii() -> None:
    """It prints to whatever console the operator has, and the Windows one is cp1252."""
    usage = cli._usage()

    usage.encode("ascii")  # raises if a dash or a quote sneaks back in


@pytest.mark.parametrize("word", ["-h", "help"])
def test_the_other_spellings_of_help_work(word, capsys) -> None:
    assert cli.main([word]) == 0
    assert "usage: db-ops <app>" in capsys.readouterr().out
