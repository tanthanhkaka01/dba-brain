"""Lift one of the operator's configuration files into the `*.example.json` beside it.

Every file under `data/` has an example, and the examples **ship**: they are what a stranger copies
to get a working tool root, and what the documentation points at. The two therefore drift in one
direction — the operator's file gains records as the estate grows, and the example does not — and
the drift is invisible until somebody runs the shipped suite against the shipped examples and finds
the catalogue describes a tenth of the collectors the package carries.

It has been done by hand three times now: `telegram_support_commands.example.json` (5 records to
21), `metric_definitions.example.json` (4 to 10), and this module exists because the fourth time
was `metric_definitions` again, at 10 against 90. The rule fires exactly here — the moment
a task needs a throwaway script it belongs in a `common` command — and the reason is the one that
keeps proving itself: the hand-written lifts each got something different wrong. One invented four
variant filenames that did not exist and would have made the catalogue refuse to load; another
carried a server id in a prompt string nobody would think to read.

**Refusing is the whole design.** This does not scrub. It copies records and then runs the estate's
own identifier scan over the result, and if the result names anything real it writes nothing and
says which record and which term. A tool that quietly rewrote what it found would be a second
scrubber with its own opinions, and the project already decided there is one answer to that
question.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.common import identifier_scan
from db_ops.lib.paths import builtin_asset_root, resolve_tool_path

__all__ = ["LiftError", "lift_example", "referenced_files"]


class LiftError(RuntimeError):
    """The lift cannot be written, and the message says which record is at fault."""


#: Keys whose value is a path into the shipped assets. A lifted record naming a file that is not
#: there produces a catalogue that refuses to load, and the failure arrives at collection time on
#: somebody else's machine — so the paths are checked here, where the fix is obvious.
PATH_KEYS: tuple[str, ...] = ("path", "file", "script")


def referenced_files(payload: Any) -> list[str]:
    """Every asset path any record in *payload* names, in the order found."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in PATH_KEYS and isinstance(value, str) and value.strip():
                    found.append(value.strip())
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def blank_out(payload: Any, keys: tuple[str, ...]) -> int:
    """Empty every value stored under one of *keys*, anywhere in *payload*. Returns how many.

    For the fields that are *per-install by nature* rather than estate detail that leaked: a
    `report_base_url` is the address of whoever's worker serves the pages, and an example carrying
    one address is wrong for every reader rather than secret. Blanking is not scrubbing — the field
    stays, with the empty value that means "not set", which is what the reader has to fill in
    anyway.
    """
    emptied = 0

    def walk(node: Any) -> None:
        nonlocal emptied
        if isinstance(node, dict):
            for key, value in node.items():
                if key in keys and isinstance(value, str) and value:
                    node[key] = ""
                    emptied += 1
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return emptied


def lift_example(
    *,
    source: str | Path,
    dest: str | Path,
    tool_root: Path | None = None,
    blank_keys: tuple[str, ...] = (),
    write: bool = True,
) -> dict[str, Any]:
    """Copy *source* to *dest*, refusing anything that would carry the estate into it.

    Returns a summary rather than raising on a finding, because the caller is usually a person
    deciding what to fix — except for the two conditions that make the answer meaningless: an
    unreadable source, and a referenced file that does not exist.
    """
    source_path = Path(source)
    dest_path = Path(dest)
    if not source_path.is_file():
        raise LiftError(f"no such file: {source_path}")

    payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    emptied = blank_out(payload, tuple(blank_keys))

    missing = [
        reference
        for reference in referenced_files(payload)
        if not _resolves(reference, tool_root=tool_root)
    ]
    if missing:
        raise LiftError(
            f"{source_path} names {len(missing)} file(s) that do not exist, so the lifted example "
            f"would not load: {', '.join(sorted(set(missing))[:6])}"
        )

    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    # The estate's own terms, over the text that would be written. `scan` wants a tree, so the
    # candidate is written beside the destination first and removed unless it is clean.
    candidate = dest_path.with_suffix(".lift-candidate")
    candidate.write_text(rendered, encoding="utf-8")
    try:
        result = identifier_scan.scan({"root": str(candidate.parent), "paths": [candidate.name]})
        data = result.get("data", result)
        hits = int(data.get("hits") or 0)
        findings = data.get("files") or []
    finally:
        candidate.unlink(missing_ok=True)

    summary: dict[str, Any] = {
        "source": str(source_path),
        "dest": str(dest_path),
        "records": _record_count(payload),
        "referenced_files": len(referenced_files(payload)),
        "blanked": emptied,
        "identifier_hits": hits,
        "terms": sorted({term for f in findings for term in (f.get("terms") or [])})[:20],
        "written": False,
    }
    if hits:
        summary["message"] = (
            f"{hits} identifier(s) would be carried into {dest_path.name}. Nothing written. "
            "Fix the source, or drop the field that names them."
        )
        return summary

    if write:
        dest_path.write_text(rendered, encoding="utf-8")
        summary["written"] = True
    summary["message"] = (
        f"{summary['records']} record(s) lifted into {dest_path.name}; "
        f"{summary['referenced_files']} referenced file(s) all exist; no identifiers."
    )
    return summary


def _resolves(reference: str, *, tool_root: Path | None) -> bool:
    """Does *reference* name a file that exists, in either vocabulary these files use?

    Two are in play and both are correct in their own file. Configuration says
    `assets/backup/sqlserver/...`, which resolves through `resolve_tool_path` — the operator's tree
    first, the package's shipped copy second. The metric catalogue says `sqlserver/001_....sql`,
    relative to the collectors root, because a variant is always one of the shipped queries.

    Trying both is what keeps this a *lift* tool rather than a catalogue tool: it has no opinion
    about which file it is copying, only about whether the result would load.
    """
    if resolve_tool_path(reference, tool_root=tool_root).exists():
        return True
    # `lib.paths` owns where a shipped asset kind lives, so this asks it rather than importing
    # `metrics` — a `common` module that imports an app is the one thing ORD 13 forbids, and the
    # boundary test caught it the first time this function was written.
    collectors = builtin_asset_root("metrics")
    return bool(collectors) and (collectors / reference).exists()


def _record_count(payload: Any) -> int:
    """How many records the file holds — the longest list in it, which is what these files are."""
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return max((len(v) for v in payload.values() if isinstance(v, list)), default=0)
    return 0
