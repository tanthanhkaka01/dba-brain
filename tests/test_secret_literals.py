"""A value from the secret store must not sit in a file that ships.

This check exists because of a gap between the two scanners that were already in the gate, and the
gap is exactly one string wide:

- `gitleaks` matches the *shape* of a credential. A real password written into a test as an
  example looks like every other test placeholder, so it gets allowlisted and stops being looked at.
- `check-identifiers` matches configured *identifiers* — addresses, hostnames, accounts. A password
  is not an identifier, so it is not in the term list at all.

A real SA password sat in a shipped test from v0.2.0 to v0.4.1 — eight tags and every sdist —
because nothing compared the literal against the store. Comparing them is the whole idea: a value
is a secret because the store says so, not because it looks like one.
"""

from __future__ import annotations

import json

import pytest

from db_ops.common import secret_literals
from db_ops.lib.secret_text import encrypt_secret_text

KEY = "test-passphrase-not-a-real-one"


def _store(tmp_path, secrets: dict[str, str]):
    path = tmp_path / "encrypted_secret_text.json"
    path.write_text(json.dumps(encrypt_secret_text(secrets, KEY)), encoding="utf-8")
    return path


def test_a_stored_value_in_a_shipped_file_is_reported(tmp_path):
    store = _store(tmp_path, {"MSSQL_LAB_SA": "Jf-not-real-37d#42"})
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_thing.py").write_text(
        'for good in ("Jf-not-real-37d#42", "plain12345"):\n    check(good)\n', encoding="utf-8")

    outcome = secret_literals.scan(
        {"root": str(tmp_path), "paths": ["pkg"], "store": str(store)}, key=KEY)

    assert outcome["hits"] == 1
    finding = outcome["findings"][0]
    assert finding["secret_ref"] == "MSSQL_LAB_SA"
    assert finding["file"] == "pkg/test_thing.py"
    assert finding["line"] == 1


def test_the_report_names_the_ref_and_never_the_value(tmp_path):
    """A checker that echoes what it found has moved the leak into the log."""
    secret = "Jf-not-real-37d#42"
    store = _store(tmp_path, {"MSSQL_LAB_SA": secret})
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_thing.py").write_text(f'PASSWORD = "{secret}"\n', encoding="utf-8")

    outcome = secret_literals.scan(
        {"root": str(tmp_path), "paths": ["pkg"], "store": str(store)}, key=KEY)
    report = secret_literals.format_report(outcome)

    assert "MSSQL_LAB_SA" in report
    assert secret not in report
    assert secret not in json.dumps(outcome)


def test_a_clean_tree_reports_nothing(tmp_path):
    store = _store(tmp_path, {"MSSQL_LAB_SA": "Jf-not-real-37d#42"})
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "code.py").write_text("PASSWORD_REF = 'MSSQL_LAB_SA'\n", encoding="utf-8")

    outcome = secret_literals.scan(
        {"root": str(tmp_path), "paths": ["pkg"], "store": str(store)}, key=KEY)

    assert outcome["hits"] == 0, "a ref *name* is what belongs in code - only the value is a leak"


def test_short_and_placeholder_values_are_not_searched(tmp_path):
    """A four-character value collides with ordinary words; `changeme` proves nothing."""
    store = _store(tmp_path, {"SHORT": "abc", "TEMPLATE": "changeme"})
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "code.py").write_text("x = 'abc'\ny = 'changeme'\n", encoding="utf-8")

    outcome = secret_literals.scan(
        {"root": str(tmp_path), "paths": ["pkg"], "store": str(store)}, key=KEY)

    assert outcome["hits"] == 0
    assert outcome["secrets_searched"] == 0 or outcome["hits"] == 0


def test_no_key_refuses_rather_than_reporting_clean(tmp_path, monkeypatch):
    """The one answer this must never give is 'clean' about a store it could not read."""
    monkeypatch.delenv("DB_OPS_SECRET_KEY", raising=False)
    store = _store(tmp_path, {"MSSQL_LAB_SA": "Jf-not-real-37d#42"})

    with pytest.raises(secret_literals.SecretLiteralError):
        secret_literals.scan({"root": str(tmp_path), "paths": ["pkg"], "store": str(store)},
                             key=None)


def test_an_unreadable_store_refuses(tmp_path):
    store = tmp_path / "encrypted_secret_text.json"
    store.write_text("{ not json", encoding="utf-8")

    with pytest.raises(secret_literals.SecretLiteralError):
        secret_literals.scan({"root": str(tmp_path), "paths": [], "store": str(store)}, key=KEY)
