"""The same function, class or constant is defined in exactly one place.

The rule, set on 2026-08-15: *no function and no file may exist in two places.* It came from a
real slip in the same session — a CLI transport copied into five app folders — but the scan that
followed found 40 more, nearly all of them older than that.

Duplication is not a style complaint here. Two of the copies in this tree already cost something:
six copies of ``queue_message.py`` had drifted into three behaviours before any of them shipped,
one having lost ``reply_message_id`` — the field a command reply needs — and nobody noticed until
a reply arrived blank. A definition in two files is a rule with two versions, and the second one
is found by the person debugging why the first one did not apply.

**``KNOWN_DUPLICATES`` is a baseline that shrinks, not an allowlist.** The test fails if a *new*
duplicate appears, and equally if an entry here has already been fixed — so finishing one means
deleting its line in the same commit, and the file empties out. An allowlist that only ever grows
is how a rule turns into decoration.

There is no length threshold, and that is deliberate: the shortest entry on the list is
``FULL = "full"``, spelled in three files, and it is the most dangerous one. It is the value the
retention rule matches to find the anchor of a backup chain — spell it differently in one place
and the pruner stops recognising full backups and deletes the chain it exists to protect. Length
is not a measure of how much a shared rule costs to get wrong.

The one exclusion is ``__all__``: an export list, equal by construction in a re-export shim.
"""

from __future__ import annotations

import ast
import collections
import hashlib
from pathlib import Path

import pytest


DB_OPS_ROOT = Path(__file__).resolve().parents[1] / "db_ops"

#: Names whose repetition carries no meaning.
IGNORED_NAMES = frozenset({"__all__"})

#: name -> the files that still define it identically. Measured 2026-08-15 at 2.85.19.
#: Every entry is work to do; none of it is permission.
KNOWN_DUPLICATES: dict[str, frozenset[str]] = {

}


def _relative(path: Path) -> str:
    return path.relative_to(DB_OPS_ROOT).as_posix()


def _definitions() -> dict[str, set[str]]:
    """``name -> files`` for every top-level definition that is byte-identical across files."""
    seen: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for path in DB_OPS_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken file fails louder elsewhere
            continue
        for node in tree.body:
            name, source = _named_source(node)
            if not name or name in IGNORED_NAMES:
                continue
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
            seen[(name, digest)].add(_relative(path))
    return {name: files for (name, _digest), files in seen.items() if len(files) > 1}


def _named_source(node: ast.AST) -> tuple[str, str]:
    """The name this statement binds and the source it binds it to, or ``("", "")``."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        # Unparsed, so formatting and comments do not hide a copy — and re-parsed through the
        # same path on both sides, so only the code itself is compared.
        return node.name, ast.unparse(node)
    target = None
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    if isinstance(target, ast.Name) and getattr(node, "value", None) is not None:
        return target.id, ast.unparse(node.value)
    return "", ""


def test_no_new_definition_is_duplicated() -> None:
    found = _definitions()
    new = sorted(
        f"{name}: {sorted(files)}"
        for name, files in found.items()
        if files - KNOWN_DUPLICATES.get(name, frozenset())
    )
    assert not new, (
        "these are defined identically in more than one file: " + "; ".join(new) +
        ". Define it once and import it — a rule with two copies is a rule with two versions, and "
        "the second is found by whoever is debugging why the first did not apply. Do not add it "
        "to KNOWN_DUPLICATES: that list only shrinks."
    )


def test_the_baseline_has_no_entries_that_are_already_fixed() -> None:
    """A name that is no longer duplicated must leave the list, or it stops measuring anything."""
    found = _definitions()
    stale = sorted(
        f"{name}: {sorted(files - found.get(name, set()))}"
        for name, files in KNOWN_DUPLICATES.items()
        if files - found.get(name, set())
    )
    assert not stale, (
        "KNOWN_DUPLICATES lists files that no longer duplicate these — delete the entries, the "
        f"cleanup moved on without them: {stale}"
    )


# --------------------------------------------------------------------------- #
# The same body under a different name — added 2026-08-16
# --------------------------------------------------------------------------- #
#
# The check above matches on **(name, body)**, so it sees a function copied verbatim and misses
# one copied and renamed. That is not a theoretical gap: `db/queue_message.py` held a private copy
# of "spawn a db_ops CLI, read JSON back" that differed from `lib/common_cli` only in the module
# string, and `lib` held the same four-line float parser three times as `to_number`, `_num` and
# `_float_or_none`. None of the five was visible here.
#
# Bodies are compared after dropping the docstring, so prose that explains *why* a copy exists
# does not hide it. Four lines is the floor: below that, two functions agreeing is usually the
# language, not a shared rule.

#: Minimum body length, in lines, before two identical functions are worth calling duplicates.
MIN_DUPLICATE_BODY_LINES = 4

#: body -> the ``file::name`` pairs that still share it. A shrinking baseline, like the one above.
#: **Empty**, and it was never allowed to be anything else for long: the seven groups this check
#: found on the day it was written were all closed within it.
#:
#: What they were, because the pattern is worth recognising — five of the seven were **dead code
#: or a rename**, not a hard refactor:
#:
#: * two pairs where one half was a dead twin (`mark_metric_daily_report_created` and
#:   `mark_daily_report_created_for_scope`, both writing `metric_results.daily_report_created`
#:   and both called by nothing) — deleting the dead half closed the pair;
#: * three copies of "a float, or None" inside `lib` alone, now `lib.coerce.as_float`;
#: * "parse a UTC timestamp" in `backup_restore` and `common`, now `lib.coerce.as_utc_datetime` —
#:   one deciding whether a backup is due, the other whether a restore drill counts;
#: * "decode bytes tolerantly" in `common` and `metrics`, now `lib.coerce.as_text`;
#: * "read a column the row may not have" in `reports`, `sla` **and** `telegram` — three copies
#:   that had already disagreed (two returned `""`, one `None`; one did not catch `TypeError`),
#:   now `lib.rows`;
#: * "announce a phase, and never let announcing break the run" — four copies in `backup_restore`,
#:   the fourth already drifted to different parameter names, now `events.announce`.
#:
#: The rename cases are the argument for this check existing: every one of them was invisible to
#: the name-based check above.
KNOWN_BODY_DUPLICATES: dict[str, frozenset[str]] = {

}


def _bodies() -> dict[str, set[str]]:
    """``body -> {file::name}`` for every function body shared by two or more definitions."""
    seen: dict[str, set[str]] = collections.defaultdict(set)
    for path in DB_OPS_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = list(node.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]          # the docstring is where the difference is explained
            if not body:
                continue
            text = "\n".join(ast.unparse(statement) for statement in body)
            if len(text.splitlines()) < MIN_DUPLICATE_BODY_LINES:
                continue
            seen[hashlib.sha256(text.encode()).hexdigest()].add(f"{_relative(path)}::{node.name}")
    return {digest: names for digest, names in seen.items() if len(names) > 1}


def test_no_new_function_body_is_duplicated_under_another_name() -> None:
    allowed = {frozenset(names) for names in KNOWN_BODY_DUPLICATES.values()}
    new = sorted(
        ", ".join(sorted(names)) for names in _bodies().values()
        if frozenset(names) not in allowed
    )
    assert not new, (
        "these functions have identical bodies under different names: " + "; ".join(new) +
        ". Define it once and import it. Do not add it to KNOWN_BODY_DUPLICATES: that list only "
        "shrinks."
    )


def test_the_body_baseline_has_no_entries_that_are_already_fixed() -> None:
    present = {frozenset(names) for names in _bodies().values()}
    stale = sorted(
        f"{reason}: {sorted(names)}"
        for reason, names in KNOWN_BODY_DUPLICATES.items()
        if frozenset(names) not in present
    )
    assert not stale, (
        "KNOWN_BODY_DUPLICATES lists groups that are no longer duplicated — delete the entries: "
        f"{stale}"
    )
