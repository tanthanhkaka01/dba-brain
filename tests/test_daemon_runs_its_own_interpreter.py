"""The daemon's children must run the Python the daemon is running, not whatever `PATH` says.

Every command in `data/app_commands.example.json` begins `python -m db_ops...`. `python` resolves
through `PATH`, and `PATH` is not where the toolkit is installed: after `pip install dbabrain` into
a virtualenv — the documented way — `db-ops daemon` starts from the venv and its children get a
system Python without the package, or another project's venv.

The symptom is the worst kind, and it is why this is worth a test rather than a comment. Every
command the reader ran by hand works. The moment the daemon runs *the same commands*, all of them
fail with `ModuleNotFoundError: No module named 'db_ops'`, once a minute, in a child process whose
output nobody is watching. Measured in a clean install on 2026-08-23: three scheduled commands,
three failures, three manual runs that succeeded.

A command naming a specific interpreter is left alone — that is somebody being deliberate.
"""
from __future__ import annotations

import os
import shlex
import sys

import pytest

from db_ops.jobs.daemon import use_this_interpreter


@pytest.mark.parametrize("word", ["python", "python3", "PYTHON"])
def test_a_bare_python_becomes_this_interpreter(word: str):
    rewritten = use_this_interpreter(f"{word} -m db_ops.metrics.cli --config config.json collect")
    assert sys.executable in rewritten
    assert rewritten.endswith("-m db_ops.metrics.cli --config config.json collect")


def test_the_rest_of_the_command_is_untouched():
    original = "python -m db_ops.telegram.cli --config config.json send-queue --limit 5"
    assert use_this_interpreter(original).endswith(original[len("python"):])


@pytest.mark.parametrize("command", [
    "/usr/bin/python3.11 -m db_ops.x",
    "C:/Python312/python.exe -m db_ops.x",
    "db-ops metrics collect",
    "bash ./script.sh",
    "",
])
def test_anything_deliberate_is_left_exactly_as_written(command: str):
    assert use_this_interpreter(command) == command


def test_an_unbalanced_quote_is_not_ours_to_repair():
    """Better to let the shell report its own syntax error than to guess at the intent."""
    broken = 'python -m db_ops.x --note "unclosed'
    assert use_this_interpreter(broken) == broken


def test_leading_whitespace_survives():
    assert use_this_interpreter("  python -m db_ops.x").startswith("  ")


def test_the_quoting_matches_the_shell_that_will_parse_it():
    r"""`shell=True` is cmd.exe on Windows, and cmd does not treat single quotes as quoting.

    A path like `C:\Program Files\...\python.exe` quoted the POSIX way reaches the child with
    literal apostrophes around it.
    """
    rewritten = use_this_interpreter("python -m db_ops.x")
    interpreter = rewritten[: -len(" -m db_ops.x")]
    if os.name == "nt":
        assert interpreter == f'"{sys.executable}"'
    else:
        assert interpreter == shlex.quote(sys.executable)
