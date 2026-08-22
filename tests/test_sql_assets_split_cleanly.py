"""Every shipped .sql file must survive being cut into batches, because that is how it is run.

`split_sql_batches` splits a script on its `GO` lines. It is a text split — it does not parse SQL
and does not know what a comment is. So a bare `GO` **inside** a `/* ... */` block cuts the comment
in half, and the first batch reaches the server carrying an opening `/*` with no close.

That is not hypothetical. On 2026-08-11 `025_sqlserver_transaction_lock_holders.sql` failed on
every SQL Server target at once with:

    [42000] Missing end comment mark '*/'. (113)

The metric's real SQL ended at line 221. Lines 223-413 were a commented-out ad-hoc diagnostic
scratchpad someone had left in the asset, and line 226 inside it was a bare `GO`. The metric had
been dead on the whole estate, and the error message pointed at a comment mark rather than at the
`GO` that caused it — so nothing about the failure suggested where to look.

The fix was to delete the scratchpad; this test is here so the next one is caught before it ships.
It checks the property that actually matters — each batch is independently well-formed — rather
than "no GO inside a comment", because the second is one way to break the first and there are
others (a `GO` inside a quoted string would split the same way).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_ops.common.sql_execution import split_sql_batches
from db_ops.lib.paths import BUILTIN_ASSET_ROOTS


#: Every tree this project ships or runs SQL from. The shipped SQL now lives with the component
#: that owns it, so this is built from the map rather than from one directory name; the operator's
#: task and bot SQL stays at the tool root.
#:
#: Enumerating only some of them is how this file quietly went from 193 parametrised cases to 37
#: the day the built-ins moved — green, and testing a fifth of what it used to. That is why
#: `test_the_case_count_has_not_silently_collapsed` sits below: a root that stops being listed
#: costs cases, and losing cases must fail rather than pass quietly.
ASSET_ROOTS = tuple(BUILTIN_ASSET_ROOTS.values()) + (
    Path(__file__).resolve().parents[1] / "assets",
)

#: The floor the parametrised set must not fall through. Raise it when SQL is added; a drop means
#: a root stopped being enumerated, which is the failure this file has already had once.
MINIMUM_SQL_FILES = 150


def _sql_files() -> list[Path]:
    return sorted(path for root in ASSET_ROOTS if root.is_dir() for path in root.rglob("*.sql"))


def _relative(path: Path) -> str:
    for root in ASSET_ROOTS:
        if root in path.parents:
            return path.relative_to(root).as_posix()
    return path.as_posix()


def test_the_case_count_has_not_silently_collapsed() -> None:
    """A green run over a fifth of the files looks exactly like a green run over all of them."""
    found = _sql_files()

    assert len(found) >= MINIMUM_SQL_FILES, (
        f"only {len(found)} .sql files found across {[str(r) for r in ASSET_ROOTS]}; "
        f"expected at least {MINIMUM_SQL_FILES}. A shipped SQL tree has probably moved without "
        f"ASSET_ROOTS following it."
    )


@pytest.mark.parametrize("path", _sql_files(), ids=_relative)
def test_every_batch_of_a_shipped_script_is_a_complete_statement(path: Path) -> None:
    text = path.read_text(encoding="utf-8-sig")
    batches = split_sql_batches(text)

    offenders = [
        f"batch {index}: {batch.count('/*')} '/*' vs {batch.count('*/')} '*/'"
        for index, batch in enumerate(batches, 1)
        if batch.count("/*") != batch.count("*/")
    ]
    assert not offenders, (
        f"assets/{_relative(path)} does not survive the GO split: {offenders}. "
        "A `GO` inside a /* ... */ block cuts the comment in half and the server rejects the "
        "batch with \"Missing end comment mark '*/'\". Move the commented-out section out of the "
        "asset, or remove the GO inside it."
    )


def test_the_scanner_would_have_caught_the_failure_it_was_written_for() -> None:
    """A guard that cannot fail is not a guard — pin it against the exact shape that shipped."""
    broken = "SELECT 1;\n\n/* -- check\nUSE master;\nGO\nSELECT 2;\n*/\n"

    batches = split_sql_batches(broken)

    assert len(batches) > 1
    assert batches[0].count("/*") != batches[0].count("*/")
