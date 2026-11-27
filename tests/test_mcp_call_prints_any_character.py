"""``dbabrain mcp call`` prints any answer, whatever the stream's code page.

On Windows stdout is the ANSI code page (cp1252 here). Reading the worker's job runs with
``dbabrain mcp call store_query @q.json > out.txt`` on 2026-11-26 died with ``UnicodeEncodeError`` at the first ✅ in
a backup's message - the answer was there and could not be printed. ``call`` and ``tools`` now send a piped or
redirected answer as UTF-8, as common.cli does, and a console shows what it cannot draw as ``?``.
"""

from __future__ import annotations

import io
import sys

from dbabrain.mcp import cli


class _Console(io.TextIOWrapper):
    def isatty(self) -> bool:
        return True


def test_a_redirected_answer_leaves_as_utf8(monkeypatch):
    buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buffer, encoding="cp1252"))
    cli._answer_stream()
    print("✅ repository check: clean · 1385 files")
    sys.stdout.flush()
    assert buffer.getvalue().decode("utf-8").replace("\r\n", "\n") == "✅ repository check: clean · 1385 files\n"


def test_a_console_shows_a_mark_instead_of_a_traceback(monkeypatch):
    buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", _Console(buffer, encoding="cp1252"))
    cli._answer_stream()
    print("✅ done")
    sys.stdout.flush()
    assert buffer.getvalue().decode("cp1252").strip() == "? done"
