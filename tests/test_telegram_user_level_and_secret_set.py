"""Two hand-edits that standing up a node's Telegram needed, turned into commands.

Found on 2026-09-11 configuring a fresh node:

* **`telegram user-level`.** Intake records every sender at ``user_type: 0``, so the operator's own
  ``/spbot_self_status`` was refused four times ("Permission denied, user_type=0"), and the only fix
  was editing ``telegram_users.json`` by hand — a file the running intake rewrites every second.
  ``group-level`` already existed for groups; users had nothing.
* **`common.cli secret-set`.** Moving one bot token onto the node meant writing it into a plaintext
  file, running ``encrypt-secret`` and deleting the file, although the single-entry write
  (``lib.secret_text.set_secret_text``) already existed. And ``encrypt-secret`` *replaces* the store,
  so on that node — whose scaffold source holds no secrets — running it would have wiped both the
  token and the database password ``instance-add`` had stored. The command says so when it matters.

A level is a permission and a secret is a secret, so both commands are strict in the way that
protects the operator: no substring match for a user, and no secret accepted from the command line
or a file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from db_ops import scaffold
from db_ops.lib.secret_text import load_secret_text_file
from db_ops.telegram.updates import set_user_level

REPO_ROOT = Path(__file__).resolve().parents[1]
KEY = "user-level-and-secret-set-passphrase"
TOKEN = "8629909482:AAH-not-a-real-token-but-shaped-like-one-000"


def _users(tmp_path, *records):
    path = tmp_path / "telegram_users.json"
    path.write_text(json.dumps({"telegram_users": list(records)}), encoding="utf-8")
    return path


OPERATOR = {"user_id": "851670612", "username": "operator_one", "user_type": 0, "status": "active"}
OTHER = {"user_id": "700000001", "username": "operator_two", "user_type": 0, "status": "active"}


# ----------------------------------------------------------------------------------- user-level --

def test_a_user_is_cleared_by_id_and_the_permission_check_reads_it(tmp_path):
    from db_ops.telegram.command_processor import telegram_user_type

    path = _users(tmp_path, dict(OPERATOR), dict(OTHER))

    result = set_user_level(user="851670612", level=100, users_path=path)

    assert result["before"] == {"user_type": 0} and result["after"] == {"user_type": 100}
    assert telegram_user_type(path, user_id="851670612") == 100
    assert telegram_user_type(path, user_id="700000001") == 0, "nobody else was touched"


def test_a_username_works_with_or_without_the_at_sign(tmp_path):
    path = _users(tmp_path, dict(OPERATOR))

    set_user_level(user="@Operator_One", level=5, users_path=path)

    assert json.loads(path.read_text(encoding="utf-8"))["telegram_users"][0]["user_type"] == 5


def test_a_fragment_of_a_username_grants_nothing(tmp_path):
    """`group-level` accepts a substring naming one group. A level is a permission, so here a
    fragment is refused even when it would be unambiguous."""
    path = _users(tmp_path, dict(OPERATOR))

    with pytest.raises(RuntimeError, match="no user matches"):
        set_user_level(user="operator", level=100, users_path=path)


def test_a_negative_level_is_refused_rather_than_read_as_disable(tmp_path):
    path = _users(tmp_path, dict(OPERATOR))

    with pytest.raises(RuntimeError, match="0 or more"):
        set_user_level(user="851670612", level=-1, users_path=path)


def test_a_node_nobody_has_messaged_says_how_users_get_there(tmp_path):
    with pytest.raises(RuntimeError, match="first message the bot"):
        set_user_level(user="@anyone", level=1, users_path=tmp_path / "missing.json")


def test_the_cli_routes_user_level_to_the_function():
    from db_ops.telegram import cli

    args = cli.parse_args(["user-level", "--user", "@operator_one", "--level", "100"])

    assert args.telegram_function is set_user_level and args.level == 100


# ------------------------------------------------------------------------------ alerts default --

def test_init_ships_alerts_on_and_a_fresh_root_routes_nothing(tmp_path):
    """On by default since 2026-09-11, so storing the token and giving a group its level is all it
    takes — and "on" must not mean "sends early": with no group level, no level has a chat."""
    scaffold.initialise(tmp_path, app_name="probe")
    config = json.loads((tmp_path / "data" / "telegram_config.json").read_text(encoding="utf-8"))
    assert config["enabled"] is True

    env = dict(os.environ, DB_OPS_HOME=str(tmp_path), PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    for level in ("logging", "warning", "error", "critical"):
        answer = subprocess.run([sys.executable, "-m", "db_ops.telegram.cli", "--config", "config.json",
                                 "route", level], cwd=tmp_path, env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        route = json.loads(answer.stdout)
        assert route["enabled"] is True and route["alert"] is False and route["chat_id"] == "", route


# ----------------------------------------------------------------------------------- secret-set --

@pytest.fixture
def node(tmp_path):
    scaffold.initialise(tmp_path, app_name="probe")
    return tmp_path


def _secret_set(node: Path, payload, *, source: str = "-"):
    env = dict(os.environ, DB_OPS_HOME=str(node), DB_OPS_SECRET_KEY=KEY, PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, "-m", "db_ops.common.cli", "secret-set", source],
                          input=text if source == "-" else None, cwd=node, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def _store(node: Path) -> dict[str, str]:
    return load_secret_text_file(node / "data" / "encrypted_secret_text.json", key=KEY)


def test_a_secret_on_stdin_is_stored_encrypted_and_never_echoed(node):
    result = _secret_set(node, {"ref": "TELEGRAM_BOT_TOKEN", "value": TOKEN})

    assert result.returncode == 0, result.stdout + result.stderr
    assert _store(node)["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert TOKEN not in result.stdout + result.stderr
    assert TOKEN not in (node / "data" / "encrypted_secret_text.json").read_text(encoding="utf-8")


def test_a_secret_on_the_command_line_is_refused_and_nothing_is_written(node):
    """Inline JSON puts the secret in argv, readable by every process on the machine."""
    inline = json.dumps({"ref": "TELEGRAM_BOT_TOKEN", "value": TOKEN})

    result = _secret_set(node, "", source=inline)

    assert json.loads(result.stdout)["success"] is False
    assert not (node / "data" / "encrypted_secret_text.json").exists()


def test_a_secret_in_a_file_is_refused(node):
    """A request file is exactly the plaintext on disk this command exists to avoid."""
    (node / "req.json").write_text(json.dumps({"ref": "X", "value": TOKEN}), encoding="utf-8")

    result = _secret_set(node, "", source="@req.json")

    assert json.loads(result.stdout)["success"] is False


def test_a_different_value_for_an_existing_ref_needs_overwrite(node):
    _secret_set(node, {"ref": "API_KEY", "value": "first"})

    refused = _secret_set(node, {"ref": "API_KEY", "value": "second"})
    replaced = _secret_set(node, {"ref": "API_KEY", "value": "second", "overwrite": True})

    assert json.loads(refused.stdout)["success"] is False
    assert json.loads(replaced.stdout)["success"] is True and _store(node)["API_KEY"] == "second"


def test_it_warns_that_encrypt_secret_would_drop_it_on_a_fresh_node(node):
    """init's plaintext source holds only notes, so encrypt-secret would rebuild an empty store."""
    answer = json.loads(_secret_set(node, {"ref": "TELEGRAM_BOT_TOKEN", "value": TOKEN}).stdout)

    plaintext = answer["data"]["plaintext_source"]
    assert plaintext["exists"] is True and plaintext["holds_ref"] is False
    assert "every other secret" in plaintext["warning"]


def test_also_plaintext_keeps_a_masters_source_in_step(node):
    source = node / "secrets" / "secret_text.json"
    source.write_text(json.dumps({"EXISTING": "x"}), encoding="utf-8")

    answer = json.loads(_secret_set(node, {"ref": "NEW_REF", "value": "v",
                                           "also_plaintext": True}).stdout)

    assert json.loads(source.read_text(encoding="utf-8"))["NEW_REF"] == "v"
    assert answer["data"]["plaintext_source"]["holds_ref"] is True
    assert "warning" not in answer["data"]["plaintext_source"]
