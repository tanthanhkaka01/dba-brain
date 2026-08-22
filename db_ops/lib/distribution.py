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

#: Packages the thin `v0.1.0` ships, as subpackage names under ``db_ops``.
#:
#: The set is the import closure of four entry points — the metrics CLI, the Telegram CLI, the
#: store CLI and the daemon — not a list somebody liked the look of. `jobs` earns its place because
#: an alert nobody scheduled is a demo: the daemon is what makes collection recurring.
PUBLIC_PACKAGES: tuple[str, ...] = (
    "lib",
    "common",
    "db",
    "logging_ops",
    "metrics",
    "telegram",
    "jobs",
)

#: Packages the thin release leaves behind, each with the reason. They arrive in `v0.2.0`.
#:
#: Stated as a mapping rather than a set because an unexplained exclusion is indistinguishable from
#: an oversight, and this list decides what a stranger can and cannot do with the toolkit.
PRIVATE_PACKAGES: dict[str, str] = {
    "backup_restore": "backup and restore validation - the next capability after monitoring works",
    "control": "master/worker build and deploy, and the export itself. The thing that produces the "
               "public tree must not be in the public tree",
    "reports": "turns metrics into periodic reports. `v0.1.0` claims threshold alerting only, so "
               "this is the first thing to reconsider if that claim grows",
    "sla": "SLA/SLO evaluation, which needs report history to be worth anything",
    "sql_tasks": "the scheduled SQL runner - operator-authored SQL, a larger trust surface",
    "sre": "host provisioning, Ansible, VMware, database-in-Docker. The largest scrub liability in "
           "the tree and the furthest from the release claim",
    "webhost": "the configuration console, which exposes every other app",
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
PUBLIC_VERSION = "0.1.0"
