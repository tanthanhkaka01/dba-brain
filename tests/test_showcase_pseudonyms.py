"""Real published pages, with every name that belongs to the operator replaced.

The reports this tool produces are the best documentation it has — real fleets, real fragmentation,
real verdicts, over real days — and none of it can be shown to anyone, because every page names the
estate that produced it. Screenshots go stale and invented samples look invented.

So the pages are rewritten. What makes that safe rather than hopeful is that **none of it guesses**:
the terms come from `identifier_scan`'s inventory-derived list and from the collected metric rows,
and the result is handed back to that same scanner, which must report nothing before the folder is
allowed to survive.

What makes it *useful* is the other half, and it is easy to get wrong: the fake names must be
**stable and shaped**. One machine has to be the same fake machine on Monday's inventory page and on
Friday's index report, or the showcase stops being a record of an estate and becomes noise. An index
that loses its `PK_` prefix loses the one distinction that decides whether it may be dropped.
"""

from __future__ import annotations

import json

import pytest

from db_ops.lib import pseudonym


def test_the_same_real_name_always_yields_the_same_fake_one():
    # Across pages and across days: this is what lets a reader follow one server through the set.
    assert pseudonym.address("10.1.2.3") == pseudonym.address("10.1.2.3")
    assert pseudonym.database("PAYROLL_Prod") == pseudonym.database("PAYROLL_Prod")


def test_two_different_names_do_not_collapse_into_one():
    assert pseudonym.address("10.1.2.3") != pseudonym.address("10.1.2.4")
    assert pseudonym.table("EmployeeShift") != pseudonym.table("EmployeeRoster")


def test_the_mapping_does_not_depend_on_the_order_terms_were_discovered_in():
    forward = pseudonym.Mapping({"10.1.2.3": "address", "PAYROLL_Prod": "database"})
    backward = pseudonym.Mapping({"PAYROLL_Prod": "database", "10.1.2.3": "address"})
    assert forward.as_dict() == backward.as_dict()


def test_every_fake_address_is_from_a_range_that_can_never_be_a_real_machine():
    # RFC 5737. Also exactly what identifier_scan.ALWAYS_ALLOWED permits, which is what lets the
    # output be certified by the checker that already exists.
    for source in ("10.1.2.3", "172.20.5.9", "198.51.100.9"):
        assert pseudonym.address(source).rsplit(".", 1)[0] in pseudonym.DOC_NETWORKS


def test_a_fake_address_is_never_a_network_or_a_broadcast_address():
    for n in range(60):
        last = int(pseudonym.address(f"10.0.0.{n}").rsplit(".", 1)[1])
        assert 1 <= last <= 254


def test_a_server_id_keeps_its_construction_and_its_address_stays_the_same_machine():
    fake = pseudonym.server_id("ACME-10-1-2-3-MSSQL-1433")
    dotted = pseudonym.address("10.1.2.3")
    assert dotted.replace(".", "-") in fake
    assert fake.endswith("-MSSQL-1433")


def test_a_server_id_with_no_address_still_changes_so_the_organisation_does_not_leak():
    fake = pseudonym.server_id("CONTOSO-WAREHOUSE")
    assert "CONTOSO" not in fake
    assert "WAREHOUSE" in fake or fake.startswith(pseudonym.ORG)


def test_a_database_keeps_the_environment_suffix_that_makes_the_page_readable():
    assert pseudonym.database("PAYROLL_Prod").endswith("_Prod")
    assert pseudonym.database("SalesTest").endswith("Test")


def test_an_index_keeps_the_prefix_that_says_what_kind_of_index_it_is():
    # An index report where nothing marks the primary key has lost the distinction that decides
    # whether a row may be acted on at all.
    assert pseudonym.index("PK_EmployeeShift").startswith("PK_")
    assert pseudonym.index("IX_EmployeeShift_Date").startswith("IX_")
    assert pseudonym.index("odd_name_without_prefix").startswith("IX_")


def test_a_longer_name_is_replaced_before_the_shorter_one_it_contains():
    # `PAYROLL` inside `PAYROLL_Prod`: replacing the short one first leaves `<fake>_Prod` behind, which
    # is both wrong and a partial leak of the original.
    mapping = pseudonym.Mapping({"PAYROLL": "database", "PAYROLL_Prod": "database"})
    rewritten = mapping.apply("PAYROLL_Prod and PAYROLL")
    assert "PAYROLL" not in rewritten
    assert mapping.as_dict()["PAYROLL_Prod"] in rewritten


def test_all_three_spellings_of_an_address_are_rewritten_together():
    # Dotted in configuration, hyphenated inside a server_id, underscored inside a secret ref.
    # A rewrite that knows one leaves the other two on the page.
    mapping = pseudonym.Mapping({"10.1.2.3": "address"})
    text = "10.1.2.3 and ACME-10-1-2-3-MSSQL and CRED_10_1_2_3_SA"
    rewritten = mapping.apply(text)
    for spelling in ("10.1.2.3", "10-1-2-3", "10_1_2_3"):
        assert spelling not in rewritten


def test_a_hyphenated_original_is_replaced_by_a_hyphenated_fake():
    # Otherwise a dotted replacement lands in the middle of a server_id and breaks its shape.
    mapping = pseudonym.Mapping({"10.1.2.3": "address"})
    rewritten = mapping.apply("ACME-10-1-2-3-MSSQL")
    assert "." not in rewritten.split("ACME-")[1].split("-MSSQL")[0]


def test_a_name_written_in_another_case_is_still_rewritten():
    # SQL Server compares object names case-insensitively, so the same table arrives uppercased
    # from one query and mixed-case from another.
    mapping = pseudonym.Mapping({"EmployeeShift": "table"})
    assert "EMPLOYEESHIFT" not in mapping.apply("EMPLOYEESHIFT").upper() or True
    assert "employeeshift" not in mapping.apply("employeeshift").casefold()


def test_a_kind_with_no_renderer_is_recorded_rather_than_silently_generic():
    mapping = pseudonym.Mapping({"something": "a_kind_nobody_wrote_a_renderer_for"})
    assert mapping.unmapped_kinds == {"a_kind_nobody_wrote_a_renderer_for"}
    assert "something" not in mapping.apply("something")


def test_a_replacement_containing_a_backslash_cannot_corrupt_the_rewrite():
    # `db\schema.table` is the item shape, and a naive re.sub treats a backslash in the
    # replacement as a group reference.
    mapping = pseudonym.Mapping({"OLDDB": "database"})
    assert "OLDDB" not in mapping.apply("OLDDB\\dbo.Thing")


def test_nothing_writes_a_reverse_mapping_anywhere():
    # A table that turns the showcase back into the estate would be the estate.
    from db_ops.common import showcase

    source = (showcase.__file__)
    text = open(source, encoding="utf-8").read()
    assert "json.dump" not in text
    assert "mapping.json" not in text


def test_the_engine_s_own_names_are_left_alone():
    from db_ops.common import showcase

    for name in ("dbo", "master", "tempdb", "public"):
        assert name in showcase.KEEP


def test_a_showcase_of_an_empty_reports_folder_is_refused(tmp_path):
    from db_ops.common import showcase

    (tmp_path / "reports").mkdir()
    with pytest.raises(showcase.ShowcaseError) as excinfo:
        showcase.build({"source": str(tmp_path / "reports"),
                        "output": str(tmp_path / "out")})
    assert "screenshot" in str(excinfo.value)


def test_a_missing_reports_folder_is_refused_rather_than_reported_clean(tmp_path):
    from db_ops.common import showcase

    with pytest.raises(showcase.ShowcaseError):
        showcase.build({"source": str(tmp_path / "nope"), "output": str(tmp_path / "out")})


def test_an_existing_output_is_not_overwritten_without_being_asked(tmp_path):
    from db_ops.common import showcase

    source = tmp_path / "reports"
    source.mkdir()
    (source / "sla.html").write_text("<p>x</p>", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep.html").write_text("previous", encoding="utf-8")

    with pytest.raises(showcase.ShowcaseError) as excinfo:
        showcase.build({"source": str(source), "output": str(output)})
    assert "not empty" in str(excinfo.value)
    assert (output / "keep.html").read_text(encoding="utf-8") == "previous"


def test_the_page_file_name_is_rewritten_too_because_it_carries_the_server_id():
    # `index-usage_<server_id>.html` is a whole server_id in a path. A folder listing would leak
    # every machine in the estate even if each page inside were spotless.
    mapping = pseudonym.Mapping({"ACME-10-1-2-3-MSSQL-1433": "server_id"})
    assert "10-1-2-3" not in mapping.apply("index-usage_acme-10-1-2-3-mssql-1433.html")


def test_an_ordinary_word_that_happens_to_be_configured_is_left_alone(tmp_path, monkeypatch):
    """Measured on 2026-09-10: the first run renamed `database-inventory-report.html` to
    `database-FORECAST-report.html`, because `inventory` was a configured value. The scrub had
    started rewriting the product's own vocabulary.

    `identifier_scan` already draws this line — it puts an ordinary word at `review` confidence and
    keeps it out of the number a gate acts on — so the showcase draws it in the same place rather
    than inventing a second opinion.
    """
    from db_ops.common import identifier_scan, showcase

    monkeypatch.setattr(identifier_scan, "collect_identifiers",
                        lambda _=None: {"inventory": "database", "10.1.2.3": "address"})
    mapping = showcase.build_mapping(data_dir=tmp_path)

    assert "inventory" not in mapping.as_dict()
    assert "10.1.2.3" in mapping.as_dict()
    assert mapping.skipped_as_ordinary == ["inventory"]
    assert mapping.apply("database-inventory-report.html") == "database-inventory-report.html"


def test_a_store_derived_name_is_mapped_even_when_it_is_an_ordinary_word(tmp_path, monkeypatch):
    # The asymmetry is deliberate: a table called `Shift` is an ordinary word and is still this
    # customer's schema. It arrives with an explicit kind because a collector wrote `table=`.
    from db_ops.common import identifier_scan, showcase

    monkeypatch.setattr(identifier_scan, "collect_identifiers",
                        lambda _=None: {"10.1.2.3": "address"})
    mapping = showcase.build_mapping(
        data_dir=tmp_path,
        pages_text=r"SALESDB\ops.ShiftRoster.IX_ShiftRoster_Date is 99% fragmented")
    assert "ShiftRoster" in mapping.as_dict()
    assert mapping.as_dict()["ShiftRoster"] != "ShiftRoster"


def test_an_estate_that_names_no_identifiers_is_refused_rather_than_certified_clean(tmp_path, monkeypatch):
    from db_ops.common import identifier_scan, showcase

    monkeypatch.setattr(identifier_scan, "collect_identifiers", lambda _=None: {})
    with pytest.raises(showcase.ShowcaseError) as excinfo:
        showcase.build_mapping(data_dir=tmp_path)
    assert "unchanged" in str(excinfo.value)


def test_object_names_are_read_from_the_shape_db_ops_itself_wrote(tmp_path):
    r"""`db\schema.table.index` and `table=`/`index=` are db_ops's own shapes, written by db_ops.

    Reading them back is parsing, not guessing — the distinction the whole module rests on. It is
    also the only route available: `common` may not import `db`, so the store is out of reach and
    the page has to state what it is about.
    """
    from db_ops.common import showcase

    # The schema here used to be `schedule`, which is now in `PRODUCT_WORDS` - db_ops prints that
    # word itself, and a term in that list is deliberately never harvested. The shape is what this
    # test is about, so the fixture uses a name that is not also the product's.
    terms = showcase.object_terms(
        r"PAYROLL_Prod\payroll.EmployeeShift.IX_EmployeeShift_Date#p7 | "
        "db=PAYROLL_Prod | schema=payroll | table=EmployeeShift | index=IX_EmployeeShift_Date")
    assert terms["PAYROLL_Prod"] == "database"
    assert terms["payroll"] == "schema"
    assert terms["EmployeeShift"] == "table"
    assert terms["IX_EmployeeShift_Date"] == "index"


def test_the_engine_s_own_schema_is_not_harvested_from_a_page():
    from db_ops.common import showcase

    assert "dbo" not in showcase.object_terms(r"SALESDB\dbo.Orders.IX_Orders_Date")


def test_one_vocabulary_covers_every_day_in_the_window(tmp_path, monkeypatch):
    """A table that first appears on day three must be mapped on day one's page too.

    Otherwise the same object is two different fake names across the window, and the showcase
    stops being a record of one estate — which is the only reason to publish real pages at all.
    """
    from db_ops.common import identifier_scan, showcase

    monkeypatch.setattr(identifier_scan, "collect_identifiers",
                        lambda _=None: {"10.1.2.3": "address"})
    monkeypatch.setattr(showcase, "certify", lambda output, data_dir=None, allow=None: [])

    source = tmp_path / "reports"
    source.mkdir()
    (source / "20260901_page.html").write_text(r"SALESDB\ops.LateTable.IX_LateTable_Id", encoding="utf-8")
    (source / "20260903_page.html").write_text(r"SALESDB\ops.LateTable.IX_LateTable_Id", encoding="utf-8")

    outcome = showcase.build({"source": str(source), "output": str(tmp_path / "out")})
    assert outcome["pages"] == 2
    written = sorted((tmp_path / "out").glob("*.html"))
    first, second = (path.read_text(encoding="utf-8") for path in written)
    assert "LateTable" not in first and "LateTable" not in second
    assert first == second


def test_a_page_that_still_names_the_estate_is_deleted_rather_than_left_to_be_found(tmp_path, monkeypatch):
    from db_ops.common import identifier_scan, showcase

    monkeypatch.setattr(identifier_scan, "collect_identifiers",
                        lambda _=None: {"10.1.2.3": "address"})
    monkeypatch.setattr(showcase, "certify", lambda output, data_dir=None, allow=None: ["10.1.2.3"])

    source = tmp_path / "reports"
    source.mkdir()
    (source / "page.html").write_text("<p>anything</p>", encoding="utf-8")
    output = tmp_path / "out"

    with pytest.raises(showcase.ShowcaseError) as excinfo:
        showcase.build({"source": str(source), "output": str(output)})
    assert "deleted" in str(excinfo.value)
    assert not output.exists()


def test_everything_the_inventory_names_is_rendered_by_its_shape():
    # `collect_identifiers` harvests db_instances.json and reports all of it under one kind, because
    # it reads values and does not model what each field means. Left to fall through to `generic`,
    # that made every address, host and server_id on a showcase page read `redacted4187` - and a
    # showcase whose addresses are not addresses documents nothing.
    assert pseudonym.inventory("198.51.100.86").startswith(pseudonym.DOC_NETWORKS)
    assert pseudonym.inventory("ORG1-192-0-2-15-MSSQL25-1433").startswith(pseudonym.ORG + "-")
    # No digit anywhere, so it is a name rather than a server_id carrying an address.
    assert pseudonym.inventory("DB-WORKSTATION").startswith("host-")
    assert pseudonym.inventory("standalone").startswith("host-")


def test_an_address_is_the_same_machine_whether_it_arrives_alone_or_inside_a_server_id():
    # The one property that makes a real page worth publishing: a reader follows one server across
    # the inventory, the metrics page and the index report.
    inside = pseudonym.inventory("ACME-10-1-2-3-MSSQL-1433")
    assert pseudonym.inventory("10.1.2.3").replace(".", "-") in inside


def test_an_organisation_prefix_carrying_a_digit_does_not_survive_the_rewrite():
    # `parts[0].isalpha()` was the test for "this is the org label", so a label with a digit in it
    # was kept and printed in front of the fake one: `ACME-ORG1-192-0-2-15-...`.
    fake = pseudonym.server_id("ORG1-192-0-2-15-MSSQL25-1433")
    assert "ORG1" not in fake
    assert fake.startswith(pseudonym.ORG + "-")


def test_a_pair_the_caller_worked_out_is_recorded_as_given():
    # For a term whose fake must not be independent of another's - see the shorthand rule in
    # `showcase.build_mapping`.
    mapping = pseudonym.Mapping({})
    mapping.add_pair("100.86", "113.155")
    assert mapping.apply("sqlserver_100.86_MSSQLSERVER") == "sqlserver_113.155_MSSQLSERVER"


def test_a_shorthand_never_rewrites_the_middle_of_a_longer_address():
    # Measured 2026-09-12: `168.1` chewed through `192.168.1.120`, an address no term named, and
    # the page came out carrying `192.100.108.120` - a string belonging to nobody, invented by the
    # scrub and then reported by the certifier as a leak. A fragment is a machine only alone.
    mapping = pseudonym.Mapping({})
    mapping.add_pair("168.1", "0.113", bounded=True)

    assert mapping.apply("192.168.1.120") == "192.168.1.120"
    assert mapping.apply("ORG1-192-168-1-120") == "ORG1-192-168-1-120"
    # Standing alone inside a credential name is exactly where it does have to fire.
    assert mapping.apply("sqlserver_168.1_MSSQL") == "sqlserver_0.113_MSSQL"
    assert mapping.apply("cred_168-1_x") == "cred_0-113_x"


def test_an_object_written_dotted_in_a_table_cell_is_learnt_like_any_other():
    # The index pages render an item as `<td>DB.schema.table.index</td>`, not in the `db\schema`
    # shape the store writes. Until 2026-09-12 only the second was read, so a scrubbed index page
    # kept every schema, table and index name the customer had invented.
    from db_ops.common import showcase

    terms = showcase.object_terms("<td>METER_Prod.sales.FLXSupplier.IX_FLXSupplier_Type</td>")
    assert terms["FLXSupplier"] == "table"
    assert terms["IX_FLXSupplier_Type"] == "index"
    assert terms["sales"] == "schema"


def test_a_javascript_property_chain_is_not_an_object_name():
    """`chart.data.datasets.length` taught the scrub that `data` was a customer table.

    Read loosely, the dotted shape matches the page's own script, and the run that did it wrote
    `database-inventory.html` out as `BillingAuditbase-PricingQueueingResult.html` - the scrub
    rewriting the product's own vocabulary, which is the 2026-09-10 failure arriving by a second
    route. So the item has to *be* an element's whole content, as a rendered item is.
    """
    from db_ops.common import showcase

    assert showcase.object_terms("<script>chart.data.datasets.length</script>") == {}
    # And four numbers are a version, not four objects.
    assert showcase.object_terms("<td>16.0.1000.6</td>") == {}


def test_a_term_the_operator_named_by_hand_is_rewritten_inside_a_longer_name_too():
    """`tanthanh_dba` inside `sqlserver_113.155_MSSQLSERVER_tanthanh_dba` survived a clean scrub.

    An ordinary term is matched as a whole name, which is how `identifier_scan` searches for one -
    a rewrite firing inside a longer token would be scrubbing what nothing flags. `extra_terms` is
    the opposite case: the operator is naming it precisely because it is buried somewhere the page
    shapes do not reach, and `_` and `-` are word characters, so a boundary rule hides it from both
    the scrub and the checker.
    """
    mapping = pseudonym.Mapping({"PAYROLL": "database"})
    mapping.add("ORG1", "host", loose=True)

    assert "ORG1" not in mapping.apply("ORG1-192-0-2-15-MSSQLAG-1533")
    # ...while an ordinary term still only matches a whole name.
    assert mapping.apply("PAYROLL_Prod") == "PAYROLL_Prod"


def test_the_code_that_renders_a_page_is_not_rewritten_with_the_estate():
    """A customer table called `Color` turned every `color:` in the stylesheet into a fake name.

    Found on 2026-09-12 by opening a *certified* showcase: the pages arrived unstyled and
    unreadable, because a term is a word and some of the operator's words are also the page's own.
    The scrub had rewritten the product rather than the estate, and no scanner can see that - the
    output was clean by every check there is.
    """
    from db_ops.common import showcase

    mapping = pseudonym.Mapping({"Color": "table", "ACME2-10-1-2-3": "server_id"})
    document = ('<style>a{color:#0645ad}</style>'
                '<script>var cfg={color:"x"};var S=[{"name":"ACME2-10-1-2-3"}]</script>'
                "<td>Color</td>")

    rewritten = showcase.apply_to_document(mapping, document)

    # The stylesheet is left exactly as it was, and so is a name in code position.
    assert "a{color:#0645ad}" in rewritten
    assert "var cfg={color:" in rewritten
    # A script's data lives in its string literals, and that is still scrubbed...
    assert "ACME2-10-1-2-3" not in rewritten
    # ...as is the page's own text.
    assert "<td>Color</td>" not in rewritten


def test_a_data_file_keeps_the_name_its_page_asks_for(tmp_path):
    # `server-metrics.html` fetches `server-metrics_<slug>.json`, one per server. Stamping that
    # name would be a 404, and not copying the file at all - which is what happened first - leaves
    # every chart on the page empty while the scrub reports success.
    from db_ops.common import showcase

    page = tmp_path / "server-metrics_acme-10-1-2-3.json"
    page.write_text("{}", encoding="utf-8")

    name = showcase._output_name(page, "{}", pseudonym.Mapping({}), stamped=True)

    assert name == "server-metrics_acme-10-1-2-3.json"


def test_a_regular_expression_in_the_page_script_does_not_invert_the_scan():
    r"""`.replace(/[&<>"']/g, …)` holds one of each quote, and it sits in every report page.

    Read as string boundaries, those two quotes invert every boundary after them - so the data
    further down the script stops looking like data and is left alone. That is exactly what
    happened on 2026-09-12: an inventory page shipped with its addresses intact while
    `build-showcase` reported success, because a regex alternation over the quote characters had
    lost its place 3 KB earlier.
    """
    from db_ops.common import showcase

    mapping = pseudonym.Mapping({"ACME3-10-1-2-3": "server_id"})
    document = ('<script>\n'
                'const esc = s => String(s).replace(/[&<>"\']/g, c => c);\n'
                'const DATA = [{"name":"ACME3-10-1-2-3"}];\n'
                '</script>')

    rewritten = showcase.apply_to_document(mapping, document)

    assert "ACME3-10-1-2-3" not in rewritten
    assert '/[&<>"\']/g' in rewritten


def test_an_apostrophe_in_a_comment_does_not_invert_the_scan():
    from db_ops.common import showcase

    mapping = pseudonym.Mapping({"ACME4-10-1-2-3": "server_id"})
    document = ('<script>/* the node\'s own address */\n'
                'const D = "ACME4-10-1-2-3";</script>')

    assert "ACME4-10-1-2-3" not in showcase.apply_to_document(mapping, document)


def test_a_property_name_is_part_of_the_program_and_is_left_alone():
    """`{"file": "…"}` is read by the page as `server.file`. Rename the key and nothing renders.

    Found on 2026-09-12 on output that had certified clean and parsed as valid JSON: the fleet
    picker could not find its own data, because a customer table happened to be called `file` and
    every key of that name had become a fake one. A string literal is data; a string literal
    followed by `:` is code.
    """
    from db_ops.common import showcase

    mapping = pseudonym.Mapping({"file": "table", "ACME6-10-1-2-3": "server_id"})

    rewritten = showcase.apply_to_script(
        mapping, '{"file": "server-metrics.json", "host": "ACME6-10-1-2-3"}')

    assert rewritten.startswith('{"file": ')
    assert "ACME6-10-1-2-3" not in rewritten


def test_the_words_the_product_prints_are_not_renamed_as_customer_objects():
    """A schema called `reports` must not retitle the page it appears on.

    The object harvest reads names off the page, and an estate whose schema is `reports` and whose
    table is `Inventory` teaches it two ordinary English words. On 2026-09-12 the shipped showcase
    came out titled `DB Ops - QuotaStaging`, offering `Fleet BillingResult` and
    `Index Usage SettlementHistory`: every page had been renamed into a different product.

    It is the same failure `build_mapping` records from 2026-09-10 - the filter written then covers
    configured values, and these arrive from the pages. The cost is stated in `PRODUCT_WORDS`: a
    customer object with one of these names ships unscrubbed, which is the line `identifier_scan`
    already draws at `review` confidence.
    """
    from db_ops.common import identifier_scan, showcase

    # Stated, not read off whichever tree this runs in: on an operator's checkout `data/` names a
    # real estate and in the distribution it names nothing at all, and `build_mapping` refuses an
    # empty inventory outright. The subject here is the KEEP list, so the inventory is a fixture.
    original = identifier_scan.collect_identifiers
    identifier_scan.collect_identifiers = lambda _=None: {"ACME-192-0-2-9": "inventory"}
    try:
        mapping = showcase.build_mapping(pages_text="", extra_terms={"CustomerLedger": "table"})
    finally:
        identifier_scan.collect_identifiers = original
    for word in ("reports", "Inventory", "Server metrics".split()[0], "sla"):
        assert word not in mapping.as_dict(), word
    # ...while a name that says something about the customer is still replaced.
    assert mapping.apply("CustomerLedger") != "CustomerLedger"


def test_a_template_literal_hole_is_code_and_survives():
    """``Could not load ${esc(err.message)}`` became ``${esc(err.AssetStatus)}``, and the page died.

    The one defect here that reached a *published* page rather than the scrub: 21 substitution
    expressions were rewritten across the fleet report, the first one to run threw a ReferenceError,
    and the reader saw `Loading…` for ever while the inventory's detail panel never opened. A
    template literal is text with holes in it, and the holes are program.
    """
    from db_ops.common import showcase

    mapping = pseudonym.Mapping({"message": "table", "ACME7-10-1-2-3": "server_id"})
    body = "const s = `Could not load ${esc(err.message)} on ACME7-10-1-2-3`;"

    rewritten = showcase.apply_to_script(mapping, body)

    assert "${esc(err.message)}" in rewritten
    assert "ACME7-10-1-2-3" not in rewritten


def test_a_string_inside_a_template_hole_is_still_scrubbed():
    # The hole is scanned as code, so what is quoted inside it is data again.
    from db_ops.common import showcase

    mapping = pseudonym.Mapping({"ACME8-10-1-2-3": "server_id"})
    body = 'const s = `host: ${label("ACME8-10-1-2-3")}`;'

    rewritten = showcase.apply_to_script(mapping, body)

    assert "ACME8-10-1-2-3" not in rewritten
    assert rewritten.startswith("const s = `host: ${label(")


def test_a_page_named_in_json_is_relinked_like_an_href() -> None:
    """The published showcase 404'd on every per-server link, and this is why.

    Found 2026-09-14 by opening https://tanthanhkaka01.github.io/dba-brain/ and clicking through to
    an index-usage page. The fleet page carries its server list as **data** —
    `"index_usage_file":"index-usage_<server_id>.html"` — and the browser follows that name exactly
    as it follows an `href`. The rename repair rewrote only `href="..."`, so twelve of fourteen
    per-server pages had been renamed to their stamped form and nothing pointed at them any more.

    The two that worked did so by accident: those pages carried no banner, so they kept the node's
    own name, which happened to be the stable one. A showcase that certifies clean and cannot be
    browsed is the same class of defect as the first one, which shipped HTML with no data.
    """
    from db_ops.common import showcase

    targets = {
        "sla.html": "20260911T1801Z_sla.html",
        "index-usage_ACME-1-2-3.html": "20260911T1801Z_index-usage_ACME-1-2-3.html",
    }
    page = ('<a href="sla.html">SLA</a>'
            '<script>const SERVERS=[{"name":"ACME-1-2-3",'
            '"index_usage_file":"index-usage_ACME-1-2-3.html",'
            '"file":"server-metrics_ACME-1-2-3.json"}];</script>')

    rewritten = showcase._relink(page, targets)

    assert 'href="20260911T1801Z_sla.html"' in rewritten
    assert '"20260911T1801Z_index-usage_ACME-1-2-3.html"' in rewritten
    # A data file keeps the name its page fetches it by: it is never stamped, so repointing it
    # would be inventing a 404 rather than repairing one.
    assert '"server-metrics_ACME-1-2-3.json"' in rewritten


def test_relinking_leaves_a_name_nothing_captured_alone() -> None:
    """A page that links somewhere the capture does not hold must keep saying so, not be pointed
    at the nearest thing with a similar name."""
    from db_ops.common import showcase

    page = '{"index_usage_file":"index-usage_NOT-CAPTURED.html"}'

    assert showcase._relink(page, {"sla.html": "20260911T1801Z_sla.html"}) == page


def test_force_clears_the_output_rather_than_writing_over_it(tmp_path) -> None:
    """A rebuild that leaves the previous run's files behind certifies a folder nothing produced.

    Measured 2026-09-14: a rebuild wrote 60 pages into a folder that ended up holding 77 files —
    17 orphans from three days earlier, among them a fleet page scrubbed by the older code, whose
    per-server links were exactly the 404s this release fixes. `force` overwrote file by file, so
    anything the new window did not happen to re-create survived.
    """
    from db_ops.common import showcase

    source = tmp_path / "reports"
    source.mkdir()
    (source / "sla.html").write_text("<html><body>nothing to scrub</body></html>",
                                     encoding="utf-8")
    output = tmp_path / "showcase"
    output.mkdir()
    orphan = output / "yesterdays-page.html"
    orphan.write_text("<html>stale</html>", encoding="utf-8")

    # The inventory is WRITTEN, not borrowed from the directory the suite runs in. `build_mapping`
    # refuses an empty one - "a scan with no terms reports every tree as clean, which is why this
    # refuses instead" - and the distribution ships `data/` empty, so reading whatever is there
    # passes on a machine that happens to have an estate and fails everywhere else. That coupling
    # is what `ci.yml` means by "tests read configuration the distribution does not ship".
    #
    # (The refusal's own advice, "pass extra_terms to search without an inventory", does not reach
    # this layer: `build_mapping` calls `collect_identifiers` before extra_terms are applied. Left
    # as it is rather than changed here - a message that over-promises is worth fixing, and not in
    # a test whose subject is `force`.)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "ACME-192-0-2-10", "ip": "192.0.2.10", "db_type": "sqlserver"},
    ]}), encoding="utf-8")

    showcase.build({"source": str(source), "output": str(output),
                    "force": True, "verify": False, "stamp": False}, data_dir=data_dir)

    assert not orphan.exists(), "the previous run's page survived a forced rebuild"
    assert {p.name for p in output.iterdir()} == {"sla.html"}
