"""No two databases share one fake name on a showcase (1.280).

Why it matters: 0.36.0's showcase listed ``PAYROLL`` twice in one server's backup coverage table -
two real databases of different names, 16 and 10 characters, that hashed to the one fake. Nothing
leaked, but the page read as one database backed up twice, and a showcase that misstates the
estate's shape documents nothing. The operator, 2026-11-09: one fake on two *different* servers is
acceptable, never within one server. A database name reaches the scrub as text, with no server
beside it, so the mapping keeps every database's fake unique - the per-server rule, and more.
"""

from __future__ import annotations

from dbabrain.lib import pseudonym

#: Two names whose first pick is the same fake (``pseudonym.database`` gives both ``POLICY``).
ALIKE = ("APPDB000", "APPDB001")


def test_the_two_names_do_hash_alike_so_this_test_tests_something():
    assert pseudonym.database(ALIKE[0]) == pseudonym.database(ALIKE[1])


def test_two_databases_that_hash_alike_get_two_fakes():
    mapping = pseudonym.Mapping({ALIKE[0]: "database", ALIKE[1]: "database"})
    first, second = mapping.as_dict()[ALIKE[0]], mapping.as_dict()[ALIKE[1]]
    assert first != second


def test_the_collision_resolves_the_same_way_whatever_order_the_names_came_in():
    forward = pseudonym.Mapping({ALIKE[0]: "database", ALIKE[1]: "database"}).as_dict()
    backward = pseudonym.Mapping({ALIKE[1]: "db_name", ALIKE[0]: "database"}).as_dict()
    assert forward == backward


def test_one_database_spelt_in_another_case_keeps_one_fake():
    mapping = pseudonym.Mapping({"PAYROLL_Prod": "database", "payroll_prod": "database"})
    assert mapping.as_dict()["PAYROLL_Prod"].casefold() == mapping.as_dict()["payroll_prod"].casefold()


def test_a_large_estate_past_the_word_pool_still_gets_a_distinct_name_each():
    names = {f"APPDB{i:03d}": "database" for i in range(120)}
    fakes = [fake.casefold() for fake in pseudonym.Mapping(names).as_dict().values()]
    assert len(set(fakes)) == len(fakes) == 120


def test_a_database_keeps_its_environment_suffix_on_a_later_pick():
    mapping = pseudonym.Mapping({f"APP{i:02d}_Prod": "database" for i in range(40)})
    assert all(fake.endswith("_Prod") for fake in mapping.as_dict().values())
