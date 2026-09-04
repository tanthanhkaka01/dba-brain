"""Produce the public tree: a filtered copy of this repository, ready to become `dba-brain`.

**The scrub can reach zero and publication still cannot happen.** Nothing in this tree turned the
private repository into the public one until this module existed, and that — not the scrub — was
the structural blocker: `control.cli` had fifteen subcommands and none of them produced a tree.

Two decisions shape it, both made on 2026-08-22:

- **`dba-brain` is a new repository receiving a copy.** Not a history rewrite. `HR-2`'s fresh
  history is satisfied by construction, and `G-02` becomes a property of *one tree* rather than of
  every commit that ever existed — which is the difference between reviewing 1,006 files and
  reviewing 1,006 files times every revision they ever had.
- **`v0.1.0` is thin**: one SQL Server, metrics, a Telegram alert. Seven of the fourteen components
  do not ship, and `db_ops/lib/distribution.py` is the single declaration of which.

**Copy what is named, never everything-except.** A deny list means a file added tomorrow ships by
default, and the file nobody decided about is exactly the one that leaks. So `PUBLIC_PATHS` is a
plan, an unknown top-level path is an error rather than a guess, and the export refuses rather than
improvising.

**It refuses binaries by extension** (`03-16`). `scripts/archive/` holds two `.xlsx` files of real
exported Oracle data and a public key naming a host and a cloud tenancy — all of which a text
scanner reads as empty and passes. A rule beats a reading, so anything not known to be text has to
be allow-listed by hand.

**And it runs the identifier scan over the result.** `check-identifiers` exists precisely so that
this step is a gate rather than a report: a tree with a real hostname in it does not get written.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from db_ops.lib.distribution import (
    PRIVATE_PACKAGES,
    PUBLIC_DISTRIBUTION_NAME,
    PUBLIC_VERSION,
    PRIVATE_PATHS,
    is_private_subpath,
    PRIVATE_TESTS,
    PUBLIC_PATHS,
    private_docs,
)

#: File types the export will copy. Anything else must be named in ``allow_binaries``.
#:
#: An allow list rather than a deny list for the same reason the path plan is: a new binary format
#: added to the tree must be a decision. `.png` and `.ico` are absent deliberately — the moment
#: this project ships an image, somebody should have to say so here.
TEXT_SUFFIXES: frozenset[str] = frozenset({
    ".py", ".sql", ".sh", ".ps1", ".bat", ".cmd",
    ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".conf",
    ".md", ".txt", ".rst", ".example", ".in",
    ".gitignore", ".dockerignore", ".env",
    # Templates the shipped code *renders*, not documents it prints. Both are plainly text and
    # both were being refused as unrecognised binaries — which is the rule working as designed and
    # reaching the wrong answer: `reports` renders nothing without its `.html`, and `docker_db`
    # cannot write a compose file without its `.j2`. Twenty-seven files, and the packages that
    # need them do not function without them.
    ".j2", ".html",
})

#: Files with no suffix at all that are still text, by name.
TEXT_NAMES: frozenset[str] = frozenset({
    "Dockerfile", "LICENSE", "NOTICE", "Makefile", ".gitignore", ".dockerignore", ".env.example",
    ".gitattributes", ".gitleaks.toml",
})

#: Public paths whose absence stops the export. The rest are copied when present and reported when
#: not, because the two failures are not the same size: a tree with no `LICENSE` cannot legally be
#: published, while a tree with no `.env.example` is merely thinner than intended.
REQUIRED_PATHS: frozenset[str] = frozenset({
    "README.md", "LICENSE", "pyproject.toml",
})

#: Never copied, wherever they appear in a tree that is otherwise public.
ALWAYS_SKIP: frozenset[str] = frozenset({
    "__pycache__", ".git", ".venv", ".pytest_cache", ".pytest_tmp", ".mypy_cache",
    "node_modules", "build", "dist", "deploy", "logs", "runtime", ".vscode",
    "db_ops.egg-info",
})


#: Files under ``tests/`` that are shared machinery rather than tests, and are never filtered out
#: however they import. Dropping one takes every test that depends on it with it — measured the
#: hard way: `conftest.py` imports `db_ops.reports` for one fixture, so the first version of the
#: filter removed it and **18 unrelated test files failed collection** with
#: `ModuleNotFoundError: No module named 'conftest'`, which says nothing about the cause.
TEST_INFRASTRUCTURE: frozenset[str] = frozenset({"conftest.py", "__init__.py"})


def _private_imports(path: Path, *, module_level_only: bool = False) -> list[tuple[str, str]]:
    """Every ``db_ops`` component this file imports that the thin release does not ship.

    ``module_level_only`` is the difference between "this file cannot be collected" and "this file
    has a code path that needs a private package". A lazy import inside a fixture is the second:
    it costs nothing until something calls it, and the tests that would are filtered out anyway.
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    nodes = tree.body if module_level_only else list(ast.walk(tree))
    found: list[tuple[str, str]] = []
    for node in nodes:
        modules: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        for module in modules:
            parts = module.split(".")
            if len(parts) > 1 and parts[0] == "db_ops" and parts[1] in PRIVATE_PACKAGES:
                found.append((module, parts[1]))
    return found


def test_exercises_a_private_package(path: Path) -> tuple[bool, str]:
    """True when a test file imports a component the thin release does not ship.

    Such a file cannot pass in the public tree — the module it imports is not there — so shipping
    it hands a stranger a suite that fails on `git clone`. It also carries that component's
    identifiers: on 2026-08-22, **94 of the 127 remaining identifiers in `tests/` were in these
    files**, so filtering them is most of the scrub as well as most of the correctness.

    Shared machinery is exempt (:data:`TEST_INFRASTRUCTURE`) — it is not a test, and removing it
    breaks every test that depends on it.

    Read statically, for the same reason the closure walk is: importing the tests to find out what
    they import needs every driver installed, and the whole point is that the public half stands
    alone.
    """
    if path.name in TEST_INFRASTRUCTURE:
        return False, ""
    private = _private_imports(path)
    return (True, private[0][1]) if private else (False, "")


def publishable_repository_above(target: Path) -> tuple[Path, str] | None:
    """The nearest git repository at or above *target* **that can push somewhere**, or ``None``.

    The hazard is not git. It is a **remote**: a public tree inside a checkout that can push is one
    command away from an early release, and `HR-1` says the public repository does not exist yet.
    Both halves of an accidental release are irreversible — a public git history and a PyPI version.

    So a repository with no remote is fine, and that matters: `git init` with no remote is exactly
    the right staging ground for reviewing a public tree before it becomes one. Refusing every git
    directory would forbid the correct workflow to prevent the wrong one.

    This exists because it went wrong on 2026-08-22: the export was pointed at a path the operator
    had connected to their GitHub account. Nothing was pushed, and the `rm -rf` that preceded the
    export may have destroyed a local clone. The fix is not "be careful with the argument" — a tool
    that can cause an irreversible release should refuse to be aimed at one.
    """
    import subprocess

    for candidate in [target, *target.parents]:
        if not (candidate / ".git").exists():
            continue
        try:
            result = subprocess.run(
                ["git", "-C", str(candidate), "remote"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # No git binary, or it would not answer. Refuse rather than assume: an unanswerable
            # question about whether this can be published is not a "no".
            return candidate, "could not be asked whether it has a remote"
        remotes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if remotes:
            return candidate, ", ".join(remotes)
        return None
    return None


class ExportError(RuntimeError):
    """The export refused. Every message says what to do about it."""


@dataclass
class ExportPlan:
    """What an export would write, decided before anything is written."""

    files: list[tuple[Path, Path]] = field(default_factory=list)
    skipped_packages: list[str] = field(default_factory=list)
    skipped_docs: list[str] = field(default_factory=list)
    skipped_tests: list[str] = field(default_factory=list)
    skipped_subpaths: list[str] = field(default_factory=list)
    refused_binaries: list[str] = field(default_factory=list)
    missing_paths: list[str] = field(default_factory=list)
    uncommitted: list[str] = field(default_factory=list)

    @property
    def missing_required(self) -> list[str]:
        """The absences that stop the export, as opposed to the ones worth mentioning."""
        return [name for name in self.missing_paths if name in REQUIRED_PATHS]

    @property
    def file_count(self) -> int:
        return len(self.files)


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES


def _walk(source: Path, *, root: Path | None = None) -> list[Path]:
    """Every file under *source*, minus the directories nothing ever ships.

    The skip test is applied to the path **relative to the repository**, not to the absolute one.
    Checking absolute parts means a repository that happens to live under a directory called
    `build`, `dist` or `.pytest_tmp` exports nothing at all — and reports success, because zero
    files copied is not an error to a loop. Found by the tests, which run under `.pytest_tmp`.
    """
    base = root if root is not None else source
    kept: list[Path] = []
    for child in sorted(source.rglob("*")):
        if not child.is_file():
            continue
        try:
            parts = child.relative_to(base).parts
        except ValueError:
            parts = child.parts
        if any(part in ALWAYS_SKIP for part in parts):
            continue
        kept.append(child)
    return kept


def _uncommitted_files(root: Path) -> set[str]:
    """Repo-relative paths git does not have committed: untracked, staged or modified.

    The export copies the **working tree**, which is what makes it able to ship a change before it
    is committed — convenient, and the reason it once published five half-written modules from
    another session that happened to be open at the time. CI caught those on a duplicate-definition
    guard, which is the kind of thing unfinished code trips.

    A file nobody has committed is a file nobody has decided is done. This does not stop an export
    — the operator may deliberately be shipping a work in progress — but it is named, loudly,
    because the failure mode is silent and public.

    Returns an empty set when git cannot answer (not a repository, git absent). Not being able to
    check is not the same as there being nothing to report, and the caller says so.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root, capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()

    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        # Porcelain v1: two status characters, a space, then the path. A rename is `old -> new`
        # and the new name is the one that would be copied.
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path.replace("\\", "/"))
    return paths


def build_plan(root: Path, *, allow_binaries: frozenset[str] | set[str] = frozenset()) -> ExportPlan:
    """Decide the whole copy before writing a byte of it.

    Separate from :func:`export` because a plan can be reviewed, diffed and asserted on, and
    because "what would this ship" is a question worth being able to ask without producing a tree.
    Every refusal below is reported rather than raised, so one run names *all* the problems instead
    of the first one.
    """
    plan = ExportPlan()
    dropped_docs = set(private_docs())
    in_flight = _uncommitted_files(root)

    for name in PUBLIC_PATHS:
        source = root / name
        if not source.exists():
            plan.missing_paths.append(name)
            continue
        candidates = [source] if source.is_file() else _walk(source, root=root)
        for child in candidates:
            relative = child.relative_to(root)
            if relative.as_posix() in dropped_docs:
                plan.skipped_docs.append(relative.as_posix())
                continue
            if not _is_text(child) and relative.as_posix() not in allow_binaries:
                plan.refused_binaries.append(relative.as_posix())
                continue
            if relative.parts[0] == "tests" and child.suffix == ".py":
                if child.name in PRIVATE_TESTS:
                    plan.skipped_tests.append(f"{relative.as_posix()} (describes this repository)")
                    continue
                exercises, package = test_exercises_a_private_package(child)
                if exercises:
                    plan.skipped_tests.append(f"{relative.as_posix()} ({package})")
                    continue
            plan.files.append((child, relative))

    # The package, minus the components this release does not ship.
    package = root / "db_ops"
    for child in _walk(package, root=root):
        relative = child.relative_to(root)
        parts = relative.parts
        if len(parts) > 2 and parts[1] in PRIVATE_PACKAGES:
            if parts[1] not in plan.skipped_packages:
                plan.skipped_packages.append(parts[1])
            continue
        # Operator data living inside a shipped package. `PRIVATE_PATHS` is matched on the
        # top-level name and cannot say "this folder, three levels down" — and what is down there
        # is invisible to `check-identifiers`, which searches inventory terms and so reads a
        # workstation name or a lab VM as clean. See `distribution.PRIVATE_SUBPATHS`.
        if is_private_subpath(relative.as_posix()):
            plan.skipped_subpaths.append(relative.as_posix())
            continue
        if not _is_text(child) and relative.as_posix() not in allow_binaries:
            plan.refused_binaries.append(relative.as_posix())
            continue
        plan.files.append((child, relative))

    # `data/` is not copied as a folder — only the examples cross, and they cross renamed to
    # nothing: an example ships *as* an example. Copying the folder would be one edit away from
    # shipping the estate.
    data = root / "data"
    if data.is_dir():
        for child in sorted(data.glob("*.example.json")):
            plan.files.append((child, child.relative_to(root)))
        # `data/*.md` is product documentation that happens to live beside the configuration:
        # `telegram_support_commands.md` is what an operator pastes into BotFather, and the suite
        # checks the JSON and the doc list the same commands. Excluding it broke that check in the
        # export while it passed here, which is the worst shape a difference can take.
        for markdown in sorted(data.glob("*.md")):
            plan.files.append((markdown, markdown.relative_to(root)))

    secrets = root / "secrets"
    if secrets.is_dir():
        for child in sorted(secrets.glob("*.example.json")):
            plan.files.append((child, child.relative_to(root)))

    # Which of the files about to ship has nobody committed? Computed last, against the finished
    # plan, so it reports what would actually be published rather than everything dirty in the
    # checkout — `data/` and `audits/` are always changing and never cross.
    if in_flight:
        plan.uncommitted = sorted(
            relative.as_posix() for _, relative in plan.files
            if relative.as_posix() in in_flight
        )

    return plan


def unplanned_paths(root: Path) -> list[str]:
    """Tracked top-level paths that are in neither list — the ones nobody has decided about.

    This is the check that keeps the plan honest as the repository grows. A new top-level directory
    is invisible to an export that copies what it is told, so it would silently *not* ship — and a
    capability quietly missing from the public tree is as wrong as a private file quietly in it.
    """
    known = set(PUBLIC_PATHS) | set(PRIVATE_PATHS) | {"db_ops", "data", "secrets"}
    seen: list[str] = []
    for child in sorted(root.iterdir()):
        name = child.name
        if name in ALWAYS_SKIP or name.startswith(".git"):
            continue
        if name not in known and name not in seen:
            seen.append(name)
    return seen


def export(
    root: Path,
    target: Path,
    *,
    allow_binaries: frozenset[str] | set[str] = frozenset(),
    force: bool = False,
    allow_inside_git: bool = False,
) -> ExportPlan:
    """Write the public tree. Refuses on anything the plan could not decide.

    The target must be empty. An export written *over* an existing tree is the same hazard the
    deploy bundle had: a survivor from a previous run is not junk, it is cargo, and it ships.
    """
    plan = build_plan(root, allow_binaries=allow_binaries)

    problems: list[str] = []
    for source, relative in plan.files:
        if relative.parts[0] == "tests" and source.name in TEST_INFRASTRUCTURE:
            leaks = _private_imports(source, module_level_only=True)
            if leaks:
                problems.append(
                    f"{relative.as_posix()} is shared test machinery, so it always ships - and "
                    "it imports a package this release does not, at module scope. Every test "
                    "would fail collection:\n  "
                    + "\n  ".join(f"{module} ({package})" for module, package in leaks)
                    + "\nMove the import inside the function that needs it."
                )
    if plan.refused_binaries:
        problems.append(
            "these are not text and are not allow-listed, and a text scanner cannot read them:\n  "
            + "\n  ".join(plan.refused_binaries)
        )
    if plan.missing_required:
        problems.append(
            "a public tree cannot be published without these, and they are not here: "
            + ", ".join(plan.missing_required)
        )
    if problems:
        raise ExportError("\n\n".join(problems))

    found = publishable_repository_above(target)
    if found is not None and not allow_inside_git:
        repository, remotes = found
        raise ExportError(
            f"{target} is inside a git repository that can push "
            f"({repository}, remote: {remotes})."
            "\nThe public tree must not be written into one: it is one push away from an "
            "early release, and HR-1 says the public repository does not exist yet. Export to a "
            "plain directory - or a `git init` with no remote - review it, and connect a remote "
            "only when it is ready to be public."
        )

    if target.exists() and any(target.iterdir()):
        if not force:
            raise ExportError(
                f"{target} is not empty. An export written over an existing tree ships whatever "
                "survived from the previous run. Empty it, or pass force."
            )
        _empty_but_keep_metadata(target)
    target.mkdir(parents=True, exist_ok=True)

    for source, relative in plan.files:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    rename_distribution(target)
    return plan


def rename_distribution(target: Path) -> None:
    """Give the copy its public name and version, which this repository cannot carry.

    Both differ deliberately, and both are applied here rather than in the source because the
    source has a worker running it: the image is tagged from ``__version__``, so moving it to
    ``0.1.0`` here would send the deployed version backwards.

    On the way out there is no worker, which is the same reason D-03 defers the *module* rename to
    this point. Only the distribution name and the version move today; ``import db_ops`` is
    unchanged, and a distribution may legally be named differently from the module it installs.
    """
    pyproject = target / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        text = text.replace('name = "db_ops"', f'name = "{PUBLIC_DISTRIBUTION_NAME}"', 1)
        # The version stops being read from the package: a public release states its own, and
        # nothing in the public tree should have to know this repository's counter.
        text = text.replace('dynamic = ["version"]', f'version = "{PUBLIC_VERSION}"', 1)
        text = text.replace(
            "[tool.setuptools.dynamic]" + "\n" + 'version = { attr = "db_ops.__version__" }' + "\n",
            "",
        )
        pyproject.write_text(text, encoding="utf-8")

    init = target / "db_ops" / "__init__.py"
    if init.exists():
        text = init.read_text(encoding="utf-8")
        import re

        text = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{PUBLIC_VERSION}"', text, count=1)
        init.write_text(text, encoding="utf-8")


def _empty_but_keep_metadata(target: Path) -> None:
    """Clear a target for a fresh export **without destroying the repository it may be**.

    `--force` used to `rmtree` the whole directory. Pointed at an existing public repository — the
    normal case for a second release — that deletes `.git`, taking the history, the remote and any
    unpushed work with it. On Windows it does not even fail cleanly: it deletes some objects and
    then hits a permission error partway through, leaving a corrupted repository rather than none.

    Caught by doing exactly that on 2026-08-22, one command after the first push.
    """
    for child in sorted(target.iterdir()):
        if child.name in ALWAYS_SKIP or child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def discard(target: Path) -> None:
    """Remove a tree that failed its scan — **without destroying the repository it may be**.

    Writing a leaking tree and then printing "do not publish" leaves the leak on disk, one `cp`
    away from somewhere worse — the same shape as a deploy bundle whose survivors get shipped. A
    refusal has to remove what it refused.

    Found the moment `data/*.md` was added to the export: it brought in the operator's BotFather
    command list, 28 real identifiers, and the command exited 1 with all 594 files still there.

    It kept the `.git` hazard that :func:`_empty_but_keep_metadata` was written to close, in the
    one path nobody exercises on a good day: an `rmtree` of the whole target takes the history, the
    remote and any unpushed work of the public repository this normally exports into. Reproduced on
    2026-09-04 while cutting 0.7.0 — the refusal was correct (a real database name had reached a
    usage string) and the delete then walked into `.git`, surviving only because Windows refuses to
    unlink a read-only object file. On a filesystem that permits it, a scan doing its job destroys
    the repository it was protecting.

    So a refusal empties the copy and leaves the metadata, exactly as `--force` does; a target that
    is not a repository is still removed outright, because there is nothing there to keep.
    """
    if not target.exists():
        return
    if (target / ".git").exists():
        _empty_but_keep_metadata(target)
        return
    shutil.rmtree(target)


def scan_exported_tree(target: Path) -> dict:
    """Run the identifier scan over what was just written. A hit means the tree does not ship.

    Deliberately re-run against the *copy* rather than trusting the scan of the source. The two
    differ — the copy is what a stranger receives — and this is the last moment anything can be
    checked before a tree becomes public and permanent.
    """
    from db_ops.common import identifier_scan

    paths = [child.name for child in sorted(target.iterdir()) if not child.name.startswith(".")]
    return identifier_scan.scan({"root": str(target), "paths": paths})
