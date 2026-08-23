"""A deploy must not silently revert what the web console changed.

The store is shared between master and worker; ``data/`` is per node and is what the deploy
bundles. So a record edited in the console lands in the store and in the *worker's* files, while
the master's copy stays behind — and the next deploy from that master ships the old values back
over it, with a success message.

That is not hypothetical. On 2026-08-21 an operator set
``APP-REPORTS-INVENTORY-WORKFLOW.repeat_interval`` to 3600 in the console; the master's
``app_commands.json`` still said 7200, and a deploy at that moment would have reverted it.

So the deploy stops and asks. What these hold down:

* the drift is **detected**, and the report names the record, not just the file;
* **formatting-only** drift never stops a deploy — a prompt raised for nothing is a prompt people
  learn to click through;
* both answers actually **resolve** it, in opposite directions, and neither loses history;
* an unattended run with no declared answer **aborts**, because guessing which side is right is
  the one thing this gate must not do.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import write_catalogued_data

from db_ops.control import config_gate
from db_ops.db import config_sync
from db_ops.db.config_store import ConfigStore

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMAND = "APP-REPORTS-INVENTORY-WORKFLOW"


@pytest.fixture(scope="module")
def _template(tmp_path_factory):
    """One synced store and one settled ``data/``, built once and copied per test.

    Syncing 350-odd records twenty times over was most of this file's runtime. The export in here
    settles the hand-formatted files once, so a later assertion about drift is about the record
    that changed and not about a stray blank line.
    """
    root = tmp_path_factory.mktemp("drift-template")
    data = root / "data"
    write_catalogued_data(data)
    store_path = root / "template.sqlite"
    store = ConfigStore(store_path)
    config_sync.sync(store, data_dir=data, actor="setup")
    config_sync.export(store, data_dir=data)
    # Fold the write-ahead log into the file before it is copied; in WAL mode a fresh row can live
    # entirely in the -wal sidecar and a copy of the main file alone would arrive empty.
    checkpoint = sqlite3.connect(store_path)
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    return store_path, data


@pytest.fixture()
def estate(tmp_path: Path, _template):
    """A master whose ``data/`` and store agree, ready to be pulled apart."""
    template_store, template_data = _template
    store_path = tmp_path / "db_ops.sqlite"
    shutil.copy(template_store, store_path)
    data = tmp_path / "data"
    shutil.copytree(template_data, data)
    return {"store": ConfigStore(store_path), "data": data}


def read_file(data: Path) -> dict:
    return json.loads((data / "app_commands.json").read_text(encoding="utf-8"))


def interval_on_disk(data: Path) -> int:
    record = next(item for item in read_file(data)["app_commands"]
                  if item["app_command_id"] == COMMAND)
    return record["time_window"]["repeat_interval"]


def interval_in_store(store: ConfigStore) -> int:
    row = store.get_item(source_file="app_commands.json", collection="app_commands",
                         item_key=COMMAND)
    return json.loads(row["item_json"])["time_window"]["repeat_interval"]


def edit_in_console(estate, value: int) -> None:
    """What the web console does: write the store, and rewrite *its own* node's file."""
    from db_ops.db import config_edit

    row = estate["store"].get_item(source_file="app_commands.json", collection="app_commands",
                                   item_key=COMMAND)
    payload = json.loads(row["item_json"])
    payload["time_window"]["repeat_interval"] = value
    config_edit.save_record(estate["store"], source_file="app_commands.json",
                            collection="app_commands", payload=payload, item_key=COMMAND,
                            actor="thanh", data_dir=estate["data"])


def leave_master_behind(estate, value: int) -> None:
    """The master's file as it was before the console edit — the drift this gate is about."""
    payload = read_file(estate["data"])
    for item in payload["app_commands"]:
        if item["app_command_id"] == COMMAND:
            item["time_window"]["repeat_interval"] = value
    (estate["data"] / "app_commands.json").write_text(
        json.dumps(payload, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def test_a_master_in_step_with_the_store_has_no_drift(estate) -> None:
    assert config_gate.check(estate["store"], data_dir=estate["data"]) == []


def test_a_console_edit_the_master_has_not_taken_shows_as_drift(estate) -> None:
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)

    drifted = config_gate.check(estate["store"], data_dir=estate["data"])
    assert [item["file"] for item in drifted] == ["app_commands.json"]
    assert COMMAND in drifted[0]["detail"], "the report must name the record, not just the file"
    assert "changed" in drifted[0]["detail"]


def test_the_report_names_an_added_and_a_removed_record(estate) -> None:
    payload = read_file(estate["data"])
    payload["app_commands"] = [item for item in payload["app_commands"]
                               if item["app_command_id"] != COMMAND]
    (estate["data"] / "app_commands.json").write_text(
        json.dumps(payload, indent=4) + "\n", encoding="utf-8")

    detail = config_gate.check(estate["store"], data_dir=estate["data"])[0]["detail"]
    assert f"app_commands[{COMMAND}] added" in detail, (
        "the store has a record the file does not; from data/'s side that is an addition")


def test_formatting_alone_does_not_stop_a_deploy(estate) -> None:
    """A prompt raised for nothing is a prompt people learn to answer without reading.

    Key order is the case that actually happens: the store keeps a record in its file's order, and
    a file hand-edited into a different one differs byte for byte while meaning the same thing.
    Indentation is *not* such a case — the export reads the file's own indent and matches it.
    """
    path = estate["data"] / "app_commands.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["app_commands"][0] = dict(reversed(list(payload["app_commands"][0].items())))
    path.write_text(json.dumps(payload, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")

    assert config_sync.drift(estate["store"], data_dir=estate["data"]), "byte-level drift exists"
    assert config_gate.check(estate["store"], data_dir=estate["data"]) == [], (
        "but nothing an app would read has changed")


def test_the_indent_of_a_file_is_not_drift(estate) -> None:
    """The export writes back at the file's own indent, so its layout is never a reason to stop."""
    path = estate["data"] / "app_commands.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert config_sync.drift(estate["store"], data_dir=estate["data"]) == []


# --------------------------------------------------------------------------- #
# Resolving it
# --------------------------------------------------------------------------- #
def test_adopt_takes_the_consoles_values_into_the_master(estate) -> None:
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)

    result = config_gate.resolve(estate["store"], data_dir=estate["data"], decision="adopt",
                                 out=io.StringIO())
    assert result["decision"] == "adopt" and result["applied"] is True
    assert interval_on_disk(estate["data"]) == 3600
    assert interval_in_store(estate["store"]) == 3600
    assert config_gate.check(estate["store"], data_dir=estate["data"]) == []


def test_keep_ships_the_masters_files_and_re_syncs_the_store(estate) -> None:
    """"Keep" has to change the store too, or the same drift reappears on the next deploy."""
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)

    result = config_gate.resolve(estate["store"], data_dir=estate["data"], decision="keep",
                                 out=io.StringIO())
    assert result["decision"] == "keep"
    assert interval_on_disk(estate["data"]) == 7200
    assert interval_in_store(estate["store"]) == 7200
    assert config_gate.check(estate["store"], data_dir=estate["data"]) == []


def test_keep_does_not_lose_the_discarded_value(estate) -> None:
    """It is superseded on the record, not erased: the revision trail still holds 3600."""
    from db_ops.db.config_edit import record_history

    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)
    config_gate.resolve(estate["store"], data_dir=estate["data"], decision="keep",
                        out=io.StringIO())

    intervals = [entry["payload"]["time_window"]["repeat_interval"]
                 for entry in record_history(estate["store"], source_file="app_commands.json",
                                             collection="app_commands", item_key=COMMAND)]
    assert 3600 in intervals, "the console's value must still be readable in the history"
    assert intervals[0] == 7200, "and the current one is the master's"


def test_abort_changes_nothing_and_stops_the_deploy(estate) -> None:
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)

    with pytest.raises(config_gate.ConfigDriftAbort):
        config_gate.resolve(estate["store"], data_dir=estate["data"], decision="abort",
                            out=io.StringIO())
    assert interval_on_disk(estate["data"]) == 7200
    assert interval_in_store(estate["store"]) == 3600, "abort must not resolve anything either"


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #
def test_the_prompt_shows_what_differs_before_asking(estate) -> None:
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)
    out = io.StringIO()

    config_gate.resolve(estate["store"], data_dir=estate["data"], interactive=True,
                        ask=lambda prompt: "adopt", out=out)
    shown = out.getvalue()
    assert "CONFIG DRIFT" in shown
    assert "app_commands.json" in shown and COMMAND in shown
    assert "adopt" in shown and "keep" in shown and "abort" in shown


@pytest.mark.parametrize("answer,expected", [("adopt", 3600), ("keep", 7200)])
def test_the_typed_answer_decides(estate, answer: str, expected: int) -> None:
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)

    config_gate.resolve(estate["store"], data_dir=estate["data"], interactive=True,
                        ask=lambda prompt: answer + "\n", out=io.StringIO())
    assert interval_on_disk(estate["data"]) == expected


def test_an_empty_answer_is_abort_not_a_default_action(estate) -> None:
    """Pressing enter must not pick a side; both sides destroy somebody's change."""
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)

    with pytest.raises(config_gate.ConfigDriftAbort):
        config_gate.resolve(estate["store"], data_dir=estate["data"], interactive=True,
                            ask=lambda prompt: "", out=io.StringIO())


def test_nonsense_is_asked_again_and_then_aborts(estate) -> None:
    """A prompt that loops forever is a deploy that hangs a CI job."""
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)
    asked = []

    def reader(prompt):
        asked.append(prompt)
        return "maybe"

    with pytest.raises(config_gate.ConfigDriftAbort):
        config_gate.resolve(estate["store"], data_dir=estate["data"], interactive=True,
                            ask=reader, out=io.StringIO())
    assert len(asked) == 3


def test_an_unattended_run_with_no_declared_answer_aborts(estate) -> None:
    """Guessing which side is right is the one thing this gate must not do."""
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)

    with pytest.raises(config_gate.ConfigDriftAbort, match="no terminal"):
        config_gate.resolve(estate["store"], data_dir=estate["data"], interactive=False,
                            out=io.StringIO())


def test_an_unattended_run_may_declare_its_answer(estate) -> None:
    edit_in_console(estate, 3600)
    leave_master_behind(estate, 7200)

    config_gate.resolve(estate["store"], data_dir=estate["data"], decision="adopt",
                        interactive=False, out=io.StringIO())
    assert interval_on_disk(estate["data"]) == 3600


def test_no_drift_asks_nothing(estate) -> None:
    """The gate must be invisible on the ordinary deploy, or it becomes noise."""
    def reader(prompt):
        raise AssertionError("nothing to ask about")

    result = config_gate.resolve(estate["store"], data_dir=estate["data"], interactive=True,
                                 ask=reader, out=io.StringIO())
    assert result["decision"] == "none" and result["applied"] is False


def test_an_unknown_decision_is_refused(estate) -> None:
    with pytest.raises(config_gate.ConfigDriftAbort, match="must be one of"):
        config_gate.resolve(estate["store"], data_dir=estate["data"], decision="whatever",
                            out=io.StringIO())


# --------------------------------------------------------------------------- #
# The deploy actually calls it
# --------------------------------------------------------------------------- #


