"""`create-db-docker` and `move-db-docker` run in `common.cli`, and answer like every command there.

They moved from the `sre` app in 0.23.0 (1.37) so that the bot's `/spbot_create_db_docker` and the
lab drills go through the same `common.cli` path as backup and restore, reading no configuration.
Proving it on the labs found three ways the move could have been worse than the code it replaced,
and each is held here:

* a wrong SSH password ended in a `RemoteAuthError` traceback - the caller read *exited 1 without a
  JSON response* instead of the reason;
* the answer crossed a Windows pipe in the ANSI code page, which its one client decodes as UTF-8,
  so every non-ASCII character in it arrived as U+FFFD - the em dash of an "already exists"
  refusal among them;
* a local `docker compose up` writes to file descriptor 1 directly, and anything on stdout that is
  not the answer is an answer nobody can parse.
"""

from __future__ import annotations

import json
import subprocess
import sys

from db_ops.common import cli_docker_db
from db_ops.lib import common_cli


def _run(monkeypatch, capfd, work):
    monkeypatch.setattr(cli_docker_db, "_create", work)
    code = cli_docker_db.run("create-db-docker", ["-"],
                             read_request=lambda *_: ({"name": "lab01"}, 0))
    out, err = capfd.readouterr()
    return code, json.loads(out), err


def test_an_unexpected_failure_still_answers_in_the_envelope(monkeypatch, capfd):
    def work(request):
        raise RuntimeError("SSH authentication failed for labuser@192.0.2.250:22")

    code, answer, _ = _run(monkeypatch, capfd, work)

    assert code == 1 and answer["success"] is False
    assert "SSH authentication failed" in answer["error"]


def test_what_a_child_process_prints_during_the_work_goes_to_stderr(monkeypatch, capfd):
    def work(request):
        # What a local `docker compose up` does: write to fd 1 itself, past sys.stdout.
        subprocess.run([sys.executable, "-c", "print('Container lab01 Started')"], check=True)
        print("a progress line from Python")
        return {"name": "lab01", "status": "running", "dry_run": False}

    code, answer, err = _run(monkeypatch, capfd, work)

    assert code == 0 and answer["data"]["status"] == "running"
    assert "Container lab01 Started" in err and "a progress line from Python" in err


def test_a_non_ascii_character_in_the_answer_survives_the_pipe():
    """Through the real transport, both ends: the request goes out UTF-8 and the answer must come
    back UTF-8, whatever this machine's code page."""
    ok, _data, error = common_cli.run_allowing_failure(
        "create-db-docker", {"name": "lab—1", "engine": "postgres", "version": "18",
                             "dry_run": True})

    assert ok is False
    assert "lab—1" in error, ascii(error)
    assert "�" not in error
