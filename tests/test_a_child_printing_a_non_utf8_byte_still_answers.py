"""A command's answer must survive one byte of noise on the child's stdout.

The failure, on the master on 2026-09-19: a forced run refused with *"authorize exited 0 without a
JSON response"* while the gate itself had been perfectly fine — it ran, exited 0 and printed valid
JSON. What it also printed was a cp1252 em dash (0x97) from a native tool it had shelled out to.
``subprocess.run(..., encoding="utf-8")`` decodes on a reader thread, that thread died on the
undecodable byte, and the answer never arrived. The message named the command, so the search went to
the gate, which was not where the fault was.

The two directions are deliberately not symmetrical, and these tests pin both:

* **out** — the request is encoded strictly, because a payload that cannot be encoded is the
  caller's bug and must not be delivered with a character silently swapped;
* **back** — the answer is decoded with ``errors="replace"``, because the child's stdout is not only
  the JSON: whatever it shells out to writes there too, in whatever code page the machine has.
"""

from __future__ import annotations

import json
import subprocess
import sys

from db_ops.lib import common_cli


def _child_that_prints(prelude: bytes, answer: dict) -> str:
    """A program that writes raw bytes to stdout, then the JSON answer db_ops expects."""
    return (
        "import sys, json\n"
        f"sys.stdout.buffer.write({prelude!r})\n"
        f"sys.stdout.buffer.write(json.dumps({answer!r}).encode('utf-8'))\n"
        "sys.stdout.buffer.flush()\n"
    )


def test_a_cp1252_byte_on_stdout_no_longer_costs_the_answer(monkeypatch):
    """0x97 is an em dash in cp1252 and not valid UTF-8. It is noise; the JSON beside it is not."""
    answer = {"success": True, "data": {"authorized": True}, "error": ""}
    real_run = subprocess.run  # bound before the patch: the stand-in runs a real child

    def fake_run(args, **kwargs):
        return real_run([sys.executable, "-c", _child_that_prints(b"\x97 done\n", answer)],
                        capture_output=True)

    monkeypatch.setattr(common_cli.subprocess, "run", fake_run)

    completed, error = common_cli.spawn("authorize", {"run_key": "k"})

    assert error == ""
    assert completed.returncode == 0
    assert "�" in completed.stdout, "the undecodable byte is replaced, not raised"
    assert json.loads(completed.stdout[completed.stdout.index("{"):])["success"] is True


def test_the_request_is_written_as_bytes_and_encoded_strictly(monkeypatch):
    """Strict on the way out: a substituted character in a request is a wrong request, silently."""
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, b'{"success": true}', b"")

    monkeypatch.setattr(common_cli.subprocess, "run", fake_run)

    common_cli.spawn("secret-check", {"note": "an em dash — here"})

    assert isinstance(seen["input"], bytes)
    assert "encoding" not in seen and "errors" not in seen, (
        "the streams carry bytes so each direction can state its own handling")
    assert "—".encode("utf-8") in seen["input"]


def test_stderr_is_decoded_the_same_way(monkeypatch):
    """The error path reads stderr, and a failing native tool is exactly where odd bytes appear."""

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, b"", b"\x97 could not connect")

    monkeypatch.setattr(common_cli.subprocess, "run", fake_run)

    completed, error = common_cli.spawn("db-status", {})

    assert error == ""
    assert completed.stderr.endswith("could not connect")


def test_the_caller_is_told_what_happened_when_the_answer_really_is_missing(monkeypatch):
    """Nothing here should hide a child that printed no JSON at all — that message is correct and
    stays. What changed is that a decodable-byte failure no longer produces it falsely."""

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, b"nothing to say\n", b"")

    monkeypatch.setattr(common_cli.subprocess, "run", fake_run)

    try:
        common_cli.run("authorize", {})
    except common_cli.CommonCliError as exc:
        assert "without a JSON response" in str(exc)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("a child that printed no JSON must still raise")
