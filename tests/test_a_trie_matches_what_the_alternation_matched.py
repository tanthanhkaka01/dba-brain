"""The identifier scan and the showcase scrub build their word lists as a trie (0.35.0, 1.269).

A plain alternation of 856 address spellings made `check-identifiers` take more than ten minutes
over the shipped tree - 90 % of it in that one pattern, which `re` tries branch by branch at every
position. The trie lets it give up a position after a character. What it must never do is match
something else: the same spans, and the longest word still first where one word begins another
(`PAYROLL` before `PAYROLL_Prod` left `<fake>_Prod` on a page once).
"""

from __future__ import annotations

import random
import re

import pytest

from dbabrain.lib import pseudonym, trie_regex


def _plain(words):
    return "(?:" + "|".join(re.escape(w) for w in sorted(words, key=lambda w: (-len(w), w))) + ")"


WORDS = ["10.0.2.248", "10.0.2.24", "10.0.2.2", "10-0-2-248", "PAYROLL", "PAYROLL_Prod", "a.b", "a",
         "SQL$X", "x(y)", "ab", "abc", "abd", "b"]


@pytest.mark.parametrize("text", [
    "on 10.0.2.248 and 10.0.2.24x and 10.0.2.2", "PAYROLL_Prod and PAYROLL", "a.b ab abc abd abe b",
    "SQL$X x(y) 10-0-2-248-MSSQL", "",
])
def test_the_trie_finds_what_the_alternation_found(text):
    assert [m.span() for m in re.finditer(trie_regex.alternation(WORDS), text)] == \
        [m.span() for m in re.finditer(_plain(WORDS), text)]


def test_random_words_and_texts_agree():
    rng = random.Random(1269)
    alphabet = "ab.-_1"
    for _ in range(200):
        words = {"".join(rng.choice(alphabet) for _ in range(rng.randint(1, 5))) for _ in range(rng.randint(1, 12))}
        text = "".join(rng.choice(alphabet + " ") for _ in range(80))
        assert [m.span() for m in re.finditer(trie_regex.alternation(words), text)] == \
            [m.span() for m in re.finditer(_plain(words), text)], (words, text)


def test_folded_for_a_case_insensitive_pattern():
    pattern = re.compile(trie_regex.alternation(["PAYROLL_Prod", "payroll"], fold=True), re.IGNORECASE)
    assert pattern.search("x Payroll_PROD y").group(0) == "Payroll_PROD"


def test_nothing_matches_nothing():
    assert re.search(trie_regex.alternation([]), "anything") is None


def test_the_scrub_still_replaces_the_longest_address():
    """`10.0.2.24` begins `10.0.2.248`: the shorter one first would leave a stray `8` behind."""
    mapping = pseudonym.Mapping({"10.0.2.24": "address", "10.0.2.248": "address"})
    out = mapping.apply("on 10.0.2.248 and 10.0.2.24")
    fakes = mapping.as_dict()
    assert out == f"on {fakes['10.0.2.248']} and {fakes['10.0.2.24']}"
