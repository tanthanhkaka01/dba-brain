"""The showcase scrubber reads a page's JSON value before it looks for a `DOMAIN\\login` in it.

A page escapes `<` and `>` as `\\u003c` / `\\u003e`. The scrubber read the identity fields straight
out of the page's text, took the backslash of `"\\u003cnot-provided\\u003e"` for the separator of a
qualified login, and learned its "login" half - `u003e` - as a credential. Every `\\u003e` on the
page then became `\\CRED_9251`: sixteen invalid JSON escapes on the 0.30.0 showcase's inventory
page, one inside a `>` of SQL text, and a page a JSON reader could no longer load (found 2026-10-23).

What is pinned: an escape is decoded before the split, a placeholder is not a name, and a real
`DOMAIN\\login` - written `DOMAIN\\\\login` in the page - still gives both its halves.
"""
from __future__ import annotations

from dbabrain.common import showcase


def test_an_escaped_placeholder_teaches_no_name():
    page = '{"login": "\\u003cnot-provided\\u003e", "owners": ["\\u003cnot-reported\\u003e"]}'

    assert showcase._terms_from_json_fields(page) == {}


def test_an_identity_item_that_is_a_placeholder_teaches_no_name():
    page = '{"code": "SQL_AGENT_JOB_INVENTORY", "item": "\\u003cnot-provided\\u003e"}'

    assert showcase._terms_from_identity_items(page) == {}


def test_a_qualified_login_still_gives_both_halves():
    page = '{"login": "EXAMPLECORP\\\\payroll_svc"}'

    found = showcase._terms_from_json_fields(page)

    assert found == {"payroll_svc": "credential", "EXAMPLECORP": "host"}


def test_no_learned_term_is_an_escape_sequence():
    page = ('{"login": "\\u003cnot-provided\\u003e", "account": "ops\\u0040corp", '
            '"principal": "EXAMPLECORP\\\\svc_backup"}')

    found = showcase._terms_from_json_fields(page)

    assert not any(term.startswith("u00") for term in found), found
    assert "ops@corp" in found and "svc_backup" in found
