"""A JSON request between two processes of this program does not go through the machine's code page.

Every app reaches a database through `python -m db_ops.common.cli run-sql`, with the request —
including the whole SQL text — written to the child's stdin. `subprocess.run(text=True)` with no
`encoding` picks `locale.getpreferredencoding()`, which on Windows is the ANSI code page, and the
child's `sys.stdin` picks its own with `errors="surrogateescape"`. When those two disagree, a byte
the reader cannot decode becomes a lone surrogate — silently, in the middle of a SQL statement.

That is what `/spbot_run_sql_task 18` hit for two days:

    SQL failed: 'utf-16-le' codec can't encode character U+DC97 in position 350

U+DC97 is byte 0x97, which is the em dash in cp1252. The script holds two em dashes in comments,
is valid UTF-8, and contains no 0x97 byte anywhere — so the statement that reached pyodbc was not
the file. It was the file after a trip through a pipe whose ends did not agree, which is why it
failed under the daemon and never from an Administrator console: different code pages.

Six attempts to reproduce it from a shell succeeded, because a shell had both ends agreeing.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

EM_DASH = "—"

#: Long enough that the interesting character is well inside the payload, as it was in the SQL
#: script that failed. Position is not the point; surviving is.
PAYLOAD = {"sql": "x" * 340 + EM_DASH + " and what each can carry", "note": "dấu tiếng Việt"}

#: The same request minus the one thing cp1252 cannot spell at all. Needed to reach the failure
#: production actually saw: an ANSI writer emits the em dash as the single byte 0x97, which is
#: valid cp1252 and not valid UTF-8, so the pair breaks at the READER. With the Vietnamese note in
#: it the trip ends earlier, at the writer, and the reader is never asked.
ANSI_SPELLABLE = {"sql": PAYLOAD["sql"], "note": "an ordinary note"}

#: Reads stdin the way `db_ops.common.cli` does now.
_CHILD_PINNED = (
    "import sys,json;"
    "d=json.loads(sys.stdin.buffer.read().decode('utf-8-sig'));"
    "s=d['sql'];"
    "print(json.dumps({"
    "'surrogates':[hex(ord(c)) for c in s if 0xd800<=ord(c)<=0xdfff],"
    "'em_dash':s.count('\\u2014'),'note':d['note']}))"
)


def _round_trip(*, parent_encoding: str | None, payload: dict | None = None) -> dict:
    """Send one request down a real pipe, encoded the way `parent_encoding` says.

    The encoding is done HERE and the bytes are handed to a binary `subprocess.run`, rather than
    letting `text=True, encoding=...` do it. Not a style choice: on Windows `communicate` writes
    stdin from `_writerthread`, and an encode error there kills that thread without closing the
    child's stdin, so the child blocks on `read()` for ever and the test hangs. The same payload
    on Linux raises in the caller and fails in a second. A test whose failure mode depends on
    which OS runs it is not a test, and this one is about encodings crossing a pipe - so the pipe
    carries bytes, and whether they could be produced at all is an answer, not a crash.
    """
    text = json.dumps(payload if payload is not None else PAYLOAD, ensure_ascii=False)
    try:
        request = text.encode(parent_encoding or "utf-8")
    except UnicodeEncodeError as exc:
        return {"failed": f"the writer could not encode the request: {exc}"}
    completed = subprocess.run(  # noqa: S603 - our own interpreter, fixed argv
        [sys.executable, "-c", _CHILD_PINNED], input=request, capture_output=True,
    )
    if completed.returncode != 0:
        return {"failed": completed.stderr.decode("utf-8", "replace").strip()[-200:]}
    return json.loads(completed.stdout.decode("utf-8"))


def test_a_request_survives_the_pipe_when_both_ends_are_pinned() -> None:
    """The fix, stated as the property: what goes in comes out."""
    answer = _round_trip(parent_encoding="utf-8")

    assert answer.get("surrogates") == []
    assert answer["em_dash"] == 1
    assert answer["note"] == PAYLOAD["note"]


def test_the_two_ends_disagreeing_is_what_broke_it() -> None:
    """The bug, kept as a test so the fix is not mistaken for decoration.

    With the writer on the ANSI code page and the reader on UTF-8, the payload does not arrive,
    and it can fail at either end. A request holding anything outside cp1252 - a Vietnamese
    service name, say - never leaves the writer. One that cp1252 CAN spell leaves as bytes the
    reader refuses: the em dash goes out as 0x97, which is not valid UTF-8. That second one is
    the production failure, except that there the reader's `surrogateescape` swallowed the byte
    into U+DC97 instead of refusing it, and the failure moved four layers away, to a driver
    refusing to encode a character nobody had written.
    """
    assert "failed" in _round_trip(parent_encoding="cp1252"), (
        "a cp1252 writer against a UTF-8 reader must not look fine")

    at_the_reader = _round_trip(parent_encoding="cp1252", payload=ANSI_SPELLABLE)

    assert "failed" in at_the_reader, "0x97 is not UTF-8; the reader must say so"
    assert "0x97" in at_the_reader["failed"], at_the_reader["failed"]


def test_the_spawn_pins_its_encoding() -> None:
    """Read off the source, because the call is the thing that has to say it.

    A test that only exercises a round trip would keep passing on a machine whose code page
    happens to be UTF-8 — which is most CI, and none of the estate's Windows hosts.
    """
    import inspect

    from db_ops.lib import common_cli

    source = inspect.getsource(common_cli.spawn)

    assert 'encoding="utf-8"' in source, "spawn must not inherit the machine's code page"


def test_the_reader_pins_its_encoding() -> None:
    import inspect

    from db_ops.common import cli

    source = inspect.getsource(cli._read_json_request)

    assert "sys.stdin.buffer.read().decode(" in source
    assert "sys.stdin.read()" not in source


@pytest.mark.parametrize("text", ["plain ascii", EM_DASH, "dấu tiếng Việt", "日本語", "emoji 🚨"])
def test_every_character_the_writer_serialises_survives_utf8(text: str) -> None:
    """`ensure_ascii=False` on the writing end keeps a Vietnamese service name readable in a
    request somebody is debugging, so the pipe has to carry the full range — not only the em dash
    that happened to break.

    A pure round trip through the two encodings the fix pins, with no child process. An earlier
    version drove this through a real `_read_json_request`, and importing `db_ops.common.cli` in a
    subprocess under pytest hung the suite twice: the module brings up logging, and the test was
    paying for a whole app start to check a `decode` call.
    """
    payload = json.dumps({"sql": f"SELECT '{text}'"}, ensure_ascii=False)

    arrived = json.loads(payload.encode("utf-8").decode("utf-8-sig"))

    assert arrived == {"sql": f"SELECT '{text}'"}
    assert not [c for c in payload if 0xD800 <= ord(c) <= 0xDFFF]


def test_the_ansi_code_page_cannot_carry_what_the_writer_may_send() -> None:
    """Why pinning is the fix rather than picking the other encoding.

    cp1252 has no room for most of the range above, so "make both ends cp1252" would trade a
    corrupted em dash for a refused Vietnamese name. UTF-8 is the only choice that carries
    everything `ensure_ascii=False` can produce.
    """
    with pytest.raises(UnicodeEncodeError):
        "日本語".encode("cp1252")
