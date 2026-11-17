"""A secret is removed one at a time, and only by the value it holds.

Until 2026-11-17 there was ``secret-set`` and no way back: taking the master's dead MCP node token out
of the store meant decrypting two copies and comparing them by hand. The operator asked for the twin
of ``secret-set`` - *one secret at a time, and the value must match*.

The value is the guard. A ref is a name, and names are typed by hand: removing by name alone takes
away whatever a typo lands on, or a password rotated since the caller last looked. So a request names
the ref and its value; a store holding something else under that ref is refused and nothing is
written. And a master's plaintext source is held to the same value and cleaned with it, because
``deploy`` regenerates the encrypted store from that file - a ref removed only from the encrypted copy
would come back at the next deploy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dbabrain.common import scaffold
from dbabrain.lib import errors
from dbabrain.lib.secret_text import load_secret_text_file, remove_secret_text, set_secret_text

REPO_ROOT = Path(__file__).resolve().parents[1]
KEY = "secret-remove-passphrase"


def _store(data: Path) -> dict[str, str]:
    return load_secret_text_file(data / "encrypted_secret_text.json", key=KEY)


@pytest.fixture
def data(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    set_secret_text(folder, "OLD_TOKEN", "t-old", key=KEY)
    set_secret_text(folder, "DB_PASSWORD", "p-keep", key=KEY)
    return folder


def test_a_ref_with_its_value_is_removed_and_nothing_else(data):
    assert remove_secret_text(data, "OLD_TOKEN", "t-old", key=KEY) == {"removed": True, "plaintext_removed": False}
    assert _store(data) == {"DB_PASSWORD": "p-keep"}


def test_a_different_value_is_refused_and_nothing_is_written(data):
    before = (data / "encrypted_secret_text.json").read_bytes()
    with pytest.raises(errors.Refused, match="different value"):
        remove_secret_text(data, "OLD_TOKEN", "t-typed-wrong", key=KEY)
    assert (data / "encrypted_secret_text.json").read_bytes() == before


def test_a_ref_that_is_not_there_is_refused(data):
    with pytest.raises(errors.Refused, match="neither"):
        remove_secret_text(data, "NO_SUCH_REF", "anything", key=KEY)


def test_no_value_is_no_removal(data):
    with pytest.raises(errors.Refused, match="without its value"):
        remove_secret_text(data, "OLD_TOKEN", "", key=KEY)
    assert "OLD_TOKEN" in _store(data)


def test_the_plaintext_source_loses_the_ref_too_or_the_next_deploy_brings_it_back(data, tmp_path):
    source = tmp_path / "secret_text.json"
    source.write_text(json.dumps({"OLD_TOKEN": "t-old", "DB_PASSWORD": "p-keep"}), encoding="utf-8")
    assert remove_secret_text(data, "OLD_TOKEN", "t-old", key=KEY, plaintext_store=source) == {
        "removed": True, "plaintext_removed": True}
    assert json.loads(source.read_text(encoding="utf-8")) == {"DB_PASSWORD": "p-keep"}


def test_a_mismatch_in_the_plaintext_source_leaves_both_places_as_they_were(data, tmp_path):
    source = tmp_path / "secret_text.json"
    source.write_text(json.dumps({"OLD_TOKEN": "t-other"}), encoding="utf-8")
    before = (data / "encrypted_secret_text.json").read_bytes()
    with pytest.raises(errors.Refused, match="plaintext source"):
        remove_secret_text(data, "OLD_TOKEN", "t-old", key=KEY, plaintext_store=source)
    assert (data / "encrypted_secret_text.json").read_bytes() == before
    assert json.loads(source.read_text(encoding="utf-8")) == {"OLD_TOKEN": "t-other"}


# ------------------------------------------------------------------------------ the command --

def _run(node: Path, argv: list[str], payload: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, DBABRAIN_HOME=str(node), DBABRAIN_SECRET_KEY=KEY, PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return subprocess.run([sys.executable, "-m", "dbabrain.common.cli", "secret-remove", *argv],
                          input=json.dumps(payload) if payload is not None else None, cwd=node, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


@pytest.fixture
def node(tmp_path):
    scaffold.initialise(tmp_path, app_name="probe")
    set_secret_text(tmp_path / "data", "OLD_TOKEN", "t-old-value", key=KEY)
    return tmp_path


def test_the_command_removes_one_ref_and_never_echoes_its_value(node):
    done = _run(node, ["-"], {"ref": "OLD_TOKEN", "value": "t-old-value"})
    answer = json.loads(done.stdout)
    assert answer["success"] is True, answer
    assert answer["data"]["removed"] is True
    assert "OLD_TOKEN" not in _store(node / "data")
    assert "t-old-value" not in done.stdout + done.stderr


def test_the_command_refuses_a_wrong_value_without_saying_it(node):
    done = _run(node, ["-"], {"ref": "OLD_TOKEN", "value": "t-wrong-value"})
    answer = json.loads(done.stdout)
    assert answer["success"] is False and "different value" in str(answer["error"] or answer["message"])
    assert "t-wrong-value" not in done.stdout + done.stderr
    assert "OLD_TOKEN" in _store(node / "data")


def test_the_request_is_taken_from_stdin_only(node):
    done = _run(node, ['{"ref": "OLD_TOKEN", "value": "t-old-value"}'])
    assert json.loads(done.stdout)["success"] is False
    assert "OLD_TOKEN" in _store(node / "data")


def test_mcp_refuses_it_as_it_refuses_secret_set():
    from dbabrain.lib.mcp_tools import SECRET_VALUE_COMMANDS

    assert "secret-remove" in SECRET_VALUE_COMMANDS
