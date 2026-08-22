"""`db-ops init` is the first command anybody runs, and until 2026-08-22 it did not exist.

Measured on a real `pip install` into a clean virtualenv, from an empty directory: the toolkit
resolved its configuration to `site-packages` and told the reader to create a file there from an
example that does not ship. **Installable and unstartable** — which made the resolution order in
`db_ops.lib.paths` correct and useless.

These tests pin the properties that make the scaffold worth having, and every one of them is
something a reader hits in the first five minutes:

- it produces a tree that **actually runs**, not one that merely parses;
- it starts on **SQLite**, because a first run has no PostgreSQL and asking someone to install a
  database to hold the results of monitoring a database is a poor first request;
- it **never overwrites**, because the files it writes are the ones you edit immediately after;
- every file it writes **says what to put in it**, because the supported way in today is an AI
  agent editing JSON, and an agent that has to guess a schema will guess wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops import scaffold
from db_ops.lib.paths import builtin_asset_root


@pytest.fixture
def root(tmp_path: Path) -> Path:
    scaffold.initialise(tmp_path / "toolroot")
    return tmp_path / "toolroot"


def test_the_scaffold_is_a_tool_root_the_resolver_recognises(root: Path) -> None:
    """`config.json` and `data/` are the markers. Without one, standing here means nothing."""
    from db_ops.lib.paths import looks_like_tool_root

    assert looks_like_tool_root(root)


def test_every_file_it_writes_is_valid_json(root: Path) -> None:
    """A scaffold that needs fixing before it parses is worse than no scaffold."""
    for path in sorted(root.rglob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))


def test_the_store_starts_on_sqlite(root: Path) -> None:
    """The decision, not a convenience: a first run has nothing else installed."""
    store = json.loads((root / "data" / "store_config.json").read_text(encoding="utf-8"))

    assert store["backend"] == "sqlite"
    assert store["sqlite"]["connection_string"].startswith("sqlite:///")
    assert "runtime/" in store["sqlite"]["path"]


def test_the_postgresql_block_is_present_but_not_live(root: Path) -> None:
    """Moving later must be an edit, not a discovery of which fields exist."""
    store = json.loads((root / "data" / "store_config.json").read_text(encoding="utf-8"))

    assert store["backend"] != "postgresql"
    for field in ("host", "port", "database", "schema", "username", "password_ref"):
        assert field in store["postgresql"]


def test_the_inventory_starts_empty_and_explains_itself(root: Path) -> None:
    """The empty list is the point: nothing is monitored until somebody says what to monitor."""
    inventory = json.loads((root / "data" / "db_instances.json").read_text(encoding="utf-8"))

    assert inventory["db_instances"] == []
    notes = " ".join(inventory["notes"])
    for field in ("server_id", "db_type", "major_version", "default_credential_name"):
        assert field in notes, f"an agent cannot guess {field}; the file has to say"


def test_the_inventory_warns_that_service_name_is_not_a_database(root: Path) -> None:
    """The mistake that fails *every* SQL Server target at once, with error 4060.

    It is enforced in `metrics/executor.py` rather than left to configuration, and the file a
    person edits is where the warning has to be, because that is where the mistake is made.
    """
    notes = " ".join(json.loads(
        (root / "data" / "db_instances.json").read_text(encoding="utf-8"))["notes"])

    assert "label" in notes.lower() and "master" in notes


def test_the_starter_catalogue_names_queries_that_exist(root: Path) -> None:
    """A catalogue naming a missing file refuses to load, and the first run collects nothing.

    Checked against the *package*, because that is where the queries are once installed — this is
    the same class of error that shipped four invented filenames in `metric_definitions.example.json`
    and was found only when something finally loaded it.
    """
    catalogue = json.loads((root / "data" / "metric_definitions.json").read_text(encoding="utf-8"))
    metrics_root = builtin_asset_root("metrics")

    assert catalogue["metrics"], "a first run with no metrics collects nothing"
    for metric in catalogue["metrics"]:
        for variant in metric["variants"]:
            assert (metrics_root / variant["file"]).exists(), (
                f"{metric['metric_code']} names {variant['file']}, which the package does not ship"
            )


def test_the_starter_catalogue_loads_through_the_real_loader(root: Path) -> None:
    """Parsing is not loading. The loader validates fields the JSON alone cannot."""
    from db_ops.metrics.definitions import load_metric_definitions

    definitions = load_metric_definitions(root / "data" / "metric_definitions.json")

    assert {d.metric_code for d in definitions} >= {"INSTANCE_STATUS", "BACKUP_AGE"}


def test_telegram_is_written_but_off(root: Path) -> None:
    """Present so it can be found and edited; off so nothing is sent by a first run.

    A toolkit that delivers somewhere on its first collection is one nobody can try safely.
    """
    telegram = json.loads((root / "data" / "telegram_config.json").read_text(encoding="utf-8"))

    assert telegram["enabled"] is False
    # The ref, not just the env var name. A send with only `bot_token_env` set fails with
    # "Telegram bot token is empty", which names the symptom and not the missing field — found by
    # sending a real message rather than by reading the config.
    assert telegram["telegram_bot_token_ref"]
    assert telegram["bot_token_env"]


def test_the_agent_guide_is_written_beside_the_json_it_describes(root: Path) -> None:
    """A guide in a repository the agent never cloned is a guide it will not read."""
    guide = (root / "AGENTS.md").read_text(encoding="utf-8")

    assert "db_instances.json" in guide
    assert "encrypt-secret-text" in guide
    assert "--dry-run" in guide
    assert "sqlite" in guide.lower()


def test_a_second_init_keeps_what_you_edited(tmp_path: Path) -> None:
    """The files it writes are the ones you edit next, so a silent reset destroys the only work."""
    target = tmp_path / "toolroot"
    scaffold.initialise(target)
    edited = target / "data" / "db_instances.json"
    edited.write_text('{"db_instances": [{"server_id": "MINE"}]}', encoding="utf-8")

    result = scaffold.initialise(target)

    assert "MINE" in edited.read_text(encoding="utf-8")
    assert "data/db_instances.json" in result.skipped
    assert not result.written


def test_force_overwrites_when_asked(tmp_path: Path) -> None:
    target = tmp_path / "toolroot"
    scaffold.initialise(target)
    (target / "data" / "db_instances.json").write_text("{}", encoding="utf-8")

    scaffold.initialise(target, force=True)

    assert json.loads(
        (target / "data" / "db_instances.json").read_text(encoding="utf-8"))["db_instances"] == []


def test_the_directories_a_run_needs_exist(root: Path) -> None:
    """A run that has to create its own log directory fails differently on every platform."""
    for name in ("data", "logs", "runtime", "secrets"):
        assert (root / name).is_dir()


def test_the_secret_source_says_it_is_not_what_the_toolkit_reads(root: Path) -> None:
    """The commonest confusion in the whole setup, and it is silent when wrong."""
    secrets = json.loads((root / "secrets" / "secret_text.json").read_text(encoding="utf-8"))

    # Flat {ref: secret}, with commentary under an underscore key. The first version wrapped the
    # values in a "secrets" object, and encryption produced two secrets named `notes` and
    # `secrets` — after which collection reported "Password ref not found", pointing at the
    # inventory rather than at the file that was wrong.
    assert set(secrets) == {"_notes"}, "anything but _notes here becomes a secret when encrypted"

    notes = " ".join(secrets["_notes"])
    assert "encrypt-secret" in notes
    assert "never" in notes.lower() and "commit" in notes.lower()


def test_the_scaffolded_secret_file_encrypts_to_nothing(root: Path, tmp_path: Path) -> None:
    """An empty scaffold has no secrets, and the commentary must not become one."""
    from db_ops.lib.secret_text import encrypt_secret_text_file

    count = encrypt_secret_text_file(
        root / "secrets" / "secret_text.json", tmp_path / "out.json", "passphrase")

    assert count == 0


def test_the_next_steps_name_real_commands(root: Path) -> None:
    """Printed to be followed literally, by a person or by an agent reading stdout."""
    steps = scaffold.next_steps(root)

    # `encrypt-secret-text` was a `control` command, and `control` is not in the thin
    # distribution — so the documented first run named a command the release does not have. Found
    # by following the steps against a real SQL Server.
    for command in ("db-ops encrypt-secret", "db-ops metrics collect --dry-run"):
        assert command in steps
    assert "db-ops db encrypt-secret-text" not in steps
