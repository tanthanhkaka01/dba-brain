"""What the public distribution contains, and what stays behind.

**As of `v0.3.2` the public distribution is the whole toolkit — all fourteen packages.** It did not
start there. `v0.1.0` was deliberately thin (point it at one SQL Server, collect metrics, add a
Telegram token, get an alert) because a first release is easier to stand behind when it claims one
path and ships exactly what that path needs. `v0.2.0` added six packages, and `v0.3.2` the last one.

**This module remains the one place that decides.** Two things read it and they must not disagree:
the packaging metadata that decides what a wheel contains, and the export that produces the public
tree. A second copy of this list is how a wheel and a repository start shipping different software.

An empty `PRIVATE_PACKAGES` does not make the lists ceremonial. What they hold now is the *shape*
of the decision — every exclusion states a reason, names something that exists, and is guarded by
a test — so that a future one has to be argued for in the open rather than made by deleting a line
from an export script.

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
    # Last one in, 2026-08-23. It builds and deploys, bumps the version, and runs the export that
    # produces the public tree - this project's own release process, readable by anyone using it.
    "control",
)

#: Packages that do not ship, each with the reason.
#:
#: Stated as a mapping rather than a set because an unexplained exclusion is indistinguishable from
#: an oversight, and this list decides what a stranger can and cannot do with the toolkit.
#:
#: Six entries left with `v0.2.0` and the last one left with `v0.3.2`, so this is **empty as of
#: 2026-08-23: every package ships.** The mapping stays because the next exclusion has to explain
#: itself in the same place.
#:
#: `control` was the last one out, withheld on the argument that "the thing that produces the
#: public tree must not be in the public tree" — that a copy of the export would suggest the
#: private repository could be reconstructed from the public one, and that it carries the
#: private-forever list, which is a map of what is being withheld.
#:
#: That argument does not survive contact with where the code actually lives. The manifest is
#: `db_ops/lib/distribution.py` and **it has shipped since `v0.1.0`** — this file, the one you are
#: reading. The map was never withheld; only the copy tool that reads it was, which hid nothing
#: and cost readers a working `bump-version`, `build-image`, `deploy` and `worker-status`.
#:
#: Nothing in `control` names an estate: the worker host, user and credential are all arguments.
#: What it does carry is this project's own release process, and an open-source release process
#: that can be read is better than one that cannot.
#:
#: An entry here should be rare and specific. "This package would embarrass us" is not a reason;
#: "this package cannot work outside one estate" is.
PRIVATE_PACKAGES: dict[str, str] = {}


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
    "CLAUDE.md": "the maintainer's local working notes for this repository; never part of the product",
    "docker-compose.runtime.yml": "the worker's compose: bind mounts and a pinned bridge subnet "
                                  "that describe this operator's host, not the product",
    ".vscode": "editor settings, of no use to anyone else",
    "deploy": "build output",
    "logs": "generated",
    "runtime": "generated",
    "base": "test scratch shaped like a pg_basebackup tree",
    "wal": "test scratch",
    ".ruff_cache": "lint cache",
    # An empty directory a tool left behind. Recorded rather than deleted because the export
    # refuses to guess: an undecided top-level path is reported on every run until somebody says
    # which side it is on, and that prompt is the feature — a directory nobody decided about is
    # exactly the one that ships something by accident.
    "empty_top": "an empty directory left by a tool; nothing to ship",
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
#: **Empty, and the goal is to keep it that way.** Both entries it used to hold were the captured
#: record of one real lab install, sitting inside a shipped package because that is where the
#: script that read them lived. They were moved to `audits/` on 2026-08-23 — a dated capture of one
#: real run is exactly what `audits/` is for — and `db_ops/sre/data_folder/install_sql_server.example.json`
#: took their place as the script's default: documentation range addresses, and the sudo password
#: as a `sudo_password_ref` into the secret store rather than a literal.
#:
#: That is the pattern for anything that lands here: an entry means product code and operator data
#: share a directory, and moving the data out is almost always available and always better than
#: withholding the file. A file kept out by name is a file nobody in the public tree can see is
#: missing.
PRIVATE_SUBPATHS: dict[str, str] = {}


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
    "test_legacy_oracle_tool_is_python2_safe.py":
        "checks tools/python32_legacy, a Win32 Python 2.7 payload that never ships",
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
PUBLIC_VERSION = "0.7.2"
