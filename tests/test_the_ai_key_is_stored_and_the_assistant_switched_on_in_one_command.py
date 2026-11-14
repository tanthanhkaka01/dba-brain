"""One command stores the AI assistant's key and switches the assistant on - the first step of a new install (0.39.0).

The operator, 2026-11-14: *a node just installed, the AI API key added by a CLI command - write that
command, write it in the full guide, and first*. Until then it was two steps written nowhere together:
``secret-set`` for the key, then ``data/ai_assist.json`` edited by hand (``enabled``, ``api_key_ref``)
- while the assistant that could have said so was the thing being set up.

``dbabrain ai-key`` asks for the key on a terminal (not echoed); a script pipes ``{"value": ...}`` on
stdin. The key goes into the encrypted store first, then the settings point at it - so a switched-on
assistant never names a key the store does not hold - and a broken settings file is said, with
nothing stored.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dbabrain.common import scaffold
from dbabrain.lib import ai_assist as ai_rules
from dbabrain.lib.secret_text import load_secret_text_file

REPO_ROOT = Path(__file__).resolve().parents[1]
KEY = "ai-key-test-passphrase"
AI_KEY = "sk-ant-api03-not-a-real-key-but-shaped-like-one"


@pytest.fixture
def node(tmp_path):
    scaffold.initialise(tmp_path, app_name="probe")
    return tmp_path


def _ai_key(node: Path, payload=None, *, args=("-",)):
    env = dict(os.environ, DBABRAIN_HOME=str(node), DBABRAIN_SECRET_KEY=KEY, PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    env.pop("DBABRAIN_DATA_DIR", None)
    text = None if payload is None else (payload if isinstance(payload, str) else json.dumps(payload))
    return subprocess.run([sys.executable, "-m", "dbabrain.common.cli", "ai-key", *args],
                          input=text if text is not None else "", cwd=node, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def _store(node: Path) -> dict[str, str]:
    return load_secret_text_file(node / "data" / "encrypted_secret_text.json", key=KEY)


def _settings(node: Path) -> dict:
    return json.loads((node / "data" / ai_rules.FILE_NAME).read_text(encoding="utf-8"))


def _answer(result) -> dict:
    return json.loads(result.stdout[result.stdout.find("{"):])


# --------------------------------------------------------------------------- a fresh install
def test_on_a_fresh_install_the_key_is_stored_and_the_assistant_switched_on(node):
    assert _settings(node)["enabled"] is False and _settings(node)["api_key_ref"] == "", "init ships it off"

    result = _ai_key(node, {"value": AI_KEY})

    answer = _answer(result)
    assert answer["success"] is True, result.stdout + result.stderr
    assert _store(node) == {"AI_ASSIST_API_KEY": AI_KEY}
    settings = _settings(node)
    assert settings["enabled"] is True and settings["api_key_ref"] == "AI_ASSIST_API_KEY"
    ai_rules.parse_ai_assist(settings)  # every other field as init wrote it
    assert answer["data"]["next"] == 'dbabrain telegram ai-ask "What do I set up next?"'
    assert AI_KEY not in result.stdout and AI_KEY not in result.stderr, "never echoed"


def test_a_root_with_no_settings_file_gets_one_at_the_shipped_values(node):
    (node / "data" / ai_rules.FILE_NAME).unlink()

    assert _answer(_ai_key(node, {"value": AI_KEY}))["success"] is True

    settings = _settings(node)
    shipped = ai_rules.shipped_document()
    assert settings == {**shipped, "enabled": True, "api_key_ref": "AI_ASSIST_API_KEY"}


def test_the_settings_file_keeps_everything_else_and_its_indent(node):
    path = node / "data" / ai_rules.FILE_NAME
    before = _settings(node)
    before["assistant_name"] = "Helper"
    path.write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")

    _ai_key(node, {"value": AI_KEY})

    assert _settings(node) == {**before, "enabled": True, "api_key_ref": "AI_ASSIST_API_KEY"}
    assert path.read_text(encoding="utf-8").startswith('{\n  "'), "the file's own indent"


# --------------------------------------------------------------------------- the ref
def test_a_new_key_replaces_the_one_the_settings_already_name(node):
    _ai_key(node, {"value": AI_KEY})

    answer = _answer(_ai_key(node, {"value": AI_KEY + "-rotated"}))

    assert answer["success"] is True and answer["data"]["replaced"] is True
    assert _store(node) == {"AI_ASSIST_API_KEY": AI_KEY + "-rotated"}


def test_the_ref_ai_assist_json_names_is_used(node):
    path = node / "data" / ai_rules.FILE_NAME
    settings = _settings(node)
    settings["api_key_ref"] = "KEY_CLAUDE_TEAM"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    _ai_key(node, {"value": AI_KEY})

    assert _store(node) == {"KEY_CLAUDE_TEAM": AI_KEY} and _settings(node)["api_key_ref"] == "KEY_CLAUDE_TEAM"


def test_a_ref_the_request_names_that_holds_another_secret_is_refused_unless_overwrite(node):
    env_ref = {"ref": "TELEGRAM_BOT_TOKEN"}
    env = dict(os.environ, DBABRAIN_HOME=str(node), DBABRAIN_SECRET_KEY=KEY, PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.run([sys.executable, "-m", "dbabrain.common.cli", "secret-set", "-"], cwd=node, env=env,
                   input=json.dumps({"value": "token-value", **env_ref}), capture_output=True, text=True,
                   encoding="utf-8", timeout=60, check=True)  # another secret, stored earlier
    _settings_before = _settings(node)

    refused = _answer(_ai_key(node, {"value": AI_KEY, **env_ref}))
    assert refused["success"] is False and "already exists with a different value" in refused["error"]
    assert _store(node)["TELEGRAM_BOT_TOKEN"] == "token-value"
    assert _settings(node) == _settings_before

    replaced = _answer(_ai_key(node, {"value": AI_KEY, "overwrite": True, **env_ref}))
    assert replaced["success"] is True and _store(node)["TELEGRAM_BOT_TOKEN"] == AI_KEY


# --------------------------------------------------------------------------- refused, nothing stored
@pytest.mark.parametrize("args", [(json.dumps({"value": AI_KEY}),), ("@req.json",)])
def test_a_key_inline_or_in_a_file_is_refused(node, args):
    (node / "req.json").write_text(json.dumps({"value": AI_KEY}), encoding="utf-8")

    answer = _answer(_ai_key(node, args=args))

    assert answer["success"] is False and "stdin" in answer["error"]
    assert not (node / "data" / "encrypted_secret_text.json").exists() or _store(node) == {}


def test_bare_with_no_terminal_says_how_to_pipe_it(node):
    answer = _answer(_ai_key(node, None, args=()))

    assert answer["success"] is False and "pipe the request on stdin" in answer["error"]


def test_an_empty_key_is_refused(node):
    answer = _answer(_ai_key(node, {"value": ""}))
    assert answer["success"] is False and "value" in answer["error"]
    assert not (node / "data" / "encrypted_secret_text.json").exists() or _store(node) == {}


def test_a_broken_settings_file_is_said_and_nothing_is_stored(node):
    (node / "data" / ai_rules.FILE_NAME).write_text("{ not json", encoding="utf-8")

    answer = _answer(_ai_key(node, {"value": AI_KEY}))

    assert answer["success"] is False and "nothing was stored" in answer["error"]
    assert not (node / "data" / "encrypted_secret_text.json").exists() or _store(node) == {}


def test_a_key_that_is_not_shaped_like_an_anthropic_one_is_stored_with_a_warning(node):
    answer = _answer(_ai_key(node, {"value": "not-an-sk-ant-key"}))

    assert answer["success"] is True
    assert any("sk-ant-" in warning for warning in answer["data"]["warnings"])


# --------------------------------------------------------------------------- where a person finds it
def test_the_command_is_a_top_level_word_and_the_first_step_init_and_the_guide_give():
    from dbabrain import cli as top

    assert "ai-key" in top.ALIASES
    steps = scaffold.next_steps(Path("/root"))
    assert steps.index("dbabrain ai-key") < steps.index("Next, to monitor one SQL Server"), "first"
    guide = (REPO_ROOT / "dbabrain" / "common" / "agents_guide.md").read_text(encoding="utf-8")
    assert guide.index("dbabrain ai-key") < guide.index("## 3. The clock"), "the first step after the passphrase"
