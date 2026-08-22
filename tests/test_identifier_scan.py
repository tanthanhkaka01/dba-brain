"""The scrub checker has to be trustworthy in both directions, and the second one is harder.

Missing a real address ships it. But **reporting things that are not leaks is the failure that
actually happens**, because a checker that cries wolf gets switched off and then misses everything.
This module's own first run proved it: deriving terms from the inventory and matching them as
substrings produced **1,550 hits** — the estate has databases genuinely called `Export`,
`Inventory`, `Damage` and `Maintenance`, so every occurrence of the word "inventory" in
`inventory_report.py` was a finding. The same run, tiered by confidence and matched on word
boundaries, reports 80.

So these tests pin both edges: the spellings that must be found, and the coincidences that must
not be reported.
"""

from __future__ import annotations

import json

import pytest

from db_ops.common import identifier_scan
from db_ops.common.identifier_scan import CERTAIN, LIKELY, REVIEW


# --------------------------------------------------------------------------- #
# One identifier, three spellings
# --------------------------------------------------------------------------- #
def test_an_address_is_searched_dotted_hyphenated_and_underscored() -> None:
    """The three forms are one machine, and each hid from a different earlier grep.

    Hyphenated is how a `server_id` spells it; underscored is how a secret ref does, and a `\\b`
    in a grep does not match there because `_` is a word character. That is exactly how
    `ORACLE_..._SYS` survived the first scrub.
    """
    assert identifier_scan._spellings("192.0.2.248") == {
        "192.0.2.248", "192-0-2-248", "192_0_2_248",
    }


def test_a_hyphenated_identifier_also_gets_its_underscore_form() -> None:
    assert "ACME_192_0_2_248" in identifier_scan._spellings("ACME-192-0-2-248")


# --------------------------------------------------------------------------- #
# Confidence — what a hit is worth acting on
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("term", ["192.0.2.248", "192-0-2-248", "192_0_2_248"])
def test_an_address_in_any_spelling_is_certain(term: str) -> None:
    assert identifier_scan.confidence(term) == CERTAIN


@pytest.mark.parametrize("term", ["ACME-192-0-2-248", "ORACLE_203_0_113_121_1522_SYS"])
def test_a_token_with_a_digit_and_a_separator_is_certain(term: str) -> None:
    """Nothing in ordinary prose looks like this, so a match is the estate rather than a word."""
    assert identifier_scan.confidence(term) == CERTAIN


def test_a_digit_without_a_separator_is_only_likely() -> None:
    """`FINDB7` is distinctive but not unmistakable, and the difference is the separator.

    A separator plus a digit is a shape prose never produces. `FINDB7` on its own could be a
    variable, a version or a column, so it is matched on a word boundary and reported one tier
    down rather than treated as proof.
    """
    assert identifier_scan.confidence("FINDB7") == LIKELY


@pytest.mark.parametrize("term", ["SALESDB", "SALESDB_STG", "SALESCLUSTER"])
def test_a_distinctive_token_is_likely(term: str) -> None:
    assert identifier_scan.confidence(term) == LIKELY


@pytest.mark.parametrize("term", ["Export", "Inventory", "Maintenance", "Damage"])
def test_an_ordinary_word_that_is_also_a_database_name_is_review_only(term: str) -> None:
    """These are real database names in this estate *and* ordinary English.

    Acting on them mechanically is how a scrub renames a Python function called `export`, so they
    are reported and deliberately kept out of the headline count.
    """
    assert identifier_scan.confidence(term) == REVIEW


# --------------------------------------------------------------------------- #
# Matching — the part that decides whether anyone trusts the number
# --------------------------------------------------------------------------- #
def _scan(tmp_path, text: str, terms: list[str], **over):
    (tmp_path / "sample.py").write_text(text, encoding="utf-8")
    request = {
        "root": str(tmp_path),
        "paths": ["sample.py"],
        "from_inventory": False,
        "extra_terms": terms,
    }
    request.update(over)
    return identifier_scan.scan(request)


def test_an_address_is_found_inside_a_longer_token(tmp_path) -> None:
    """An address must be found inside a `server_id`, so addresses match loosely, not whole-word.

    The address here is invented rather than taken from RFC 5737, because a documentation range is
    suppressed by design — using one would make this test pass for the wrong reason and stop
    proving that loose matching works at all.
    """
    outcome = _scan(tmp_path, 'TARGET = "ACME-10-1-2-3-MSSQL-1433"\n', ["10.1.2.3"])

    assert outcome["certain"] == 1
    assert outcome["hits"] == 1


def test_a_word_shaped_term_does_not_match_inside_an_identifier(tmp_path) -> None:
    """The single change that took this module from 1,550 hits to 80.

    A database called `Inventory` must not make every line of `inventory_report.py` a finding.
    """
    text = "from db_ops.reports import inventory_report\ninventory_rows = build_inventory()\n"
    outcome = _scan(tmp_path, text, ["Inventory"])

    assert outcome["hits"] == 0
    assert outcome["review"] == 0, "a lowercase variable is not the database name"


def test_a_word_shaped_term_is_still_found_when_it_stands_alone(tmp_path) -> None:
    """Case-sensitive and whole-word, but not blind: the real name still gets reported."""
    outcome = _scan(tmp_path, 'DATABASES = ["Inventory", "Export"]\n', ["Inventory"])

    assert outcome["review"] == 1
    assert outcome["hits"] == 0, "review findings never enter the number a gate acts on"


def test_a_short_code_does_not_match_a_longer_name_starting_with_it(tmp_path) -> None:
    """A short code against a longer name starting with it made the first run unreadable."""
    outcome = _scan(tmp_path, 'DB = "SALESDB7"\n', ["SALESDB"])

    assert outcome["hits"] == 0


# --------------------------------------------------------------------------- #
# Deliberate hits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("line", [
    'HOST = "192.0.2.10"',
    'SERVER_ID = "ACME-198-51-100-31"',
    'REF = "ORACLE_203_0_113_121_1522_SYS"',
])
def test_the_documentation_ranges_are_never_reported(line: str, tmp_path) -> None:
    """RFC 5737 exists so an example can never be someone's machine. Reporting it inverts the rule.

    This is not cosmetic: the first version of the scrub script flagged its own output, and a
    checker that reports success as failure is one the next person turns off.
    """
    outcome = _scan(tmp_path, line + "\n", ["192.0.2.10", "198.51.100.31", "203.0.113.121"])

    assert outcome["hits"] == 0
    assert outcome["allowed"] >= 1


def test_dockers_address_pool_is_not_an_estate_address(tmp_path) -> None:
    """`172.17.0.0/16` is a fact about Docker. `CONTRIBUTING.md` records it as a rule, not a case."""
    outcome = _scan(tmp_path, "  - subnet: 172.17.0.0/16\n", ["172.17.0.0"])

    assert outcome["hits"] == 0


def test_an_oracle_version_is_not_an_address(tmp_path) -> None:
    """`11.2.0.2` matches an IPv4 pattern and is a version. A blind regex corrupts it."""
    outcome = _scan(tmp_path, 'MIN_VERSION = "11.2.0.2"\n', ["11.2.0.2"])

    assert outcome["hits"] == 0


def test_a_caller_can_allow_a_line_it_knows_about(tmp_path) -> None:
    outcome = _scan(
        tmp_path,
        'HOST = "10.1.2.3"  # example: not a real machine\n',
        ["10.1.2.3"],
        allow=["# example:"],
    )

    assert outcome["hits"] == 0
    assert outcome["allowed"] == 1


# --------------------------------------------------------------------------- #
# Deriving the terms from configuration, which is the whole design
# --------------------------------------------------------------------------- #
def test_the_terms_come_from_the_inventory_rather_than_a_maintained_map(tmp_path) -> None:
    """A machine added to `db_instances.json` is searched for from that moment.

    The alternative — a map in `CONTRIBUTING.md` — is a second copy of the estate, and the two
    disagree the first time somebody adds a server without updating the map.
    """
    data = tmp_path / "data"
    data.mkdir()
    (data / "db_instances.json").write_text(json.dumps({"db_instances": [{
        "server_id": "ACME-192-0-2-77",
        "ip": "192.0.2.77",
        "site": "ACME",
        "service_name": "SALESDB-PROD",
        "database_names": ["SALESDB", "Export"],
        "env": "prod",
        "db_type": "sqlserver",
        "cmd_access": {"host": "192.0.2.77", "credential_name": "WIN_192_0_2_77_SVC"},
    }]}), encoding="utf-8")

    terms = identifier_scan.collect_identifiers(data)

    assert "192.0.2.77" in terms and "ACME-192-0-2-77" in terms
    assert "SALESDB" in terms and "WIN_192_0_2_77_SVC" in terms
    assert "prod" not in terms, "an environment label identifies nothing"
    assert "sqlserver" not in terms, "an engine name identifies nothing"


def test_a_vendor_default_is_not_treated_as_an_estate_name() -> None:
    """`MSSQLSERVER` is how Windows registers a default instance; every install has it.

    Scrubbing it would break the code that depends on the vendor's spelling, which is why the
    exclusion carries its reason rather than sitting in an unexplained set.
    """
    for term in ("mssqlserver", "freepdb1", "free_sb"):
        assert term in identifier_scan.GENERIC_TERMS
        assert identifier_scan.GENERIC_TERMS[term], f"{term} is excluded without a stated reason"


def test_every_exclusion_states_why() -> None:
    """An unexplained exclusion is how a scan quietly stops covering something."""
    unexplained = sorted(
        term for term, reason in identifier_scan.GENERIC_TERMS.items() if not str(reason).strip()
    )
    assert not unexplained, f"these are excluded with no reason given: {unexplained}"

    for fragment, reason in identifier_scan.ALWAYS_ALLOWED.items():
        assert str(reason).strip(), f"{fragment} is allowed with no reason given"


def test_a_missing_inventory_is_a_usage_error_not_a_clean_result(tmp_path) -> None:
    """The dangerous failure is reporting zero because there was nothing to look for."""
    with pytest.raises(identifier_scan.IdentifierScanError):
        identifier_scan.collect_identifiers(tmp_path / "nowhere")


def test_scanning_with_no_terms_refuses_rather_than_reporting_clean(tmp_path) -> None:
    with pytest.raises(identifier_scan.IdentifierScanError):
        identifier_scan.scan({"root": str(tmp_path), "from_inventory": False})
