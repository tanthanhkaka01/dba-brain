"""The export decides what becomes public, and publication is the one thing that cannot be undone.

So this file is about refusals more than about copying. Every check below exists because the cost
of getting it wrong is a real hostname in a repository that a stranger has already cloned.

Four rules it enforces, and the reasoning for each is in the module it tests:

- **Copy what is named, never everything-except.** A deny list ships a file added tomorrow by
  default, and the file nobody decided about is exactly the one that leaks.
- **Refuse binaries by extension.** A text scanner reads an `.xlsx` as empty and passes it; this
  repository holds two that carry real exported Oracle data. A rule beats a reading.
- **Refuse a non-empty target.** An export written over a previous run ships whatever survived from
  it — the same hazard the deploy bundle had, where a survivor is not junk but cargo.
- **Scan the copy, not the source.** The copy is what a stranger receives, and it is the last
  moment anything can be checked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.control import export_public
from db_ops.lib.distribution import DOC_FOR_PACKAGE, PRIVATE_PACKAGES, PUBLIC_PACKAGES

#: A package that does not ship — invented here, and deliberately not a real one.
#:
#: This file has now been wrong in both directions. It first named `sre`, which was true until
#: `v0.2.0` shipped it, and three tests failed for having memorised an answer the code already
#: knew. It then read `sorted(PRIVATE_PACKAGES)[0]`, which was correct until `v0.3.2` shipped the
#: last withheld package and the list became empty — and an empty list is not a broken manifest,
#: it is the goal.
#:
#: These tests are about the **mechanism** that honours an exclusion, not about which packages are
#: currently excluded. So the fixture builds a miniature repository containing a package named
#: below, and the exclusion is injected. The mechanism stays under test whether or not anything
#: real is withheld, which is the only version of this that survives the list changing again.
WITHHELD = "atlantis"
WITHHELD_DOC = "99_atlantis_app.md"


@pytest.fixture(autouse=True)
def _atlantis_is_withheld(monkeypatch):
    """Declare the invented package private, everywhere the export looks it up."""
    private = {WITHHELD: "invented for this test; the mechanism is the subject, not the list"}
    monkeypatch.setattr("db_ops.lib.distribution.PRIVATE_PACKAGES", private)
    monkeypatch.setattr("db_ops.control.export_public.PRIVATE_PACKAGES", private)
    monkeypatch.setitem(DOC_FOR_PACKAGE, WITHHELD, WITHHELD_DOC)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A miniature of this repository: two public packages, one private, and the paths around them."""
    root = tmp_path / "repo"
    for name in ("lib", "metrics"):
        (root / "db_ops" / name).mkdir(parents=True)
        (root / "db_ops" / name / "__init__.py").write_text("", encoding="utf-8")
    (root / "db_ops" / WITHHELD).mkdir(parents=True)
    (root / "db_ops" / WITHHELD / "__init__.py").write_text("", encoding="utf-8")
    (root / "db_ops" / "__init__.py").write_text('__version__ = "0.0.0"\n', encoding="utf-8")

    (root / "docs").mkdir()
    (root / "docs" / "04_metrics_engine.md").write_text("# metrics\n", encoding="utf-8")
    (root / "docs" / WITHHELD_DOC).write_text("# sre\n", encoding="utf-8")

    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text("import pytest\n", encoding="utf-8")
    (root / "tests" / "test_metrics.py").write_text(
        "from db_ops.metrics import cli\n", encoding="utf-8")
    (root / "tests" / f"test_{WITHHELD}.py").write_text(
        f"from db_ops.{WITHHELD} import cli\n", encoding="utf-8")

    (root / "data").mkdir()
    (root / "data" / "db_instances.json").write_text("{}", encoding="utf-8")
    (root / "data" / "db_instances.example.json").write_text("{}", encoding="utf-8")

    (root / "audits").mkdir()
    (root / "audits" / "secret.md").write_text("names a real host\n", encoding="utf-8")

    (root / "README.md").write_text("# readme\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    return root


def _shipped(plan: export_public.ExportPlan) -> set[str]:
    return {relative.as_posix() for _, relative in plan.files}


# --------------------------------------------------------------------------- #
# What crosses, and what does not
# --------------------------------------------------------------------------- #
def test_a_private_package_does_not_cross(tree: Path) -> None:
    shipped = _shipped(export_public.build_plan(tree))

    assert "db_ops/metrics/__init__.py" in shipped
    assert not any(path.startswith(f"db_ops/{WITHHELD}/") for path in shipped)


def test_the_doc_of_a_private_package_does_not_cross(tree: Path) -> None:
    """Derived from the package list, so the two can never disagree about what exists."""
    plan = export_public.build_plan(tree)

    assert "docs/04_metrics_engine.md" in _shipped(plan)
    assert f"docs/{WITHHELD_DOC}" not in _shipped(plan)
    assert f"docs/{WITHHELD_DOC}" in plan.skipped_docs


def test_the_operators_estate_does_not_cross_but_its_example_does(tree: Path) -> None:
    """`data/` is handled per file. Copying the folder is one edit away from shipping the estate."""
    shipped = _shipped(export_public.build_plan(tree))

    assert "data/db_instances.example.json" in shipped
    assert "data/db_instances.json" not in shipped


def test_a_private_path_does_not_cross(tree: Path) -> None:
    shipped = _shipped(export_public.build_plan(tree))

    assert not any(path.startswith("audits/") for path in shipped)


def test_a_test_for_a_private_package_does_not_cross(tree: Path) -> None:
    """It could not pass there — the module is absent — and it carries that package's identifiers."""
    plan = export_public.build_plan(tree)

    assert "tests/test_metrics.py" in _shipped(plan)
    assert f"tests/test_{WITHHELD}.py" not in _shipped(plan)
    assert any(f"test_{WITHHELD}.py" in entry for entry in plan.skipped_tests)


def test_shared_test_machinery_always_crosses(tree: Path) -> None:
    """`conftest.py` is not a test, and dropping it takes every test that depends on it.

    Measured the hard way on 2026-08-22: the first version of the filter removed `conftest.py`
    because one fixture imports `db_ops.reports`, and **18 unrelated files failed collection** with
    `ModuleNotFoundError: No module named 'conftest'` — a message that says nothing about the cause.
    """
    (tree / "tests" / "conftest.py").write_text(
        f"from db_ops.{WITHHELD} import cli\n", encoding="utf-8")

    assert "tests/conftest.py" in _shipped(export_public.build_plan(tree))


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
def test_a_binary_is_refused_rather_than_copied(tree: Path, tmp_path: Path) -> None:
    """A text scanner reads an `.xlsx` as empty and passes it. A rule beats a reading."""
    (tree / "docs" / "export.xlsx").write_bytes(b"PK\x03\x04binary")

    plan = export_public.build_plan(tree)
    assert "docs/export.xlsx" in plan.refused_binaries

    with pytest.raises(export_public.ExportError, match="not text"):
        export_public.export(tree, tmp_path / "out")


def test_a_binary_can_be_allowed_by_name(tree: Path, tmp_path: Path) -> None:
    """Shipping one is a decision somebody has to make in the open, per file."""
    (tree / "docs" / "diagram.png").write_bytes(b"\x89PNG")

    plan = export_public.export(tree, tmp_path / "out", allow_binaries={"docs/diagram.png"},
                                allow_inside_git=True)

    assert "docs/diagram.png" in _shipped(plan)


def test_a_non_empty_target_is_refused(tree: Path, tmp_path: Path) -> None:
    """An export over a previous run ships whatever survived it — a survivor is cargo, not junk."""
    target = tmp_path / "out"
    target.mkdir()
    (target / "leftover.py").write_text(f"from db_ops.{WITHHELD} import cli\n", encoding="utf-8")

    with pytest.raises(export_public.ExportError, match="not empty"):
        export_public.export(tree, target, allow_inside_git=True)

    assert (target / "leftover.py").exists(), "a refusal must change nothing"


def test_force_empties_the_target_first(tree: Path, tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()
    (target / "leftover.py").write_text("x = 1\n", encoding="utf-8")

    export_public.export(tree, target, force=True, allow_inside_git=True)

    assert not (target / "leftover.py").exists()


def test_shared_machinery_importing_a_private_package_at_module_scope_is_refused(
    tree: Path, tmp_path: Path
) -> None:
    """It always ships, so a module-scope import there fails every test in the public tree.

    A lazy import inside a fixture is a different thing and is allowed: it costs nothing until
    something calls it, and the tests that would call it are filtered out anyway.
    """
    (tree / "tests" / "conftest.py").write_text(
        f"from db_ops.{WITHHELD} import cli\n", encoding="utf-8")

    with pytest.raises(export_public.ExportError, match="shared test machinery"):
        export_public.export(tree, tmp_path / "out")


def test_a_lazy_import_in_shared_machinery_is_allowed(tree: Path, tmp_path: Path) -> None:
    (tree / "tests" / "conftest.py").write_text(
        f"def fixture():\n    from db_ops.{WITHHELD} import cli\n    return cli\n", encoding="utf-8")

    plan = export_public.export(tree, tmp_path / "out", allow_inside_git=True)

    assert "tests/conftest.py" in _shipped(plan)


# --------------------------------------------------------------------------- #
# The plan is a decision, not a discovery
# --------------------------------------------------------------------------- #
def test_a_path_nobody_decided_about_is_reported(tree: Path) -> None:
    """A new top-level directory is invisible to an export that copies what it is told.

    Reported rather than shipped, and reported rather than silently dropped: a capability missing
    from the public tree is as wrong as a private file in it, and only a person can tell which
    this is.
    """
    (tree / "brand_new").mkdir()

    assert "brand_new" in export_public.unplanned_paths(tree)


def test_the_export_writes_what_it_planned(tree: Path, tmp_path: Path) -> None:
    target = tmp_path / "out"

    plan = export_public.export(tree, target, allow_inside_git=True)

    written = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert written == _shipped(plan)


def test_this_repository_exports_without_refusing() -> None:
    """The real tree, planned end to end. A refusal here is a finding, not a broken test."""
    root = Path(__file__).resolve().parents[1]
    plan = export_public.build_plan(root)

    assert not plan.refused_binaries, f"binaries with no decision: {plan.refused_binaries}"
    assert not plan.missing_required, f"a required public path is missing: {plan.missing_required}"
    assert plan.file_count > 400
    assert set(plan.skipped_packages) == set(PRIVATE_PACKAGES)
    assert not set(plan.skipped_packages) & set(PUBLIC_PACKAGES)


# --------------------------------------------------------------------------- #
# The refusal that exists because it already went wrong
# --------------------------------------------------------------------------- #
def _repo(path: Path, *, remote: str | None) -> Path:
    """A real git repository at *path*, with or without a remote."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote],
                       check=True, capture_output=True)
    return path


def test_the_export_refuses_to_write_into_a_repository_that_can_push(
    tree: Path, tmp_path: Path
) -> None:
    """A public tree inside a checkout with a remote is one `git push` from an early release.

    `HR-1` says the public repository does not exist yet and must not be created yet, and both
    halves of an accidental release are irreversible: a public git history and a PyPI version.

    This happened on 2026-08-22. The export was pointed at a path the operator had connected to
    their GitHub account. Nothing was pushed — but the fix is not "be careful with the argument".
    A tool that can cause an irreversible release should refuse to be aimed at one.
    """
    checkout = _repo(tmp_path / "checkout", remote="https://example.com/someone/repo.git")

    with pytest.raises(export_public.ExportError, match="can push"):
        export_public.export(tree, checkout / "public")

    assert not (checkout / "public").exists(), "a refusal must write nothing at all"


def test_a_repository_with_no_remote_is_allowed(tree: Path, tmp_path: Path) -> None:
    """`git init` with no remote is the *correct* place to stage a public tree, not a hazard.

    The rule is about the remote, not about git. Refusing every git directory would forbid the
    right workflow — review the tree under version control, connect a remote only when it is
    ready — in order to prevent the wrong one.
    """
    staging = _repo(tmp_path / "staging", remote=None)

    plan = export_public.export(tree, staging / "public")

    assert plan.file_count > 0


def test_the_refusal_names_the_repository_and_its_remote(tree: Path, tmp_path: Path) -> None:
    """Naming both is what makes it actionable: the target itself usually looks innocent."""
    checkout = _repo(tmp_path / "checkout", remote="https://example.com/someone/repo.git")

    with pytest.raises(export_public.ExportError) as raised:
        export_public.export(tree, checkout / "deep" / "nested" / "public")

    message = str(raised.value)
    assert str(checkout) in message and "origin" in message


def test_a_plain_directory_is_still_allowed(tree: Path, tmp_path: Path) -> None:
    """The common case must stay simple; most exports go to a directory that is not a repo."""
    plan = export_public.export(tree, tmp_path / "plain", allow_inside_git=True)

    assert plan.file_count > 0


def test_the_guard_can_be_overridden_deliberately(tree: Path, tmp_path: Path) -> None:
    """An escape hatch, because someone will eventually have a good reason.

    Explicit and per-call: the refusal is the default, and turning it off is a decision written in
    the command rather than a state the tool remembers.
    """
    checkout = _repo(tmp_path / "checkout", remote="https://example.com/someone/repo.git")

    plan = export_public.export(tree, checkout / "public", allow_inside_git=True)

    assert plan.file_count > 0
