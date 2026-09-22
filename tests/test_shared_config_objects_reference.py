"""`data/shared_config_objects.json` describes the code, so it must be checked against the code.

The file is reference, not configuration: nothing an app decides reads it, and editing it changes
nothing. `data/README.md` is explicit that a file nothing acts on is a trap — `notify_levels.json`
and `metric_groups.json` were deleted for exactly that. The one thing that earns this one its place
is that it cannot quietly go stale: a field added to `TimeWindow`, a rule name added to `notify` or
a key added to `cmd_access` fails here until the reference names it too.

It also has three copies by the folder's own conventions — the packaged seed `init` writes from, the
master's live file, and the example that ships in the public tree — so the last test asserts the
three are the same bytes. Reference that differs per node is not reference.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.lib import cleanup_retention, shared_objects
from db_ops.lib.cmd_access import CMD_ACCESS_FIELDS, SUPPORTED_CMD_ACCESS_METHODS
from db_ops.lib.notify import NOTIFY_RULE_NAMES, NotifyRule
from db_ops.lib.sql_access import (
    SECRET_REF_FIELDS,
    SQL_ACCESS_FIELDS,
    SUPPORTED_SQL_ACCESS_METHODS,
)
from db_ops.lib.task_output import MAX_INLINE_MAX_ROWS, OUTPUT_FIELDS, OUTPUT_FORMATS
from db_ops.lib.time_window import NEW_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[1]
COPIES = (
    REPO_ROOT / "db_ops" / "common" / "catalogue" / "shared_config_objects.json",
    REPO_ROOT / "data" / "shared_config_objects.json",
    REPO_ROOT / "data" / "shared_config_objects.example.json",
)


@pytest.fixture(scope="module")
def reference() -> list[dict]:
    return shared_objects.load(REPO_ROOT / "data")


def _fields(reference: list[dict], name: str) -> list[str]:
    for item in reference:
        if item.get("object") == name:
            return [str(entry.get("field")) for entry in item.get("fields") or []]
    raise AssertionError(f"the reference does not describe {name}")


# --------------------------------------------------------------------------- #
# Every described object exists, and every shared object is described
# --------------------------------------------------------------------------- #
def test_the_eleven_shared_objects_are_described_and_nothing_else_is(reference):
    """Eight: six objects, one shared field, and one whole record.

    ``app_command`` was added 2026-09-21, and it is the first entry that describes a *record* rather
    than a block appearing inside many of them. It carries ``kind: "field"`` anyway, because that is
    the kind whose declared names ``check_data_dir`` looks for directly **on** the record — a
    ``"record"`` kind would send it looking for a nested block called ``app_command``, find none, and
    certify clean for ever.
    """
    assert [item.get("object") for item in reference] == [
        "time_window", "notify", "notify_rule", "output", "cmd_access", "sql_access",
        "cleanup_retention", "app_command",
    "backup_entry", "backup_job", "restore_entry"]


def test_every_entry_states_its_shape_before_a_reader_has_to_count(reference):
    for item in reference:
        fields = item.get("fields") or []
        assert item.get("field_count") == len(fields), item.get("object")
        required = [entry for entry in fields if entry.get("required")]
        assert item.get("required_field_count") == len(required), item.get("object")
        assert item.get("one_line"), item.get("object")
        assert Path(REPO_ROOT / str(item.get("parsed_in"))).is_file(), item.get("parsed_in")


def test_every_field_says_what_it_is_measured_against(reference):
    """The question the whole file exists to answer. An empty one is a field nobody described."""
    for item in reference:
        for entry in item.get("fields") or []:
            for key in ("type", "range_text", "default", "measured_against", "purpose"):
                assert str(entry.get(key) or "").strip(), f"{item['object']}.{entry.get('field')}.{key}"


# --------------------------------------------------------------------------- #
# The reference against the code it describes
# --------------------------------------------------------------------------- #
def test_time_window_lists_exactly_the_fourteen_fields_the_parser_accepts(reference):
    assert _fields(reference, "time_window") == list(NEW_FIELDS)


def test_the_three_intervals_are_the_ones_measured_from_the_previous_start(reference):
    """The correction of 2026-09-19, held in the file a person is pointed at."""
    intervals = {
        entry["field"]: entry["measured_against"]
        for entry in next(i for i in reference if i["object"] == "time_window")["fields"]
        if entry["field"] in {"repeat_interval", "retry_interval", "timeout"}
    }
    assert len(intervals) == 3
    for field, measured in intervals.items():
        assert "START" in measured, f"{field} is documented as measured from {measured!r}"


def test_notify_lists_its_two_rules_and_notify_rule_its_three_fields(reference):
    assert _fields(reference, "notify") == list(NOTIFY_RULE_NAMES)
    assert _fields(reference, "notify_rule") == [
        field.name for field in NotifyRule.__dataclass_fields__.values()]


def test_output_lists_the_four_keys_its_parser_reads_and_the_formats_it_accepts(reference):
    assert _fields(reference, "output") == list(OUTPUT_FIELDS)
    entry = next(item for item in reference if item["object"] == "output")
    fields = {field["field"]: field for field in entry["fields"]}
    assert fields["format"]["constraint"]["enum"] == list(OUTPUT_FORMATS)
    assert fields["max_rows"]["constraint"]["max"] == MAX_INLINE_MAX_ROWS


def test_cmd_access_lists_every_key_the_resolver_reads(reference):
    assert _fields(reference, "cmd_access") == list(CMD_ACCESS_FIELDS)


def test_cmd_access_method_names_the_three_methods_and_nothing_else(reference):
    entry = next(item for item in reference if item["object"] == "cmd_access")
    method = next(f for f in entry["fields"] if f["field"] == "method")
    assert set(method["constraint"]["enum"]) == SUPPORTED_CMD_ACCESS_METHODS
    assert method["required"] is True
    # `platform` looks like it belongs in this block and is ignored there, which is worth a line
    # in the reference rather than a surprise in the field list.
    assert "platform" in (entry.get("not_a_field") or {})


def test_sql_access_lists_every_key_its_readers_use(reference):
    """Six, not four. `mode` and `timeout_seconds` are read by `common.oracle_bridge` and
    `metrics.executor`, were in no declared list at all, and 7 of this estate's instances set them
    — found by running `check-objects` against the reference's first draft, which described four."""
    fields = _fields(reference, "sql_access")
    assert fields == list(SQL_ACCESS_FIELDS)
    assert set(SECRET_REF_FIELDS) <= set(fields)
    entry = next(item for item in reference if item["object"] == "sql_access")
    method = next(f for f in entry["fields"] if f["field"] == "method")
    assert set(method["constraint"]["enum"]) == SUPPORTED_SQL_ACCESS_METHODS


def test_cleanup_retention_lists_the_canonical_name_and_both_legacy_spellings(reference):
    assert _fields(reference, "cleanup_retention") == [
        cleanup_retention.FIELD, *cleanup_retention.LEGACY_FIELDS]
    entry = next(item for item in reference if item["object"] == "cleanup_retention")
    canonical = entry["fields"][0]
    assert canonical["required"] is True
    assert "SECONDS" in canonical["range_text"], "the unit is the whole point of the rename"


# --------------------------------------------------------------------------- #
# The file is answerable, and it is the same everywhere
# --------------------------------------------------------------------------- #
def test_one_field_can_be_asked_for_by_name_the_way_the_bot_asks():
    answer = shared_objects.describe("time-window", data_dir=REPO_ROOT / "data",
                                     field="retry_interval")
    assert answer["object"] == "time_window"
    assert answer["field"] == "retry_interval"


def test_an_unknown_name_says_what_there_is_instead_of_raising_a_key_error():
    with pytest.raises(shared_objects.SharedObjectError) as excinfo:
        shared_objects.describe("time_windows", data_dir=REPO_ROOT / "data")
    assert "time_window" in str(excinfo.value)


def test_all_three_copies_are_the_same_bytes():
    """The packaged seed, the master's file and the public example. They carry no estate data, so
    a difference between them is drift rather than configuration.

    A public checkout has no `data/shared_config_objects.json` — only the example — so the copies
    that exist are the ones compared, and at least two must. Requiring all three made this red on
    every public tree, which is how v0.20.0 came to be released with a failing CI.
    """
    contents = {path: path.read_bytes() for path in COPIES if path.is_file()}
    assert len(contents) >= 2, f"only {[p.name for p in contents]} is present; nothing to compare"
    first, *rest = list(contents.items())
    for path, payload in rest:
        assert payload == first[1], (
            f"{path.relative_to(REPO_ROOT)} differs from {first[0].relative_to(REPO_ROOT)}. "
            "Edit one and copy it to the other two - reference is identical on every node.")


def test_it_is_valid_json_with_the_schema_version_every_data_file_carries():
    # The packaged seed, which is the one copy every checkout has: a public tree ships the example
    # and no `data/shared_config_objects.json`.
    payload = json.loads(COPIES[0].read_text(encoding="utf-8-sig"))
    assert payload["schema_version"] == 2
    assert payload["notes"], "the notes are where the rules a field table cannot hold are written"

# --------------------------------------------------------------------------- #
# The constraints are machine-readable, and they are checked against this estate
# --------------------------------------------------------------------------- #
def test_every_field_carries_a_constraint_a_program_can_evaluate(reference):
    """`range_text` is for a person and cannot be tested. `constraint` is the testable half, and a
    field with only the prose is a field nothing can hold the estate to."""
    kinds = {"integer", "string", "boolean", "object", "array"}
    for item in reference:
        for entry in item.get("fields") or []:
            constraint = entry.get("constraint")
            where = f"{item['object']}.{entry.get('field')}"
            assert isinstance(constraint, dict), f"{where} has no constraint"
            assert constraint.get("kind") in kinds, f"{where}: kind={constraint.get('kind')!r}"
            if constraint["kind"] == "integer":
                low, high = constraint.get("min"), constraint.get("max")
                assert low is not None or high is not None, f"{where}: an integer with no bound"
                if low is not None and high is not None:
                    assert int(low) <= int(high), where
            if constraint["kind"] == "object":
                # Three ways an object constraint is evaluable, each saying something different.
                # `object` names the entry to recurse into. `checked_by` names the entry that
                # already walks this path, so recursing would report one mistake twice.
                # `free_form` says the CONTENTS are deliberately not described because the keys are
                # the operator's own - a backup job's `env`, a restore's `target` - so only the
                # shape can be checked. Refused is an object constraint that says none of the
                # three, because nothing can act on it.
                if not (constraint.get("checked_by") or constraint.get("free_form")):
                    assert constraint.get("object") in {i["object"] for i in reference}, where


def test_the_paths_it_declares_exist_in_the_files_it_names(reference):
    """`used_in` is what `check-objects` walks, so a path that resolves to nothing is a check that
    silently passes. Both of the backup paths did exactly that until 2026-09-19: they said
    `backups[]` and the schedule lives on `backups[].jobs[]`."""
    for item in reference:
        for site in item.get("used_in") or []:
            file_name = str(site.get("file") or "")
            if not file_name:
                continue
            # The same fallback `check_data_dir` makes: a public checkout ships only the
            # `*.example.json`, and a guard that cannot read what the code reads is a guard that
            # fails for the wrong reason.
            path = REPO_ROOT / "data" / file_name
            if not path.is_file():
                path = REPO_ROOT / "data" / file_name.replace(".json", ".example.json")
            assert path.is_file(), f"{item['object']} names a file that is not there: {file_name}"
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            records = shared_objects.walk_records(payload, str(site.get("path")))
            assert records, (
                f"{item['object']}: {file_name}:{site.get('path')} matches no record, so nothing "
                "at that path is ever checked")


def test_this_estate_obeys_its_own_reference():
    """The check that would have caught six restore entries with no retention, and the one that
    found `sql_access.mode` in use and described nowhere."""
    result = shared_objects.check_data_dir(REPO_ROOT / "data")
    assert result["objects_checked"] > 0, "nothing was checked, which is not the same as passing"
    assert result["violations"] == [], "\n".join(
        f"{item['kind']} {item['where']}.{item['field']}: {item['detail']}"
        for item in result["violations"])


def test_the_shipped_examples_obey_it_too():
    """A public checkout has only the examples, and they are what a new operator copies. One that
    violates the reference teaches the mistake."""
    result = shared_objects.check_data_dir(REPO_ROOT / "data")
    assert result["files_missing"] == []


# --------------------------------------------------------------------------- #
# The three kinds of violation, one test each
# --------------------------------------------------------------------------- #
def _violations(object_name: str, record: dict) -> list[dict]:
    return shared_objects.check_record(object_name, record, where="test",
                                       data_dir=REPO_ROOT / "data")


def test_a_required_field_that_is_absent_is_reported_as_missing():
    findings = _violations("cleanup_retention", {"cleanup_retention": None})
    assert [item["kind"] for item in findings] == ["value"]
    findings = _violations("cleanup_retention", {})
    assert [item["kind"] for item in findings] == ["missing"]
    assert findings[0]["field"] == "cleanup_retention"


def test_a_legacy_spelling_satisfies_the_requirement_rather_than_reading_as_absent():
    """`retention_days` still loads, so refusing it would report a working file as broken. It is
    described as a field of its own — with its unit, which is DAYS where the canonical one is
    seconds — so it is neither missing nor unknown."""
    assert _violations("cleanup_retention", {"retention_days": 8}) == []
    assert _violations("cleanup_retention", {"target_retention_seconds": 691200}) == []
    # A name that is neither the canonical field nor a spelling the parser reads:
    assert [item["kind"] for item in _violations("cleanup_retention", {"retention_hours": 8})] == [
        "missing", "unknown"]


def test_an_hour_outside_0_to_23_is_reported_with_the_bound_it_broke():
    findings = _violations("time_window", {"from_hour": 24})
    assert findings[0]["kind"] == "value"
    assert "must be <= 23" in findings[0]["detail"]


def test_a_repeat_interval_below_minus_one_is_a_typo_not_a_convention():
    assert _violations("time_window", {"repeat_interval": -2})[0]["detail"].startswith(
        "must be >= -1")
    assert _violations("time_window", {"repeat_interval": -1}) == []
    assert _violations("time_window", {"repeat_interval": 0}) == []


def test_a_boolean_in_an_interval_is_refused_rather_than_read_as_one():
    """`True == 1` in Python, so a checker that accepts it turns a schedule into a one-second loop
    and reports the file as correct."""
    assert _violations("time_window", {"repeat_interval": True})[0]["kind"] == "value"


def test_a_misspelled_field_is_reported_because_the_parser_would_ignore_it_in_silence():
    findings = _violations("time_window", {"to_hours": 23})
    assert [item["kind"] for item in findings] == ["unknown"]
    assert "to_hour" in findings[0]["detail"]


def test_an_underscore_prefixed_key_is_a_note_and_not_an_unknown_field():
    assert _violations("time_window", {"_note": "why this schedule is what it is"}) == []


def test_an_unknown_method_is_refused_but_an_unknown_shell_is_only_reported():
    """The difference `open_enum` draws: three methods are all there will ever be, while a host may
    have a shell this list has not heard of."""
    refused = _violations("cmd_access", {"method": "telnet"})
    assert [item["kind"] for item in refused] == ["value"]
    reported = _violations("cmd_access", {"method": "ssh", "shell": "zsh"})
    assert [item["kind"] for item in reported] == ["unlisted"]


def test_a_password_in_a_config_file_is_a_violation_whatever_it_says():
    findings = _violations("cmd_access", {"method": "ssh", "password": "hunter2"})
    assert [item["kind"] for item in findings] == ["value"]
    assert "never appear in a config file" in findings[0]["detail"]


def test_notify_is_checked_through_to_its_rules():
    findings = _violations("notify", {"logging_on_run": {"enabled": True, "telegram_chat": 5}})
    assert findings and findings[0]["where"].endswith("logging_on_run")
    assert findings[0]["field"] == "telegram_chat"
    # And the legacy boolean form still parses, so it is not a finding.
    assert _violations("notify", {"alert_on_error": True}) == []


def test_a_bridge_url_is_required_only_when_the_method_asks_for_one():
    assert _violations("sql_access", {"method": "direct"}) == []
    findings = _violations("sql_access", {"method": "api", "bridge_url": ""})
    assert [item["kind"] for item in findings] == ["missing"]
