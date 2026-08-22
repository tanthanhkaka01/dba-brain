import json

import pytest

from db_ops.common import data_sources as target_resolve
from db_ops.common.data_sources import (
    TargetResolveError,
    parse_target_spec,
    resolve_target_instance,
    list_target_instances,
    format_target_list,
)


def write_instances(data_dir, instances):
    (data_dir / "db_instances.json").write_text(
        json.dumps({"db_instances": instances}, ensure_ascii=False), encoding="utf-8"
    )


SAMPLE = [
    {"server_id": "ACME-192-0-2-248", "db_type": "sqlserver", "ip": "192.0.2.248", "port": 1433,
     "instance_name": "SQLEXPRESS", "enabled": True},
    {"server_id": "ACME-192-0-2-249-MSSQLAG-1533", "db_type": "sqlserver", "ip": "192.0.2.249",
     "port": 1533, "instance_name": "ha-primary", "enabled": True},
    {"server_id": "ACME-192-0-2-249-MSSQLAG-1534", "db_type": "sqlserver", "ip": "192.0.2.249",
     "port": 1534, "instance_name": "ha-standby", "enabled": True},
    {"server_id": "ACME-192-0-2-235", "db_type": "oracle", "ip": "192.0.2.235", "port": 1521,
     "instance_name": "LEGACYDB", "enabled": True},
    {"server_id": "ACME-192-0-2-116", "db_type": None, "ip": "192.0.2.116", "port": None,
     "instance_name": "", "enabled": True},  # OS-only host, no database
]


def test_parse_target_spec_forms():
    assert parse_target_spec("ACME-192-0-2-248") == {"kind": "server_id", "server_id": "ACME-192-0-2-248"}
    assert parse_target_spec("mssql 192.0.2.248") == {
        "kind": "triple", "db_type": "sqlserver", "ip": "192.0.2.248", "port": None}
    assert parse_target_spec("mssql 192.0.2.248 1433") == {
        "kind": "triple", "db_type": "sqlserver", "ip": "192.0.2.248", "port": 1433}


def test_parse_target_spec_colon_port_form():
    # ip:port matches the /spbot_list_server_id output — accept it on input too.
    assert parse_target_spec("mssql 192.0.2.248:1433") == {
        "kind": "triple", "db_type": "sqlserver", "ip": "192.0.2.248", "port": 1433}


def test_parse_target_spec_errors():
    with pytest.raises(TargetResolveError):
        parse_target_spec("")
    with pytest.raises(TargetResolveError):
        parse_target_spec("mssql 192.0.2.248 notaport")
    with pytest.raises(TargetResolveError):
        parse_target_spec("mssql 192.0.2.248:notaport")
    with pytest.raises(TargetResolveError, match="port given twice"):
        parse_target_spec("mssql 192.0.2.248:1433 1533")
    with pytest.raises(TargetResolveError):
        parse_target_spec("a b c d")


def test_resolve_by_server_id(tmp_path):
    write_instances(tmp_path, SAMPLE)
    inst = resolve_target_instance("ACME-192-0-2-248", data_dir=tmp_path)
    assert inst["server_id"] == "ACME-192-0-2-248"


def test_resolve_by_dbtype_ip_takes_top_1_when_port_omitted(tmp_path):
    write_instances(tmp_path, SAMPLE)
    # Two sqlserver instances share 192.0.2.249; omitting the port takes the first.
    inst = resolve_target_instance("mssql 192.0.2.249", data_dir=tmp_path)
    assert inst["server_id"] == "ACME-192-0-2-249-MSSQLAG-1533"


def test_resolve_by_dbtype_ip_port_picks_exact(tmp_path):
    write_instances(tmp_path, SAMPLE)
    inst = resolve_target_instance("sqlserver 192.0.2.249 1534", data_dir=tmp_path)
    assert inst["server_id"] == "ACME-192-0-2-249-MSSQLAG-1534"


def test_resolve_by_dbtype_ip_colon_port(tmp_path):
    write_instances(tmp_path, SAMPLE)
    inst = resolve_target_instance("mssql 192.0.2.249:1534", data_dir=tmp_path)
    assert inst["server_id"] == "ACME-192-0-2-249-MSSQLAG-1534"


def test_resolve_unknown_server_id_raises(tmp_path):
    write_instances(tmp_path, SAMPLE)
    with pytest.raises(TargetResolveError, match="Unknown server_id"):
        resolve_target_instance("NOPE", data_dir=tmp_path)


def test_resolve_no_matching_triple_raises(tmp_path):
    write_instances(tmp_path, SAMPLE)
    with pytest.raises(TargetResolveError, match="No target matches"):
        resolve_target_instance("mysql 10.0.0.1", data_dir=tmp_path)


def test_list_skips_os_only_hosts(tmp_path):
    write_instances(tmp_path, SAMPLE)
    targets = list_target_instances(tmp_path)
    server_ids = [t["server_id"] for t in targets]
    assert "ACME-192-0-2-116" not in server_ids  # no db_type -> skipped
    assert len(targets) == 4


def test_format_target_list_is_readable(tmp_path):
    write_instances(tmp_path, SAMPLE)
    text = format_target_list(tmp_path)
    assert "ACME-192-0-2-248 | sqlserver 192.0.2.248:1433 | SQLEXPRESS" in text
    assert "<db_type> <ip> [port]" in text


def test_the_target_listing_offers_only_targets_that_can_be_used(tmp_path):
    """The listing is meant to be copied from into another command, so a disabled target has no
    business in it - the resolver would refuse the id the operator just copied."""
    import json
    from db_ops.common.data_sources import format_target_list

    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "LIVE", "db_type": "sqlserver", "ip": "10.0.0.1", "port": 1433, "enabled": True},
        {"server_id": "RETIRED", "db_type": "sqlserver", "ip": "10.0.0.2", "port": 1433, "enabled": False},
    ]}), encoding="utf-8")

    text = format_target_list(tmp_path)

    assert "LIVE" in text
    assert "RETIRED" not in text
    assert "1 inactive target hidden" in text


def test_resolution_still_sees_disabled_targets_so_it_can_explain_itself(tmp_path):
    """Hiding is a listing concern only. If resolution also forgot disabled instances, asking
    for one would report "unknown server_id" for an id that is right there in the config."""
    import json
    from db_ops.common.data_sources import list_target_instances

    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "RETIRED", "db_type": "sqlserver", "ip": "10.0.0.2", "port": 1433, "enabled": False},
    ]}), encoding="utf-8")

    assert [t["server_id"] for t in list_target_instances(tmp_path)] == ["RETIRED"]
