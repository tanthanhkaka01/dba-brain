"""No code reads a ``common.cli`` answer under a key that answer does not carry.

0.22.0 renamed answer keys (``engine``, ``database`` -> ``database_name``); the readers kept the old
names, and **every script-driven restore failed after its first step**, leaving the database it had
just restored RESTORING - *restore-full failed: 'engine'*. ``list-schemas`` answered every success
with ``KeyError: 'database'`` for three releases. R16's answer runs catch a key an answer carries
that the reference does not describe; nothing caught a key a *reader* expects that the answer no
longer carries.

Since 2026-10-02 the transport is typed per command (``transport/common_cli.pyi``, rendered from the
reference), so ``common_cli.run("restore-full", ...)`` *is* a ``RestoreStepAnswer``, and mypy names
a key read from it that the type does not have. This runs mypy over the package and fails on that
error alone; mypy's other findings in an untyped codebase are not this test's business. Skipped
where mypy is not installed - CI installs it (``ci.yml``, the ``dev`` extra).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
#: The two mypy codes that mean "a key the TypedDict does not have".
KEY_ERRORS = ("[typeddict-item]", "[typeddict-unknown-key]")


def test_no_answer_is_read_under_a_key_it_does_not_carry(tmp_path):
    pytest.importorskip("mypy")
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "db_ops", "--ignore-missing-imports", "--check-untyped-defs",
         "--no-error-summary", "--cache-dir", str(tmp_path / "mypy_cache")],
        cwd=REPO, capture_output=True, text=True, timeout=600)
    misread = [line for line in result.stdout.splitlines() if line.endswith(KEY_ERRORS)]
    assert not misread, "an answer read under a key it does not carry:\n" + "\n".join(misread)


def test_the_check_would_name_the_0_22_0_defect(tmp_path):
    """The check is only worth having if it fires on the defect it is for."""
    pytest.importorskip("mypy")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from db_ops.transport import common_cli\n\n\n"
        "def summary() -> str:\n"
        "    return str(common_cli.run('restore-full', {})['engine'])\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "mypy", str(probe), "--ignore-missing-imports", "--no-error-summary",
         "--cache-dir", str(tmp_path / "mypy_cache")],
        cwd=REPO, capture_output=True, text=True, timeout=600,
        env={**__import__("os").environ, "MYPYPATH": str(REPO)})
    assert 'TypedDict "RestoreStepAnswer" has no key "engine"' in result.stdout
