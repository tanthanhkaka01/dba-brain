"""What the public distribution contains, and what stays behind.

`v0.1.0` of the public toolkit is deliberately thin: **point it at one SQL Server, collect metrics,
add a Telegram token, get an alert.** That is a smaller product than this repository holds, and the
reason is not tidiness — `db_ops/sre` alone carried 21 of the 46 source scrub blockers, so most of
the remaining work to make a tree publishable leaves with the packages the thin release does not
need.

**This module is the one place that says which packages those are.** Two things read it and they
must not disagree: the packaging metadata that decides what a wheel contains, and the export that
produces the public tree. A second copy of this list is how a wheel and a repository start shipping
different software.

**Nothing is deleted from this repository to achieve the cut.** The worker runs the full toolkit
and must keep running; the Docker image copies `db_ops/` wholesale rather than installing the
wheel, so the distribution scope and the deployed scope are independent by construction. The cut is
a property of what is *copied out*, and this module states it.

The claim that makes the cut safe is that **nothing in the kept set imports anything excluded**.
That is measured rather than asserted — `tests/test_distribution_closure.py` walks the import graph
and fails the day someone adds the first edge across the line, which is the day the cut would
otherwise stop working silently.
"""

from __future__ import annotations

#: Packages the public distribution ships, as subpackage names under ``db_ops``.
#:
#: `v0.1.0` shipped seven of these — the import closure of four entry points, chosen because a
#: first release is easier to stand behind when it claims one path and ships exactly what that path
#: needs. **`v0.2.0` ships the toolkit**, and the measurements that were meant to gate that came in
#: rather than being waived: the complete tree passes its whole suite, and the identifier scan
#: reports nothing in it.
#:
#: Growing this list is still a decision somebody makes here, in the open. `public_package_globs`
#: writes it as globs so a *new subpackage of something shipped* comes along automatically while a
#: new top-level app does not, and that asymmetry is the point.
PUBLIC_PACKAGES: tuple[str, ...] = (
    "lib",
    "common",
    "db",
    "logging_ops",
    "metrics",
    "telegram",
    "jobs",
    "reports",
    "sql_tasks",
    "backup_restore",
    "sla",
    "sre",
    "webhost",
)

#: Packages that do not ship, each with the reason.
#:
#: Stated as a mapping rather than a set because an unexplained exclusion is indistinguishable from
#: an oversight, and this list decides what a stranger can and cannot do with the toolkit.
#:
#: Six entries left with `v0.2.0`. The one that remains is not waiting on maturity: it is
#: structural, and no later release reverses it.
PRIVATE_PACKAGES: dict[str, str] = {
    "control": "master/worker build and deploy, and the export itself. **The thing that produces "
               "the public tree must not be in the public tree** - a copy of the export would let "
               "a reader believe they can reproduce the private repository from the public one, "
               "and it carries the private-forever list, which is a map of what is being withheld",
}


def public_package_globs() -> tuple[str, ...]:
    """The ``[tool.setuptools.packages.find]`` include list for the thin distribution.

    Written as globs so a *new* subpackage of something shipped comes along automatically, while a
    new top-level package does not. That asymmetry is deliberate: growing `metrics` is routine, and
    adding a fourteenth app to the public distribution is a decision somebody should have to make
    here, in the open, rather than by creating a directory.
    """
    return ("db_ops",) + tuple(f"db_ops.{name}*" for name in PUBLIC_PACKAGES)


def is_public(module: str) -> bool:
    """True when *module* — ``db_ops.metrics.cli``, say — belongs to the thin distribution.

    A bare ``db_ops`` or one of its loose modules (``db_ops.config``, ``db_ops.levels``) is public:
    those are the package's own roots, and every entry point goes through them.
    """
    parts = module.split(".")
    if not parts or parts[0] != "db_ops":
        return False
    if len(parts) < 3 and (len(parts) == 1 or parts[1] not in PRIVATE_PACKAGES):
        return True
    return parts[1] in PUBLIC_PACKAGES


#: Top-level paths the public tree receives, beyond the package itself.
#:
#: Stated as a plan rather than discovered, because the failure mode of "copy everything except a
#: deny list" is that a file added tomorrow ships by default. A file nobody decided about is
#: exactly the file that leaks, so the export copies what is *named* and nothing else.
PUBLIC_PATHS: tuple[str, ...] = (
    # CI and the release workflow. They must travel with the export rather than be written into
    # the public repository by hand: `--force` empties everything except `.git`, so anything
    # created only over there disappears on the next export. And trusted publishing checks the
    # *workflow file name*, which makes it part of the product's identity rather than local setup.
    ".github",
    "README.md",
    "LICENSE",
    "NOTICE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "requirements.txt",
    "pytest.ini",
    ".gitignore",
    ".dockerignore",
    # The secret scan's configuration travels with the workflow that runs it. Left behind, the
    # public repository's scan would use the defaults and go red on the same false positive this
    # file exists to answer — and nobody reading that repository would know why.
    ".gitleaks.toml",
    # Line endings, pinned for the files Linux executes. Without it a Windows clone rewrites the
    # entrypoint to CRLF and the image stops being able to start — an error that names neither
    # the file nor the cause.
    ".gitattributes",
    ".env.example",
    "config.example.json",
    "docs",
    "examples",
    "tests",
    "docker",
    "Dockerfile",
    "docker-compose.yml",
)

#: Paths that never leave this repository, each with the reason. `G-10` is this table.
#:
#: Several are already git-ignored and so cannot be copied anyway; they are named regardless,
#: because "it is ignored" is a fact about this checkout and this list is a decision about the
#: product.
PRIVATE_PATHS: dict[str, str] = {
    "audits": "audits name hosts and accounts by design - an audit that names none stops being "
              "evidence. The techniques are republished as skills later, rewritten from scratch",
    "data": "the operator's live estate. Only data/*.example.json crosses, and that is handled "
            "per file rather than by copying the folder",
    "config.json": "the operator's runtime paths and store pointer; config.example.json ships "
                   "instead",
    "assets": "operator-authored SQL, written per server. It is operator content in the same "
              "sense db_instances.json is, and scrubbing it would produce queries that no longer "
              "run against tables nobody has",
    "scripts": "holds real exported Oracle data as .xlsx and a public key naming a host and a "
               "cloud tenancy - none of it readable by a text scanner",
    "tools": "the Oracle 8i bridge: a Win32 Python 2.7 payload that cannot run in the public "
             "image, and it carries bridge-token generation",
    "tests_estate": "exists to assert against real servers by name. That is its whole purpose, so "
                    "it can never be scrubbed and must never ship",
    "secrets": "the plaintext source of the secret store; only secrets/*.example.json crosses",
    "CLAUDE.md": "local working notes, and they hold the store passphrase on purpose",
    "docker-compose.runtime.yml": "the worker's compose: bind mounts and a pinned bridge subnet "
                                  "that describe this operator's host, not the product",
    ".vscode": "editor settings, of no use to anyone else",
    "deploy": "build output",
    "logs": "generated",
    "runtime": "generated",
    "base": "test scratch shaped like a pg_basebackup tree",
    "wal": "test scratch",
}

#: Paths *inside* a shipped package that are still operator data.
#:
#: Named file by file rather than by folder, and that is not fussiness: the first version withheld
#: `db_ops/sre/data_folder` whole, which took `deploy_sqlserver_ag.py` — the script that *reads*
#: those files, and product code — out of the export with them. `docs/10_sre_app.md` names it, so
#: the doc guard caught it in the exported tree. The captured run does not ship; the code does.
#:
#: `PRIVATE_PATHS` above is matched on the top-level name, which is the right shape for `audits`
#: or `data` and cannot express "this folder, three levels down". Everything here is the same
#: argument those entries make — content that belongs to one estate rather than to the product —
#: and each is kept out **by name**, because the alternative is scrubbing a file whose whole value
#: is being a true record.
#:
#: Note what does *not* protect these: `check-identifiers` derives its terms from the inventory, so
#: a developer workstation, a Windows account or a lab VM that was never a monitored target reads
#: as clean. A path list is the only thing that can refuse them.
PRIVATE_SUBPATHS: dict[str, str] = {
    "db_ops/sre/data_folder/20260612_install_sql_server.json":
        "the input to one real lab install: three lab hosts by address",
    "db_ops/sre/data_folder/20260612_result_install_sql_server.json":
        "its captured output - 18.9 KB of stdout carrying a workstation name, a Windows account "
        "and the SSH key path under it, VM and template names, and the lab subnet",
}


def is_private_subpath(relative: str) -> str | None:
    """The reason *relative* is withheld, or ``None`` if it ships.

    Takes the path relative to the repository root, in POSIX form, and matches a prefix so a whole
    folder can be named once.
    """
    candidate = relative.replace("\\", "/")
    for prefix, reason in PRIVATE_SUBPATHS.items():
        if candidate == prefix or candidate.startswith(prefix + "/"):
            return reason
    return None


#: Documentation that describes a package the thin release does not ship.
#:
#: Derived rather than listed: `docs/NN_<slug>.md` is one file per component, and
#: `tests/test_docs_cover_every_component.py` pins the mapping in both directions. So the docs to
#: drop are exactly the docs of the packages to drop, and the two can never drift.
DOC_FOR_PACKAGE: dict[str, str] = {
    "db": "01_runtime_store.md",
    "logging_ops": "02_logging_engine.md",
    "jobs": "03_app_command_daemon.md",
    "metrics": "04_metrics_engine.md",
    "sql_tasks": "05_sql_task_runner.md",
    "reports": "06_reports_app.md",
    "telegram": "07_telegram_app.md",
    "backup_restore": "08_backup_restore_app.md",
    "sla": "09_sla_slo_compliance_app.md",
    "sre": "10_sre_app.md",
    "control": "11_control_app.md",
    "webhost": "12_webhost_app.md",
    "common": "13_common.md",
    "lib": "14_lib.md",
}


def private_docs() -> tuple[str, ...]:
    """Component docs that must not ship, because their component does not."""
    return tuple(sorted(
        f"docs/{DOC_FOR_PACKAGE[name]}" for name in PRIVATE_PACKAGES if name in DOC_FOR_PACKAGE
    ))


#: Test files that describe **this repository**, not the product, and so do not travel with the
#: export. Each says why, because a test dropped without a reason is a test somebody will assume
#: was failing.
#:
#: The export already drops tests by what they *import*. These import nothing private — they assert
#: on the shape of the source tree itself, which is a different question and one the public tree
#: cannot answer about itself.
PRIVATE_TESTS: dict[str, str] = {
    "test_distribution_closure.py": "asserts that the seven withheld packages exist on disk, which "
                                    "is true of the private repository and false of the export by "
                                    "definition. The closure it guards is a property of the source",
    "test_legacy_oracle_tool_is_python2_safe.py": "checks tools/python32_legacy, a Win32 Python 2.7 "
                                                  "payload that never ships",
    "test_backup_restore_event_shape.py": "walks db_ops/backup_restore/*.py to check every event "
                                          "call site shares one shape. The package is withheld, so "
                                          "in the export it walks an empty path and reports that "
                                          "no file announces a run - a true statement about "
                                          "nothing. Listed here rather than caught by the import "
                                          "filter because it *reads* the package instead of "
                                          "importing it, which no static import walk can see",
}


#: What the public distribution is *called*, and what version it is at.
#:
#: Both differ from this repository on purpose, and the export applies them on the way out.
#:
#: **The name.** `pyproject.toml` says `db_ops` because that is what this repository builds and
#: what the worker runs. Uploading that to PyPI would claim `db_ops` and leave `dbabrain` — the
#: name the roadmap reserves and `G-09` requires — free for somebody else. The *import* package
#: stays `db_ops` for now: a distribution may be named differently from the module it installs,
#: and renaming the module is a separate change that D-03 defers because the path is stored as
#: text in `data/app_commands.json` and in the Dockerfile.
#:
#: **The version.** This repository is at 2.85.x, an internal counter with hundreds of increments
#: behind it. A first public release at 2.85.88 claims a history that never happened, and **a PyPI
#: version is immutable** — it cannot be re-uploaded after deletion, so the mistake is permanent.
#: The public tree starts where a first release starts.
PUBLIC_DISTRIBUTION_NAME = "dbabrain"
PUBLIC_VERSION = "0.3.0"
