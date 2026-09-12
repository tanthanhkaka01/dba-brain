"""The inventory workflow on a node that has no canonical inventory yet.

The workflow updates a canonical `database-inventory.json` — the fleet as the operator describes it,
richer than `data/db_instances.json`. Turning it on without one (measured 2026-09-10, on a node
standing itself up) produced `[Errno 2] No such file or directory` with a path and nothing else, and
the path was not even the one `--inventory --help` named.

The 2026-09-10 answer was a better error. It was not enough: on 2026-09-11 an operator switched the
workflow on, got a 404 on `/report_dba/database-inventory.html`, and the node's `db_instances.json`
already named the one server it was collecting from. So the **default** location is now seeded from
`db_instances.json`. Two cases still do not seed, and each is pinned here:

* **an explicit `--inventory` that does not exist** — a typo there would quietly start a second
  inventory beside the real one, so it stays an error;
* **nothing registered at all** (a fresh install) — "not configured", reported, and **no file
  written**: an empty file would exist, never be seeded again, and list nothing for ever, because
  the merge only updates servers the file already lists.
"""

from __future__ import annotations

import json

import pytest

from db_ops.reports import inventory_summary


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    """A workflow whose health step reports one server, without a store behind it."""
    def write(servers):
        (tmp_path / "overlay.json").write_text(json.dumps({"servers": servers}), encoding="utf-8")
        monkeypatch.setattr(inventory_summary, "build_inventory_health",
                            lambda **kwargs: {"file": str(tmp_path / "overlay.json")})
    return write


def _instances(monkeypatch, servers):
    monkeypatch.setattr(inventory_summary, "load_inventory", lambda *a, **k: servers)


def _run(tmp_path):
    # `config` is what makes the default live under <runtime>/reports - the case the daemon runs.
    config = type("Config", (), {"runtime_dir": tmp_path})()
    (tmp_path / "reports").mkdir(exist_ok=True)
    return inventory_summary.build_inventory_workflow(sqlite_path=":memory:", config=config)


LAB = {"server_id": "LAB-192-0-2-15-MSSQL25-1433", "company_code": "LAB", "ip": "192.0.2.15",
       "databases": [{"db_type": "sqlserver", "service_name": "MSSQL25", "port": 1433,
                      "platform": "linux", "default_credential_name": "sa_lab",
                      "password_ref": "LAB_SA_PASSWORD", "server_id": "LAB-192-0-2-15-MSSQL25-1433"}]}


def test_the_default_inventory_is_seeded_from_db_instances_and_then_merged(tmp_path, monkeypatch, overlay):
    """The reported case: the workflow switched on, one instance registered, no canonical file."""
    overlay([{"server_id": LAB["server_id"], "ip": LAB["ip"], "instance_health": {"status": "OK"}}])
    _instances(monkeypatch, [LAB])

    result = _run(tmp_path)

    written = json.loads((tmp_path / "reports" / "database-inventory.json").read_text(encoding="utf-8"))
    assert result["status"] == "SUCCESS"
    assert result["seeded_from_db_instances"] == 1
    assert result["merged"] == 1, "a seed the overlay cannot merge into would publish an empty page"
    assert [s["server_id"] for s in written["servers"]] == [LAB["server_id"]]
    assert written["servers"][0]["instance_health"] == {"status": "OK"}


def test_the_seed_carries_identity_and_no_credential_reference(tmp_path, monkeypatch, overlay):
    """<runtime>/reports is the webhost's root: whatever the seed holds is served as a URL."""
    overlay([])
    _instances(monkeypatch, [LAB])

    _run(tmp_path)

    text = (tmp_path / "reports" / "database-inventory.json").read_text(encoding="utf-8")
    database = json.loads(text)["servers"][0]["databases"][0]
    assert database["db_type"] == "sqlserver" and database["service_name"] == "MSSQL25"
    assert "sa_lab" not in text and "LAB_SA_PASSWORD" not in text


def test_a_fresh_install_is_not_configured_and_writes_nothing(tmp_path, monkeypatch, overlay):
    """No inventory and no instance: a state, not an error - and above all no empty file, which
    would stop the seed from ever happening once the first instance is registered."""
    overlay([])
    _instances(monkeypatch, [])

    result = _run(tmp_path)

    assert result["status"] == "NOT_CONFIGURED"
    assert "db_instances.json" in result["message"]
    assert not (tmp_path / "reports" / "database-inventory.json").exists()


def test_the_first_run_after_an_instance_is_registered_seeds_it(tmp_path, monkeypatch, overlay):
    """The sequence a fresh install actually goes through, which an empty seed file would break."""
    overlay([])
    _instances(monkeypatch, [])
    assert _run(tmp_path)["status"] == "NOT_CONFIGURED"

    _instances(monkeypatch, [LAB])
    assert _run(tmp_path)["seeded_from_db_instances"] == 1


def test_an_existing_inventory_is_never_reseeded_but_a_registered_server_is_adopted(
        tmp_path, monkeypatch, overlay):
    """The file is still the operator's — and a registered, enabled server still reaches it.

    **This test said the opposite until 2026-09-12**, and the sentence it carried was: *once the
    file exists it is the operator's, and a server they removed from it must not come back because
    db_instances.json still names it.* That rule cost more than it protected. Seeding runs once and
    the overlay only updates servers the file already lists, so on a node whose canonical file held
    **one** server, the **forty-two** instances registered over the following day were collected
    from, alerted on, given their own index-usage pages — and never appeared on the fleet page.

    So registering an instance now puts it on the report, and the deliberate-removal case keeps a
    route that is better than deletion: switch it off in the register (`enabled: false`), where
    every other part of db_ops already reads the decision. `seeded_from_db_instances` stays 0 —
    adoption is not re-seeding, it adds what is missing and touches nothing else.
    """
    overlay([])
    _instances(monkeypatch, [LAB])
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "database-inventory.json").write_text('{"servers": []}', encoding="utf-8")

    result = _run(tmp_path)

    assert result["seeded_from_db_instances"] == 0
    assert result["adopted_from_db_instances"] == ["LAB-192-0-2-15-MSSQL25-1433"]
    written = json.loads((tmp_path / "reports" / "database-inventory.json").read_text(encoding="utf-8"))
    assert [s["server_id"] for s in written["servers"]] == ["LAB-192-0-2-15-MSSQL25-1433"]


def test_a_server_switched_off_in_the_register_is_not_adopted(tmp_path, monkeypatch, overlay):
    """`reports: {"enabled": false}` is how an operator says "registered, not reported".

    It is the counterweight to adoption overriding a deletion: without a supported way to exclude
    a machine, "registered" would mean "reported" and nothing could say otherwise.
    """
    overlay([])
    off = {**LAB, "databases": [{**LAB["databases"][0],
                                 "reports": {"enabled": False}}]}
    _instances(monkeypatch, [off])
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "database-inventory.json").write_text('{"servers": []}', encoding="utf-8")

    result = _run(tmp_path)

    assert result["adopted_from_db_instances"] == []
    written = json.loads((tmp_path / "reports" / "database-inventory.json").read_text(encoding="utf-8"))
    assert written["servers"] == []


def test_a_missing_canonical_inventory_says_what_it_is_and_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory_summary, "build_inventory_health",
                        lambda **kwargs: {"file": str(tmp_path / "overlay.json")})
    (tmp_path / "overlay.json").write_text(json.dumps({"servers": []}), encoding="utf-8")

    with pytest.raises(FileNotFoundError) as excinfo:
        inventory_summary.build_inventory_workflow(
            sqlite_path=":memory:", output_dir=tmp_path,
            inventory=tmp_path / "database-inventory.json")

    message = str(excinfo.value)
    assert "does not exist yet" in message
    # It has to say all three: what the file is, that the workflow does not create it, and the
    # supported way out. An error naming only the path reads as a bug in the tool.
    assert "merge" in message
    assert "--inventory" in message
    assert "init" in message


def test_a_dry_run_stops_before_needing_the_canonical_file(tmp_path, monkeypatch):
    # --dry-run builds the overlay only, so it must not be blocked by the file it never reads.
    monkeypatch.setattr(inventory_summary, "build_inventory_health",
                        lambda **kwargs: {"file": str(tmp_path / "overlay.json")})
    (tmp_path / "overlay.json").write_text(json.dumps({"servers": []}), encoding="utf-8")

    answer = inventory_summary.build_inventory_workflow(
        sqlite_path=":memory:", output_dir=tmp_path, dry_run=True,
        inventory=tmp_path / "database-inventory.json")
    assert answer["status"] == "SUCCESS"
    assert answer["dry_run"] is True


def test_the_help_describes_the_default_the_code_actually_uses():
    """The help and the code disagreed until 2026-09-10, and only the code was right."""
    from db_ops.reports import cli

    parser_source = open(cli.__file__, encoding="utf-8").read()
    marker = parser_source[parser_source.index('"--inventory"'):][:700]
    assert "runtime" in marker and "reports" in marker, (
        "the --inventory help must name the path the code defaults to")
