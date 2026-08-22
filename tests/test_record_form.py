"""A generated config form must not lose a field or change a type.

The console draws a config record as a grid of named fields instead of raw JSON, and a generated
form is the classic way to quietly delete config: it renders the fields somebody thought of, the
operator saves, and everything else is gone. These records have no fixed shape — a SQL target
carries a nested ``time_window`` and a ``notify`` block, a metric definition carries per-``db_type``
``variants``, ``users.json`` carries a list of credential objects — so "the fields somebody thought
of" was never going to be all of them.

The defence is one property, checked against **every record in ``data/``**:

    rebuild(flatten(record)) == record

Equality here is exact, not approximate: `0` must not come back as `"0"`, `false` must not become
`"false"`, `null` must not become `""`, and an empty string must stay one. Each of those confusions
is a live config change nobody asked for — a `repeat_interval` of `"120"` is not a schedule, and a
`null` that became `""` is a filter that now matches nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import shipped_data_dir
from db_ops.db import config_sync
from db_ops.lib import record_form as rf

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The shipped configuration set — the operator's `data/` where there is one, the examples renamed
#: to the names they are examples of otherwise. Read at import, because the sweep below parametrizes
#: over every record in every catalogued file and a parametrize argument cannot ask for a fixture.
#: On a clean checkout that import used to raise, so this file was one of the two that failed at
#: *collection* and took the whole suite down before a test ran.
DATA_DIR = shipped_data_dir()


def submit(layout: rf.Layout) -> dict[str, list[str]]:
    """What a browser would post for this layout, in ``parse_qs`` shape.

    The checkbox pair is modelled exactly as the page emits it — a hidden ``false`` followed by the
    box's own ``true`` when ticked — because that ordering is the whole reason the parser reads the
    last value, and a test that posted one value would not exercise it.
    """
    posted: dict[str, list[str]] = {}
    for row in layout.rows:
        if isinstance(row, rf.Section):
            if row.empty:
                posted[row.name] = [""]
            continue
        if row.kind == rf.KIND_BOOL:
            posted[row.name] = ["false"] + (["true"] if row.value else [])
        else:
            posted[row.name] = [row.text]
    return posted


def round_trip(record: dict) -> dict:
    return rf.rebuild(submit(rf.flatten(record)))


def every_record() -> list[tuple[str, str, dict]]:
    """Every record and every file-settings document under ``data/``, with where it came from."""
    found: list[tuple[str, str, dict]] = []
    for spec in config_sync.load_catalog(DATA_DIR):
        path = DATA_DIR / spec.file
        if not path.is_file():
            continue
        payload = config_sync.read_json_file(path)
        found.append((spec.file, "__document__", payload))
        for collection in spec.collections:
            for index, record in enumerate(payload.get(collection.collection) or []):
                if isinstance(record, dict):
                    found.append((spec.file, f"{collection.collection}[{index}]", record))
    return found


# --------------------------------------------------------------------------- #
# The property, against the real estate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("source_file,where,record", every_record(),
                         ids=lambda value: value if isinstance(value, str) else "")
def test_every_real_record_survives_the_form(source_file: str, where: str, record: dict) -> None:
    assert round_trip(record) == record, f"{source_file}:{where} changed on a no-op save"


def test_the_sweep_reaches_every_catalogued_file() -> None:
    """A guard that silently matched nothing would pass forever.

    The floor used to be `> 300`, which is how many records *this operator* happens to have. That
    made it an estate assertion wearing a coverage assertion's clothes: it failed on a checkout
    holding only the shipped examples, where the same sweep works perfectly and finds 95. What the
    check is actually for is that the catalog is being walked and files are being opened, so it
    now says that — a floor low enough for the examples to clear, and the file count, which is the
    part that would collapse if the walk broke.
    """
    records = every_record()
    assert len(records) > 50, f"only {len(records)} records swept; the walk is broken"
    assert len({source_file for source_file, _, _ in records}) > 15, (
        "the sweep is reading too few files for the catalog walk to be working"
    )


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [0, 1, -5, 120, 3.5, -0.25, True, False, None, "", "text",
                                   "0", "false", "null"])
def test_a_scalar_comes_back_as_itself(value) -> None:
    """The confusable cases, spelled out: "0", "false" and "null" are strings and must stay so."""
    result = round_trip({"field": value})
    assert result == {"field": value}
    assert type(result["field"]) is type(value)


def test_a_number_typed_as_text_is_refused_rather_than_stored() -> None:
    """`repeat_interval: "soon"` is not a schedule; the save has to stop, naming the field."""
    layout = rf.flatten({"time_window": {"repeat_interval": 120}})
    posted = submit(layout)
    key = next(name for name in posted if "repeat_interval" in name)
    posted[key] = ["soon"]
    with pytest.raises(rf.RecordFormError, match="time_window.repeat_interval"):
        rf.rebuild(posted)


def test_an_emptied_number_becomes_null_not_zero() -> None:
    """Zero is a real interval. Clearing a box means "unset", and turning it into 0 would run."""
    layout = rf.flatten({"repeat_interval": 120})
    posted = submit(layout)
    posted[next(iter(posted))] = [""]
    assert rf.rebuild(posted) == {"repeat_interval": None}


def test_a_null_field_stays_null_unless_something_is_typed() -> None:
    assert round_trip({"from_minute": None}) == {"from_minute": None}
    layout = rf.flatten({"from_minute": None})
    posted = submit(layout)
    posted[next(iter(posted))] = ["30"]
    assert rf.rebuild(posted) == {"from_minute": "30"}, (
        "a value typed into a null field becomes a string; guessing the type would be worse")


def test_an_unticked_checkbox_reads_as_false() -> None:
    """The hidden companion field is what makes this work; without it the field would vanish."""
    layout = rf.flatten({"active": True})
    posted = submit(layout)
    posted[next(iter(posted))] = ["false"]
    assert rf.rebuild(posted) == {"active": False}


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #
def test_a_nested_object_becomes_a_section_with_its_own_rows() -> None:
    layout = rf.flatten({"name": "x", "time_window": {"repeat_interval": 60, "timeout": 300}})
    sections = [row for row in layout.rows if isinstance(row, rf.Section)]
    assert [section.label for section in sections] == ["time_window"]
    assert [field.label for field in layout.fields] == ["name", "repeat_interval", "timeout"]
    assert [field.depth for field in layout.fields] == [0, 1, 1]


def test_an_empty_object_survives_having_no_fields() -> None:
    """It has no leaves to carry it, so the section row posts a marker of its own."""
    assert round_trip({"metric_overrides": {}}) == {"metric_overrides": {}}


def test_a_list_of_scalars_is_one_box_one_item_per_line() -> None:
    layout = rf.flatten({"db_types": ["sqlserver", "postgresql"]})
    field = layout.fields[0]
    assert field.is_list and field.text == "sqlserver\npostgresql"
    assert round_trip({"db_types": ["sqlserver", "postgresql"]}) == {
        "db_types": ["sqlserver", "postgresql"]}


def test_a_list_of_numbers_keeps_its_numbers() -> None:
    assert round_trip({"ports": [1433, 5432]}) == {"ports": [1433, 5432]}


def test_blank_lines_in_a_list_are_dropped() -> None:
    """Trailing newlines are what a textarea produces; they must not become empty entries."""
    layout = rf.flatten({"codes": ["A", "B"]})
    posted = submit(layout)
    posted[next(iter(posted))] = ["A\n\nB\n"]
    assert rf.rebuild(posted) == {"codes": ["A", "B"]}


def test_an_empty_list_stays_an_empty_list() -> None:
    assert round_trip({"disabled_collector_types": []}) == {"disabled_collector_types": []}


def test_a_list_of_objects_keeps_a_json_box_for_that_field_only() -> None:
    """Lossless and local: one field's worth of JSON, not the whole record's."""
    record = {"name": "x", "credentials": [{"username": "a"}, {"username": "b"}]}
    layout = rf.flatten(record)
    kinds = {field.label: field.kind for field in layout.fields}
    assert kinds["credentials"] == rf.KIND_JSON
    assert kinds["name"] == rf.KIND_STR
    assert round_trip(record) == record


def test_malformed_json_in_a_subtree_names_the_field() -> None:
    layout = rf.flatten({"variants": [{"sql": "x"}]})
    posted = submit(layout)
    posted[next(iter(posted))] = ["{not json"]
    with pytest.raises(rf.RecordFormError, match="variants"):
        rf.rebuild(posted)


def test_key_order_is_preserved() -> None:
    """The store keeps a record in its file's key order; a form that reordered it reorders the file."""
    record = {"z_last": 1, "a_first": 2, "m_middle": 3}
    assert list(round_trip(record)) == ["z_last", "a_first", "m_middle"]


def test_a_key_containing_a_dot_lands_where_it_belongs() -> None:
    """The path travels as JSON precisely so a dotted key needs no escaping scheme."""
    record = {"limits": {"cpu.max": 90, "mem.max": 80}}
    assert round_trip(record) == record


def test_fields_from_another_form_are_ignored() -> None:
    """The CSRF token and the buttons post alongside the fields and are not part of the record."""
    layout = rf.flatten({"name": "x"})
    posted = submit(layout)
    posted["csrf"] = ["a-token"]
    posted["note"] = ["why I changed it"]
    assert rf.rebuild(posted) == {"name": "x"}


def test_a_grid_submission_is_told_apart_from_a_json_one() -> None:
    """Which editor was used is read off the submission, never from a mode flag that can disagree."""
    assert rf.has_form_fields(submit(rf.flatten({"name": "x"}))) is True
    assert rf.has_form_fields({"payload": ['{"name": "x"}'], "csrf": ["t"]}) is False
