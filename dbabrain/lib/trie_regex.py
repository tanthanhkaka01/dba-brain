"""One regular expression for many literal words, written as a trie (0.35.0, 1.269).

`re` tries an alternation's branches one after another at every position, so `a|b|c|...` over the
856 spellings of an estate's addresses cost the identifier scan about 90 % of a ten-minute pass over
the shipped tree - measured 2026-11-02, 50 s for 5.5 MB on that one pattern. Folding the words into
a trie (`10\\.0\\.(?:2\\.248|1\\.5)`) lets the engine reject a position after one character instead
of 856. Values and rules only, so it lives in ``lib``: ``common.identifier_scan`` and
``lib.pseudonym`` (the showcase scrub) both build their alternations here.

Semantics are the plain alternation's sorted longest first: at a node that ends a word and also
continues, the continuation is tried first (greedy ``?``), so the longest word still wins.
"""

from __future__ import annotations

import re
from typing import Iterable

__all__ = ["alternation"]


def alternation(words: Iterable[str], *, fold: bool = False) -> str:
    """A non-capturing group matching exactly ``words``; ``fold`` lowercases them first, for a
    pattern compiled with ``re.IGNORECASE``. An empty list matches nothing."""
    trie: dict[str, dict] = {}
    for word in words:
        if not word:
            continue
        node = trie
        for char in (word.lower() if fold else word):
            node = node.setdefault(char, {})
        node[""] = {}
    if not trie:
        return "(?!)"
    return "(?:" + _node(trie) + ")"


def _node(node: dict[str, dict]) -> str:
    branches = [re.escape(char) + _node(child) for char, child in sorted(node.items()) if char]
    ends = "" in node
    if not branches:
        return ""
    body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
    if ends:
        return ("(?:" + body + ")" if len(branches) == 1 and len(body) > 1 else body) + "?"
    return body
