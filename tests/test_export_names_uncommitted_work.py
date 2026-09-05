"""The export copies the working tree, so it can publish work nobody has finished.

That is not a hypothetical. On 2026-08-24 an export shipped five modules and a test that another
session was still writing — untracked, never committed, open in an editor at that moment. They went
to a public repository attached to an unrelated change. CI caught them on a duplicate-definition
guard, which is the sort of thing half-finished code trips; nothing else would have.

Copying the working tree is deliberate and worth keeping: it is what lets an operator export a
change before committing it. What was missing is that the export said nothing about it. A file
nobody has committed is a file nobody has decided is done, and the export is the last moment before
that decision becomes public and permanent.

So it is reported, loudly, and it does not refuse: shipping a work in progress on purpose is the
operator's call. The report is scoped to files that would actually *ship* — `data/` and `audits/`
churn constantly and never cross, and a warning that fires every time is one nobody reads.
"""
from __future__ import annotations

import subprocess

from db_ops.control import export_public


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _repo(tmp_path):
    """A miniature repository shaped like this one, with one committed public file."""
    root = tmp_path
    (root / "db_ops").mkdir()
    (root / "db_ops" / "__init__.py").write_text("__version__ = '0'\n", encoding="utf-8")
    (root / "README.md").write_text("# probe\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "probe@example.invalid")
    _git(root, "config", "user.name", "probe")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def test_a_committed_tree_reports_nothing(tmp_path):
    plan = export_public.build_plan(_repo(tmp_path))
    assert plan.uncommitted == [], "a clean checkout must not nag"


def test_an_untracked_module_that_would_ship_is_named(tmp_path):
    root = _repo(tmp_path)
    (root / "db_ops" / "half_written.py").write_text("def f():\n    ...\n", encoding="utf-8")

    plan = export_public.build_plan(root)
    assert "db_ops/half_written.py" in plan.uncommitted, (
        "an untracked module inside the package is exactly what got published by mistake")


def test_a_modified_but_uncommitted_file_is_named(tmp_path):
    root = _repo(tmp_path)
    (root / "README.md").write_text("# probe, edited\n", encoding="utf-8")

    plan = export_public.build_plan(root)
    assert "README.md" in plan.uncommitted


def test_only_files_that_would_ship_are_reported(tmp_path):
    """`data/` and `audits/` are always dirty and never cross; warning about them trains people to ignore the warning."""
    root = _repo(tmp_path)
    (root / "audits").mkdir()
    (root / "audits" / "20260824_audit_probe.md").write_text("# probe\n", encoding="utf-8")

    plan = export_public.build_plan(root)
    assert not any(entry.startswith("audits/") for entry in plan.uncommitted)


def test_not_a_repository_is_not_a_clean_bill_of_health(tmp_path, monkeypatch):
    """Where git cannot answer, the export reports nothing rather than claiming everything is fine.

    The ceiling is what makes this case real, and it was missing. `pytest.ini` sets
    `--basetemp=.pytest_tmp`, so `tmp_path` is *inside* this repository: every other test here
    `git init`s its own tree and is shadowed by that nested `.git`, but this one deliberately does
    not, so `git status` answered for **db_ops itself** and returned db_ops' own dirty files. It
    passed anyway, because the plan for this two-file tree intersects that list only at
    `db_ops/__init__.py` — and it failed the first time somebody edited that file and ran the
    suite, which is a release bumping `__version__`. A test that depends on a specific file of the
    surrounding repository being committed is measuring the wrong tree.

    `GIT_CEILING_DIRECTORIES` stops the upward search before it leaves the temporary root, so there
    is no repository to find and the subprocess is the one being tested rather than this checkout.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    (tmp_path / "db_ops").mkdir()
    (tmp_path / "db_ops" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "db_ops" / "half_written.py").write_text("def f():\n    ...\n", encoding="utf-8")

    plan = export_public.build_plan(tmp_path)

    shipped = {relative.as_posix() for _, relative in plan.files}
    assert "db_ops/half_written.py" in shipped, "the file is in the plan..."
    assert plan.uncommitted == [], "...and still not reported, because git could not answer"
