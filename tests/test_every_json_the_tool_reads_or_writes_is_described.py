"""Every JSON this tool reads or writes is described in the reference - once, under its own name.

The operator's rule, 2026-09-23: every config file in data/, every request a common.cli command
takes and every answer it gives must be in shared_config_objects.json; every object has its own id;
no two objects describe exactly the same fields. A reference that covers most of the tree is the
dangerous kind - it answers with confidence about the parts it has, and a reader cannot tell which
parts those are.

So the checks run both ways. Coverage: every catalogued config file, every key at every level
leading to a described record, every command's request and answer. Drift: every key a parser reads
from a request is a declared field (or its legacy spelling), and every declared field is a key its
module actually names - so a field added to the code without a description, or a description left
behind by a rename, fails here rather than in a reader's head.
"""

from __future__ import annotations

import ast
import collections
import importlib.util
import json
import re
from pathlib import Path

import pytest

from db_ops.lib import response, shared_objects

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
REFERENCE = shared_objects.load(DATA)
KIND_INPUT, KIND_OUTPUT = "input", "output"

#: Keys a file root may carry that document the file rather than configure anything.
DOCUMENT_KEYS = {"schema_version", "notes", "note", "description"}


def _entries(kind: str | None = None) -> list[dict]:
    return [e for e in REFERENCE if kind is None or e.get("kind") == kind]


def _commands() -> list[str]:
    spec = importlib.util.spec_from_file_location("contract", REPO / "tests" / "test_common_cli_json_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return sorted(set(module.ALL_COMMANDS) | set(module.STDIN_ONLY_COMMANDS))


# --------------------------------------------------------------------------- #
# One id each, one field set each
# --------------------------------------------------------------------------- #
def test_every_object_has_its_own_id():
    """`object` is the id. Two ids differing only by case are two names for a reader to confuse."""
    ids = [str(e.get("object") or "").strip() for e in REFERENCE]
    assert all(ids), "an entry with no id"
    counts = collections.Counter(i.lower() for i in ids)
    assert not [i for i, n in counts.items() if n > 1], counts.most_common(3)


def test_no_two_objects_describe_exactly_the_same_fields():
    """Two entries with one field set are one concept under two names - the thing this file ends."""
    by_fields = collections.defaultdict(list)
    for entry in REFERENCE:
        by_fields[frozenset(f["field"] for f in entry.get("fields") or [])].append(entry["object"])
    twins = [names for names in by_fields.values() if len(names) > 1]
    assert not twins, twins


# --------------------------------------------------------------------------- #
# Config files
# --------------------------------------------------------------------------- #
def _config_files() -> list[str]:
    for candidate in (DATA / "data_files.json", DATA / "data_files.example.json",
                      REPO / "db_ops" / "control" / "catalogue" / "data_files.json"):
        if candidate.is_file():
            listing = json.loads(candidate.read_text(encoding="utf-8-sig"))["data_files"]
            return sorted(str(f["file"]) for f in listing if f.get("kind") == "config")
    pytest.skip("no data_files catalogue in this tree")


def _described_paths(file_name: str) -> list[str]:
    return [str(site.get("path") or "") for e in REFERENCE for site in e.get("used_in") or []
            if site.get("file") == file_name]


def test_every_config_file_the_catalogue_names_is_described():
    missing = [f for f in _config_files() if not _described_paths(f)]
    assert not missing, f"config files the reference does not describe: {missing}"


def _steps(path: str) -> list[str]:
    return [part for part in path.split(".") if part]


def _undescribed_keys(document: dict, paths: list[str]) -> list[str]:
    """Keys on the way to a described record that no path names and no document key excuses."""
    if "" in paths:
        return []  # the file's root is itself a described record
    problems: list[str] = []

    def walk(node, label, remaining_paths):
        if not isinstance(node, dict):
            return
        next_steps = collections.defaultdict(list)
        for steps in remaining_paths:
            if steps:
                next_steps[steps[0]].append(steps[1:])
        for key, value in node.items():
            if str(key).startswith("_") or key in DOCUMENT_KEYS:
                continue
            matching = [s for s in next_steps if s.rstrip("[]{}") == key]
            if not matching:
                problems.append(f"{label}{key}")
                continue
            for step in matching:
                rest = next_steps[step]
                if any(not r for r in rest):
                    continue  # this step IS a described record; its own entry checks its keys
                children = (value if step.endswith("[]") and isinstance(value, list)
                            else list(value.values()) if step.endswith("{}") and isinstance(value, dict)
                            else [value])
                for child in children:
                    walk(child, f"{label}{key}.", rest)

    walk(document, "", [_steps(p) for p in paths])
    return problems


@pytest.mark.parametrize("suffix", [".json", ".example.json"])
def test_every_key_on_the_way_to_a_described_record_is_described(suffix):
    """A file's root and every block between it and a described record have no key the reference
    does not name. `backup_restore.copy_file_patterns` sat beside the described `backups[]` and
    `restores[]` - configuration nobody could look up."""
    gaps = {}
    for file_name in _config_files():
        path = DATA / file_name.replace(".json", suffix)
        if not path.is_file():
            continue
        problems = _undescribed_keys(json.loads(path.read_text(encoding="utf-8-sig")),
                                     _described_paths(file_name))
        if problems:
            gaps[path.name] = problems
    assert not gaps, gaps


# --------------------------------------------------------------------------- #
# Requests and answers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", [KIND_INPUT, KIND_OUTPUT])
def test_every_command_is_described_by_exactly_one_entry_of_each_kind(kind):
    claimed = collections.Counter(c for e in _entries(kind) for c in e.get("commands") or [] if c != "*")
    commands = _commands()
    missing = [c for c in commands if c not in claimed]
    twice = [c for c, n in claimed.items() if n > 1]
    unknown = [c for c in claimed if c not in commands]
    assert not (missing or twice or unknown), {"missing": missing, "twice": twice, "unknown": unknown}


def test_the_envelope_is_exactly_what_the_response_builder_writes():
    envelope = next(e for e in REFERENCE if e["object"] == "cli_response")
    assert {f["field"] for f in envelope["fields"]} == set(response.ok("x")) == set(response.fail("x", "e"))


def _text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def _named_in(field: str, modules: list[str]) -> bool:
    patterns = (f'"{field}"', f"'{field}'", "--" + field.replace("_", "-"))
    return any(p in _text(m) for m in modules for p in patterns)


@pytest.mark.parametrize("kind,modules_key", [(KIND_INPUT, "parsers"), (KIND_OUTPUT, "produced_in")])
def test_every_described_field_is_a_key_its_module_names(kind, modules_key):
    """A description left behind by a rename names a key no code reads or writes."""
    stale = {}
    for entry in _entries(kind):
        modules = entry.get(modules_key) or [entry["parsed_in"]]
        names = [f["field"] for f in entry["fields"]] + list(entry.get("legacy_fields") or {})
        gone = [n for n in names if not _named_in(n, modules)]
        if gone:
            stale[entry["object"]] = gone
    assert not stale, stale


def _request_keys(path: Path) -> set[str]:
    """Every key a module reads from a variable called `request` or `payload`."""
    keys: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "pop") and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("request", "payload") and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name) and node.value.id in ("request", "payload")
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            keys.add(node.slice.value)
    return keys


def test_every_key_a_request_parser_reads_is_a_described_field():
    """A request key added to the code without a description is a key nobody can look up."""
    declared = collections.defaultdict(set)
    for entry in _entries(KIND_INPUT):
        names = {f["field"] for f in entry["fields"]} | set(entry.get("legacy_fields") or {})
        for module in entry.get("parsers") or [entry["parsed_in"]]:
            declared[module] |= names
    undescribed = {module: sorted(_request_keys(REPO / module) - names)
                   for module, names in declared.items()}
    undescribed = {m: keys for m, keys in undescribed.items() if keys}
    assert not undescribed, undescribed


def test_a_legacy_request_spelling_names_a_field_of_its_own_entry():
    for entry in _entries(KIND_INPUT):
        fields = {f["field"] for f in entry["fields"]}
        for old, new in (entry.get("legacy_fields") or {}).items():
            assert new in fields and old not in fields, (entry["object"], old, new)
            assert re.fullmatch(r"[a-z0-9_]+", old), old
