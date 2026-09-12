"""Where this installation's own pages are, and whether the published links still agree.

Someone reading `/spbot_self_status` on a phone got every fact about the process and no way to
reach anything it had produced - the URLs lived in a runbook and in an operator's memory. Worse,
the one place a URL *was* recorded, ``report_base_url``, had gone stale: the estate moved to a new
node on 2026-09-08 and the setting still named the retired worker, so every page rendered after the
move linked "Server dashboard" and "Fleet inventory" at a machine that runs nothing.

Both halves are tested here: the URLs are built from the port the webhost command really states,
and the disagreement between "where this node serves" and "where the pages say they are" is
reported rather than left for someone to discover by clicking.
"""

from __future__ import annotations

import json

from db_ops.lib import webhost_endpoints


def test_the_port_and_mount_come_from_the_command_that_actually_runs():
    parsed = webhost_endpoints.parse_serve_options(
        "python -m db_ops.webhost.cli --config config.json serve "
        "--root runtime/reports --mount report_dba --port 8080")
    assert parsed["port"] == 8080
    assert parsed["mount"] == "report_dba"


def test_a_command_that_omits_a_flag_gets_the_cli_s_own_default():
    parsed = webhost_endpoints.parse_serve_options("python -m db_ops.webhost.cli serve")
    assert parsed["port"] == webhost_endpoints.DEFAULT_PORT
    assert parsed["mount"] == webhost_endpoints.DEFAULT_REPORTS_MOUNT


def test_a_command_that_is_not_a_webhost_serve_describes_nothing():
    # An empty answer is the honest one: on a node with no webhost entry the pages are simply not
    # served here, which is different from being served on the default port.
    assert webhost_endpoints.parse_serve_options(
        "python -m db_ops.metrics.cli --config config.json collect") == {}


def test_a_mistyped_port_loses_that_one_flag_and_not_the_whole_report():
    parsed = webhost_endpoints.parse_serve_options(
        "python -m db_ops.webhost.cli serve --port eighty --mount report_dba")
    assert parsed["port"] == webhost_endpoints.DEFAULT_PORT
    assert parsed["mount"] == "report_dba"


def test_a_node_with_no_address_gets_no_urls_rather_than_urls_with_a_hole_in_them():
    assert webhost_endpoints.endpoints(host="") == {}


def test_the_stable_pages_hang_off_the_reports_mount():
    urls = webhost_endpoints.endpoints(host="192.0.2.10", port=8080)
    assert urls["reports"] == "http://192.0.2.10:8080/report_dba/"
    assert urls["console"] == "http://192.0.2.10:8080/db_ops/"
    assert urls["inventory"] == "http://192.0.2.10:8080/report_dba/database-inventory.html"
    assert urls["sla"] == "http://192.0.2.10:8080/report_dba/sla.html"


def test_the_per_server_page_stays_a_template_because_a_real_server_id_must_not_ship():
    urls = webhost_endpoints.endpoints(host="192.0.2.10")
    assert urls["index_usage"].endswith("index-usage_{server_id}.html")


def test_a_standard_port_is_left_out_of_the_url():
    assert webhost_endpoints.base_url(host="192.0.2.10", port=80, mount="report_dba") == (
        "http://192.0.2.10/report_dba/")


def _write_config(root, *, base_url, port=8080):
    (root / "app_commands.json").write_text(json.dumps({"app_commands": [
        {"app_command_id": 1, "command_text":
            f"python -m db_ops.webhost.cli --config config.json serve "
            f"--root runtime/reports --mount report_dba --port {port}",
         "enabled": True},
    ]}), encoding="utf-8")
    (root / "webhost_config.json").write_text(
        json.dumps({"web": {"mount": "db_ops"}}), encoding="utf-8")
    (root / "reports_config.json").write_text(
        json.dumps({"report_base_url": base_url}), encoding="utf-8")


def test_published_links_pointing_at_another_host_are_reported_as_a_disagreement(tmp_path, monkeypatch):
    # The 2026-09-10 finding, as a test: the node serves on .93 and every page it publishes says
    # .249. Nothing detected it until a reader clicked a link and landed on a retired machine.
    from db_ops.common import data_sources

    _write_config(tmp_path, base_url="http://192.0.2.249:8080/report_dba/")
    monkeypatch.setattr(data_sources, "_resolve_data_dir", lambda _=None: tmp_path)

    facts = data_sources.webhost_endpoints(tmp_path, host="192.0.2.93")
    assert facts["served_here"] is True
    assert facts["port"] == 8080
    assert facts["matches_published"] is False
    assert facts["published_base"] == "http://192.0.2.249:8080/report_dba/"


def test_a_base_url_naming_this_node_is_not_reported_as_a_problem(tmp_path, monkeypatch):
    from db_ops.common import data_sources

    _write_config(tmp_path, base_url="http://192.0.2.93:8080/report_dba/")
    monkeypatch.setattr(data_sources, "_resolve_data_dir", lambda _=None: tmp_path)

    facts = data_sources.webhost_endpoints(tmp_path, host="192.0.2.93")
    assert facts["matches_published"] is True


def test_the_status_block_names_every_page_and_the_warning_when_links_are_stale():
    from db_ops.common.self_status import _web_lines

    text = "\n".join(_web_lines({
        "served_here": True, "enabled": True,
        "urls": webhost_endpoints.endpoints(host="192.0.2.93"),
        "published_base": "http://192.0.2.249:8080/report_dba/",
        "matches_published": False,
    }))
    assert "http://192.0.2.93:8080/db_ops/" in text
    assert "database-inventory.html" in text
    assert "sla.html" in text
    assert "WARNING" in text and "192.0.2.249" in text


def test_a_node_that_does_not_serve_says_so_instead_of_inventing_a_port():
    from db_ops.common.self_status import _web_lines

    assert "not served from this node" in "\n".join(_web_lines({"served_here": False}))


def test_a_disabled_webhost_command_is_named_because_nothing_is_listening():
    from db_ops.common.self_status import _web_lines

    text = "\n".join(_web_lines({
        "served_here": True, "enabled": False,
        "urls": webhost_endpoints.endpoints(host="192.0.2.93"),
        "matches_published": True,
    }))
    assert "disabled" in text


def test_a_container_does_not_offer_its_bridge_address_as_a_link(tmp_path, monkeypatch):
    """Measured on the worker, 2026-09-10: `self-status` printed
    `http://172.30.240.2:8080/db_ops/` — Docker's private pool, routable from nowhere else.

    Inside a container the address a socket reports is the bridge address, and the published port
    is a mapping the container cannot see, so the node genuinely does not know how anyone reaches
    it. `report_base_url` is somebody stating exactly that fact, so it is used instead.
    """
    from db_ops.common import data_sources

    _write_config(tmp_path, base_url="http://192.0.2.20:8080/report_dba/")
    monkeypatch.setattr(data_sources, "_resolve_data_dir", lambda _=None: tmp_path)

    facts = data_sources.webhost_endpoints(tmp_path, host="172.30.240.2", runtime="docker")
    assert facts["address_source"] == "published"
    assert "172.30.240.2" not in facts["urls"]["console"]
    assert facts["urls"]["console"] == "http://192.0.2.20:8080/db_ops/"


def test_a_container_with_nothing_configured_prints_a_blank_to_fill_in(tmp_path, monkeypatch):
    # A placeholder is honest; a bridge address that looks clickable is not.
    from db_ops.common import data_sources
    from db_ops.lib import webhost_endpoints as lib

    _write_config(tmp_path, base_url="")
    monkeypatch.setattr(data_sources, "_resolve_data_dir", lambda _=None: tmp_path)

    facts = data_sources.webhost_endpoints(tmp_path, host="172.30.240.2", runtime="docker")
    assert facts["address_source"] == "placeholder"
    assert lib.PLACEHOLDER_HOST in facts["urls"]["console"]
    assert "172.30.240.2" not in facts["urls"]["console"]


def test_a_container_does_not_raise_the_stale_link_warning(tmp_path, monkeypatch):
    # The comparison would be a bridge address against a real one: always "different", never
    # meaningful. A warning that fires on every containerised node is noise.
    from db_ops.common import data_sources

    _write_config(tmp_path, base_url="http://192.0.2.20:8080/report_dba/")
    monkeypatch.setattr(data_sources, "_resolve_data_dir", lambda _=None: tmp_path)

    facts = data_sources.webhost_endpoints(tmp_path, host="172.30.240.2", runtime="docker")
    assert facts["matches_published"] is None


def test_a_node_on_the_operating_system_still_answers_for_itself(tmp_path, monkeypatch):
    from db_ops.common import data_sources

    _write_config(tmp_path, base_url="http://192.0.2.93:8080/report_dba/")
    monkeypatch.setattr(data_sources, "_resolve_data_dir", lambda _=None: tmp_path)

    facts = data_sources.webhost_endpoints(tmp_path, host="192.0.2.93", runtime="host")
    assert facts["address_source"] == "node"
    assert facts["matches_published"] is True


def test_a_published_base_is_split_back_into_its_parts():
    from db_ops.lib import webhost_endpoints as lib

    assert lib.parse_base_url("http://192.0.2.20:8080/report_dba/") == {
        "scheme": "http", "host": "192.0.2.20", "port": 8080, "mount": "report_dba"}
    assert lib.parse_base_url("https://reports.example.com/pages/")["port"] == 443
    assert lib.parse_base_url("") == {}
    assert lib.parse_base_url("not a url at all") == {}


def test_the_status_block_says_where_the_host_came_from():
    from db_ops.common.self_status import _web_lines
    from db_ops.lib import webhost_endpoints as lib

    text = "\n".join(_web_lines({
        "served_here": True, "enabled": True, "runtime": "docker",
        "address_source": "published", "matches_published": None,
        "urls": lib.endpoints(host="192.0.2.20"),
    }))
    assert "report_base_url" in text and "docker" in text
    assert "WARNING" not in text


def _write_root(root, *, worker_host, base_url="", port=8080):
    """A tool root: config.json beside a data/ folder, the way `init` lays one out."""
    data = root / "data"
    data.mkdir(exist_ok=True)
    _write_config(data, base_url=base_url, port=port)
    (root / "config.json").write_text(
        json.dumps({"worker": [{"node_id": "w", "host": worker_host, "user": "dev"}]}),
        encoding="utf-8")
    return data


def test_the_published_base_is_derived_from_the_worker_the_estate_already_declares(tmp_path):
    """Nobody should have to remember to re-type where the reports live.

    `config.json` -> `worker[].host` is the machine `deploy` connects to, and the webhost command
    already states the port and mount. Both move when the estate moves, so a derived answer follows
    it. The literal it replaced was wrong three times in one day on 2026-09-10.
    """
    from db_ops.common import data_sources

    data_sources._clear_report_base_url_cache()
    data = _write_root(tmp_path, worker_host="192.0.2.77", port=8081)
    assert data_sources.report_base_url(data) == "http://192.0.2.77:8081/report_dba/"


def test_an_explicit_setting_still_wins_because_a_proxy_or_a_dns_name_cannot_be_derived(tmp_path):
    from db_ops.common import data_sources

    data_sources._clear_report_base_url_cache()
    data = _write_root(tmp_path, worker_host="192.0.2.77",
                       base_url="https://reports.example.com/pages/")
    assert data_sources.report_base_url(data) == "https://reports.example.com/pages/"


def test_a_root_that_declares_no_worker_stays_empty_rather_than_inventing_a_host(tmp_path):
    # Empty means "not configured", which the callers already handle: the pages fall back to
    # relative hrefs and Telegram leaves the link out. A guessed host would 404 instead.
    from db_ops.common import data_sources

    data_sources._clear_report_base_url_cache()
    data = tmp_path / "data"
    data.mkdir()
    _write_config(data, base_url="")
    (tmp_path / "config.json").write_text(json.dumps({"worker": []}), encoding="utf-8")
    assert data_sources.report_base_url(data) == ""


def test_deriving_the_base_url_does_not_call_back_into_the_thing_that_asked_for_it(tmp_path):
    # webhost_endpoints asks report_base_url for the published base; report_base_url derives from
    # the same config. Routing the derivation through webhost_endpoints recursed forever.
    from db_ops.common import data_sources

    data_sources._clear_report_base_url_cache()
    data = _write_root(tmp_path, worker_host="192.0.2.77")
    facts = data_sources.webhost_endpoints(data, host="192.0.2.77")
    assert facts["published_base"] == "http://192.0.2.77:8080/report_dba/"
    assert facts["matches_published"] is True
