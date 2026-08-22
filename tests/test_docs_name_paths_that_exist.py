"""Every repository path the documentation names is a path that exists.

On 2026-08-22 the shipped assets moved out of ``db_ops/assets/`` and into the component that owns
each tree — ``metrics/collectors``, ``common/backup_scripts``, ``common/restore_scripts``,
``sre/host_config``. The code and the tests moved with them and the suite stayed green, because a
test executes its paths. Documentation does not: **thirteen markdown files went on naming
``db_ops/assets/...`` and nothing noticed**, and the only thing that caught it was a person
grepping by hand afterwards.

That is the failure mode worth guarding. A doc naming a path that no longer exists is worse than a
doc with no path at all: the reader tries it, gets nothing, and cannot tell whether they have the
wrong tree or the wrong version. It had already happened twice before — an ``assets/``
reorganisation each time, which is exactly the kind of change that moves many files and touches no
behaviour, so nothing that runs has an opinion about it.

Two vocabularies are checked, because the docs legitimately use both:

- **``db_ops/...``** is a literal location on disk, always. It is where the code is and it means
  nothing else.
- **``assets/...``** is what *configuration* says, and it resolves through
  :func:`db_ops.lib.paths.resolve_tool_path` — the operator's tree first, the package's shipped
  copy second. So it is checked the way the tool would resolve it rather than as a literal
  directory: ``assets/backup/sqlserver/mssql_backup_database.sh`` is correct even though no such
  file sits at the repository root, and stops being correct the moment the shipped copy moves
  without ``BUILTIN_ASSET_ROOTS`` being updated.

The sweep reads the **whole page**, not just inline code spans. The first version of this guard
read only backticked tokens and passed while a path inside a fenced shell block was broken on
purpose to test it — and fenced blocks are where the runnable examples live, which is where a
wrong path costs the most.

Three kinds of token are skipped rather than excepted, because none of them claims a file exists:

- a **shape** — a brace set, a glob, an angle-bracket placeholder;
- a place *inside* a file — ``module.py::function``, ``worker_status.py:17`` — where the file is
  the part that has to exist, so the suffix is cut and the file checked;
- an **operator-owned** asset kind. ``assets/tasks/`` and ``assets/sql_telegram_commands/`` ship no
  built-in copy at all (:data:`db_ops.lib.paths.OPERATOR_ASSET_KINDS`), so every path under them in
  a doc is an example of what *you* would write. Requiring those to exist would mean shipping one
  estate's task SQL to make the documentation pass.

``audits/`` is deliberately outside the sweep. An audit is true as of its date and goes stale by
design; correcting one to match today's tree is the one thing ``audits/README.md`` forbids.
"""

from __future__ import annotations

import re
from pathlib import Path

from db_ops.lib.paths import OPERATOR_ASSET_KINDS, resolve_tool_path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Any token that looks like a path into the code or the asset trees, anywhere on the page —
#: prose, table cell, inline span or fenced block. The lookbehind stops it matching the tail of a
#: longer path that was already matched from its start.
DOCUMENTED_PATH = re.compile(r"(?<![\w./-])((?:db_ops|assets)/[\w./{}<>*?,-]+)")

#: Trailing punctuation a sentence or a JSON snippet leaves on the end of a path.
TRAILING = ".,;:)`\"'"

#: Characters that make a token a *shape* rather than a name: a placeholder, a glob, a brace set.
NOT_A_LITERAL_PATH = "<>*{}?"

#: ``module.py::symbol`` and ``module.py:17`` name a place inside a file. The file is the part that
#: has to exist, so the suffix is cut before checking.
INSIDE_A_FILE = re.compile(r"(?P<path>[^\s:]+\.(?:py|sql|sh|ps1|json|md))::?[\w.]+$")

#: The example this guard uses to prove it reads fenced blocks. Pinned rather than described,
#: because "the sweep still works" is not something a passing test can otherwise claim.
A_PATH_INSIDE_A_FENCED_BLOCK = (
    "db_ops/metrics/collectors/sqlserver/legacy_2008r2/069_sqlserver_linked_server_status.sql"
)

#: Paths the docs name that genuinely do not exist, with the reason each is tolerated. Empty on
#: purpose: the sweep came back clean once the two real errors it found were fixed, and an entry
#: here should be rare enough to argue for. ``test_every_exception_is_still_earned`` deletes one
#: that stops being needed.
ALLOWED_MISSING: dict[str, str] = {}


def _documentation_pages() -> list[Path]:
    """The pages describing the tree as it is now — the per-component reference and the READMEs."""
    pages = sorted(REPO_ROOT.glob("docs/*.md"))
    for name in ("README.md", "assets/README.md", "data/README.md"):
        candidate = REPO_ROOT / name
        if candidate.exists():
            pages.append(candidate)
    return pages


def _is_operator_owned(token: str) -> bool:
    """True for an asset kind the package ships nothing of, where a documented path is an example."""
    parts = token.split("/")
    return len(parts) > 1 and parts[0] == "assets" and parts[1] in OPERATOR_ASSET_KINDS


def _documented_paths() -> list[tuple[Path, str]]:
    """Every token claiming a repository path, with the page that names it."""
    found: list[tuple[Path, str]] = []
    for page in _documentation_pages():
        for match in DOCUMENTED_PATH.finditer(page.read_text(encoding="utf-8")):
            token = match.group(1).rstrip(TRAILING)
            if any(character in token for character in NOT_A_LITERAL_PATH):
                continue
            inside = INSIDE_A_FILE.match(token)
            if inside:
                token = inside.group("path")
            if _is_operator_owned(token):
                continue
            found.append((page, token))
    return found


def _resolve(token: str) -> Path:
    """Where the tool would look for *token* — literal for code, the asset lookup for the rest."""
    if token.startswith("assets/"):
        return resolve_tool_path(token, tool_root=REPO_ROOT)
    return REPO_ROOT / token


def test_every_path_the_documentation_names_exists() -> None:
    missing = sorted(
        {
            (page.relative_to(REPO_ROOT).as_posix(), token)
            for page, token in _documented_paths()
            if token not in ALLOWED_MISSING and not _resolve(token).exists()
        }
    )
    assert not missing, (
        "These pages name repository paths that do not exist. A move that leaves the docs behind "
        "is invisible to every other test, because tests execute their paths and prose does "
        "not:\n" + "\n".join(f"  {page}: {token}" for page, token in missing)
    )


def test_the_sweep_reaches_inside_fenced_code_blocks() -> None:
    """The runnable examples live in fenced blocks, and the first version of this guard missed them.

    It was written reading inline code spans only, and passed while a path inside a fenced shell
    block was broken on purpose to check it. Pinning one such path means a regex that stops
    reaching into fences fails here rather than going quiet.
    """
    page = REPO_ROOT / "docs" / "13_common.md"
    on_that_page = [token for found, token in _documented_paths() if found == page]
    assert A_PATH_INSIDE_A_FENCED_BLOCK in on_that_page, (
        "The run-sql example's sql_file path was not picked up, so the sweep is no longer reading "
        "fenced code blocks."
    )


def test_the_sweep_actually_reads_something() -> None:
    """A regex that silently stops matching would make this guard pass by finding nothing.

    A floor rather than an equality: docs get written, and a guard that fails when someone
    documents one more file teaches people to delete the guard.
    """
    documented = _documented_paths()
    assert len(documented) > 100, (
        f"Only {len(documented)} documented paths were found, which means the sweep is broken "
        "rather than the docs being clean."
    )
    assert len({page for page, _ in documented}) > 5, (
        "The documented paths all come from too few pages for the sweep to be working."
    )


def test_every_exception_is_still_earned() -> None:
    """An exception that stops being needed must be deleted, not left as folklore."""
    for token, reason in ALLOWED_MISSING.items():
        assert not _resolve(token).exists(), (
            f"{token} exists now, so it should lose its exception ({reason})"
        )
