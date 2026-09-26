"""Everything written is English - the docs, the comments, the docstrings, the notes (rules R36).

The tool is read by people who share one language with it, and the operator's own words in another
- quoted into a docstring as the reason for a change - are exactly what a later reader cannot use.
So an operator's words are kept *translated*, marked as such, with their date. The notes in
`data/*.json` count too: they are read back through Telegram and printed on the fleet page.

What is checked is prose, not data. A test that feeds Vietnamese text through an import, an
encoding or a Telegram message is testing that the tool handles it, and its string literals are
data; its comments and docstrings are prose. Written until 0.24.0 as a review item, and the first
run of this found six Vietnamese quotes, a Vietnamese README and three lines of `â†’` mojibake.

The letters looked for are the ones Vietnamese has and English does not (``ă â đ ê ô ơ ư`` and the
tone-marked vowels), plus the signature of UTF-8 read as cp1252, which is how `→` became `â†’`.
"""

from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROOTS = ("db_ops", "tests", "tests_estate", "docs", "examples", "scripts", "data",
         "README.md", "CHANGELOG.md", "CONTRIBUTING.md")
SKIP_PARTS = {"__pycache__", ".venv", "node_modules"}
NOT_ENGLISH = re.compile(r"[ăâđêôơưĂÂĐÊÔƠƯẠ-ỹ]|â€|â†")
NOTE_KEYS = {"note", "notes", "_note", "_notes", "description", "_comment", "comment"}

#: Files whose subject is the very text this looks for - named, with why.
EXEMPT = {
    "tests/test_docs_are_not_mojibake.py": "detects mojibake, so its docstrings show `â†'` on purpose",
    "tests/test_everything_written_is_english.py": "names the letters it looks for",
}


def _files(suffix: str) -> list[Path]:
    found: list[Path] = []
    for name in ROOTS:
        root = REPO / name
        if root.is_file() and root.suffix == suffix:
            found.append(root)
        elif root.is_dir():
            found.extend(path for path in root.rglob(f"*{suffix}")
                         if not SKIP_PARTS & set(path.parts))
    return sorted(path for path in found if path.relative_to(REPO).as_posix() not in EXEMPT)


def _python_prose(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    hits = [f"{tok.start[0]}: {tok.string.strip()[:100]}"
            for tok in tokenize.generate_tokens(io.StringIO(text).readline)
            if tok.type == tokenize.COMMENT and NOT_ENGLISH.search(tok.string)]
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False) or ""
            first = node.body[0].lineno if node.body else 0
            hits.extend(f"{first + offset}: {line.strip()[:100]}"
                        for offset, line in enumerate(doc.split("\n")) if NOT_ENGLISH.search(line))
    return hits


def _json_notes(value, trail: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in NOTE_KEYS:
                for text in item if isinstance(item, list) else [item]:
                    if isinstance(text, str) and NOT_ENGLISH.search(text):
                        yield f"{trail}/{key}: {text[:100]}"
            yield from _json_notes(item, f"{trail}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _json_notes(item, f"{trail}[{index}]")


@pytest.mark.parametrize("path", _files(".py"), ids=lambda path: path.relative_to(REPO).as_posix())
def test_python_comments_and_docstrings_are_english(path):
    hits = _python_prose(path)
    assert not hits, (f"{path.relative_to(REPO).as_posix()} - prose is English; an operator's words "
                      f"are quoted translated, marked so:\n" + "\n".join(hits))


def test_markdown_is_english():
    hits = [f"{path.relative_to(REPO).as_posix()}:{number}: {line.strip()[:100]}"
            for path in _files(".md")
            for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
            if NOT_ENGLISH.search(line)]
    assert not hits, "Markdown is English:\n" + "\n".join(hits[:30])


def test_the_notes_in_json_are_english():
    """Read back through Telegram and printed on the fleet page."""
    hits = []
    for path in _files(".json"):
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            continue
        hits.extend(f"{path.relative_to(REPO).as_posix()}{hit}" for hit in _json_notes(document))
    assert not hits, "notes and descriptions are English:\n" + "\n".join(hits[:30])


def test_the_check_sees_what_it_is_for():
    assert NOT_ENGLISH.search("phải có cli thêm backup restore")
    assert NOT_ENGLISH.search("ARP scan â†’ MAC map")
    assert not NOT_ENGLISH.search("an em dash — a curly quote ’ and an arrow →")
