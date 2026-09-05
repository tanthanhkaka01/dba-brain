"""Two ways a real name reached a published package while the gate said "clean".

The scan derives what to search for from the inventory, which is the right design and the reason
it improves on its own. But between *knowing* a term and *reporting* it there were four filters,
and each threw away something real. Two of them are the subject here; the other two - an
unprinted list of addresses no configuration names, and a skip list matched against the absolute
path so a tree under a directory called `build` was never opened - are pinned at the bottom:

* a value that names two things - `server_name` is `HOST\\INSTANCE` - was searched for only as the
  pair, so prose naming the machine on its own matched nothing. The same shortfall had a rule for
  addresses already ("prose does not repeat a whole address"); it was never generalised to names;
* the `review` tier, which exists so the gate is not refused over the word "inventory", was
  excluded from the printed report as well as from the refusal - so a term the scan matched was
  never seen by anyone.

Both were measured on 2026-09-05, in a package that was already on the index.
"""

from db_ops.common import identifier_scan as scan


def test_a_host_and_instance_pair_teaches_the_scan_the_host_on_its_own():
    """A `HOST\\INSTANCE` value in the inventory has to make the host a term on its own. A comment
    naming the machine is how one actually shipped, and no comment writes the instance suffix."""
    terms: dict[str, str] = {}

    scan._harvest("EXAMPLE-SQL\\SQLEXPRESS", terms, "inventory")

    assert "EXAMPLE-SQL" in terms, "the host on its own is what prose writes"
    assert "EXAMPLE-SQL\\SQLEXPRESS" in terms, "and the pair is still searched for"


def test_the_parts_of_a_share_and_a_path_are_harvested_too():
    terms: dict[str, str] = {}

    scan._harvest("EXAMPLE-SQL$SQLEXPRESS", terms, "inventory")
    scan._harvest("DBA/SqlBK/EXAMPLE-HOST", terms, "inventory")

    assert "EXAMPLE-SQL" in terms
    assert "EXAMPLE-HOST" in terms


def test_an_address_is_never_split_into_its_octets():
    """`192.0.2.10` is dotted, and splitting it would make `192` a search term - which matches
    every port, size and version number in the tree."""
    terms: dict[str, str] = {}

    scan._harvest("192.0.2.10", terms, "inventory")

    assert "192" not in terms
    assert [t for t in terms if t.startswith("192")] == ["192.0.2.10"]


def test_a_part_that_is_only_digits_does_not_become_a_term():
    """A composite carrying a port or an id must not turn the number into something to search for."""
    terms: dict[str, str] = {}

    scan._harvest("EXAMPLE-HOST,1433", terms, "inventory")

    assert "1433" not in terms
    assert "EXAMPLE-HOST" in terms


def test_a_generic_half_is_still_excluded_after_splitting():
    """Splitting must not smuggle past the exclusion list what the whole value would have hit."""
    terms: dict[str, str] = {}

    scan._harvest("EXAMPLE-HOST\\localhost", terms, "inventory")

    assert "localhost" not in terms


def test_the_scan_reports_the_review_tier_it_refuses_to_fail_on(tmp_path):
    """Counted and listed, so a person can read it once. Refusing over it is what the tier exists
    to avoid; hiding it is what let a database name ship."""
    (tmp_path / "sample.py").write_text('LABEL = "Ledger"\n', encoding="utf-8")

    outcome = scan.scan({"root": str(tmp_path), "paths": ["."], "extra_terms": ["Ledger"],
                            "from_inventory": False})

    assert outcome["hits"] == 0, "an ordinary word must not refuse the tree"
    assert outcome["review"] >= 1, "but it must be counted"
    assert outcome["review_only_files"], "and the file named"


def test_a_scan_that_opens_no_file_refuses_instead_of_reporting_clean(tmp_path):
    """The module already refuses when it has no terms, for the reason it states: zero terms means
    every tree scans clean. Zero *files* produces the same answer and had no such guard - which is
    how a scan rooted under a skipped directory name reported a tree it never opened as clean."""
    import pytest

    empty = tmp_path / "nothing-here"
    empty.mkdir()

    with pytest.raises(scan.IdentifierScanError) as refusal:
        scan.scan({"root": str(empty), "paths": ["."], "extra_terms": ["EXAMPLE-HOST"],
                   "from_inventory": False})

    assert "reports every tree as clean" in str(refusal.value)


def test_a_skipped_directory_name_above_the_root_does_not_empty_the_scan(tmp_path):
    """`build`, `dist`, `deploy` and `runtime` are directories inside the tree being scanned. A
    tree that merely lives under a directory with one of those names is still a tree."""
    root = tmp_path / "deploy" / "exported"
    root.mkdir(parents=True)
    (root / "sample.py").write_text('HOST = "EXAMPLE-HOST"\n', encoding="utf-8")

    outcome = scan.scan({"root": str(root), "paths": ["."], "extra_terms": ["EXAMPLE-HOST"],
                            "from_inventory": False})

    assert outcome["files_scanned"] == 1, "the file is inside the root, not inside a build dir"
    assert outcome["hits"] >= 1


def test_a_name_is_matched_whatever_case_it_is_written_in(tmp_path):
    """The inventory holds a service in upper case; a doc wrote it in title case and matched
    nothing. Names are matched case-insensitively for that reason - and the ordinary-word tier is
    not, because matching `export` as well as `Export` is what produced a report nobody read."""
    (tmp_path / "note.md").write_text("the two Widgetworks commands are gone\n", encoding="utf-8")

    outcome = scan.scan({"root": str(tmp_path), "paths": ["."],
                         "extra_terms": ["WIDGETWORKS"], "from_inventory": False})

    assert outcome["hits"] == 1, "the service is the same service in any case"
    assert outcome["files"][0]["terms"] == ["WIDGETWORKS"], "reported under the configured spelling"


def test_a_case_insensitive_match_is_classified_not_dropped(tmp_path):
    """The half-fix that hid it: the pattern matched, then the hit was looked up under the text
    found rather than the configured spelling, missed, and was skipped. Matched-then-dropped is
    indistinguishable from never matched, and it is the failure this whole file is about."""
    (tmp_path / "note.md").write_text("HOST widgetworks and Widgetworks\n", encoding="utf-8")

    outcome = scan.scan({"root": str(tmp_path), "paths": ["."],
                         "extra_terms": ["WIDGETWORKS"], "from_inventory": False})

    assert outcome["hits"] == 2
    assert outcome["likely"] == 2, "classified at the configured term's tier, not dropped to review"
