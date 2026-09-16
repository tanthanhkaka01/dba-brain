"""A node must publish links to pages *it* serves, and be able to say so with a command.

`report_base_url` in `data/reports_config.json` is the third field of its kind. `store_config.json`
got `db use-store` in v0.14.0 and `bot_telegram.json` got `telegram use-bot` on 2026-09-14, both
because a catalogued file travels inside a config bundle: `import-data` faithfully hands a machine
that has never run the *source's* identity, and nothing says so.

Measured 2026-09-14 on a node built for a soak: `self-status` printed
`WARNING: published links point at http://<the worker>:8080/report_dba/ - not this node`. The
warning was correct, and there was no command to act on it — the field was still a hand-edit, which
is the one thing this release exists to stop.

Empty is a state, not a fault: producers that build a page link fall back to a relative href, and
producers that need an absolute URL leave the link out. What must never happen is a confident link
to somebody else's machine.

Every address below is from RFC 5737, the range reserved for documentation. There is deliberately
no test asserting that: the first version of this file had one, spelled as a list of estate names
it must not find, and the export gate refused the tree over the list itself. G-02 scans the whole
exported tree on every export and is the enforcement; a test that re-implements it can only fail
the same way twice.
"""

from __future__ import annotations

import json

import pytest

from db_ops.reports.cli import _this_node_address, _use_base_url_command
from db_ops.reports.service import ReportWorkflowError
from db_ops.reports.use_base_url import BASE_URL_KEY, UseBaseUrlError, use_base_url


def written(root) -> dict:
    return json.loads((root / "reports_config.json").read_text(encoding="utf-8"))


def test_the_url_is_written_with_exactly_one_trailing_slash(tmp_path):
    """Every caller appends a page name to it. Two slashes or none both produce a 404."""
    result = use_base_url("http://192.0.2.10:8080/report_dba", data_dir=tmp_path)

    assert written(tmp_path)[BASE_URL_KEY] == "http://192.0.2.10:8080/report_dba/"
    assert result["now"] == "http://192.0.2.10:8080/report_dba/"
    assert result["written"] is True


def test_the_rest_of_the_reports_configuration_survives(tmp_path):
    (tmp_path / "reports_config.json").write_text(
        json.dumps({"notes": ["keep me"], "some_other_setting": 7}), encoding="utf-8")

    use_base_url("https://reports.example.com/", data_dir=tmp_path)

    document = written(tmp_path)
    assert document["notes"] == ["keep me"] and document["some_other_setting"] == 7


def test_it_says_which_address_it_replaced(tmp_path):
    """The mistake being prevented is *believing* the node is on the other one, and an
    acknowledgement does not disturb that belief."""
    (tmp_path / "reports_config.json").write_text(
        json.dumps({BASE_URL_KEY: "http://192.0.2.99:8080/report_dba/"}), encoding="utf-8")

    result = use_base_url("http://192.0.2.10:8080/report_dba/", data_dir=tmp_path)

    assert result["was"] == "http://192.0.2.99:8080/report_dba/"
    assert result["now"] == "http://192.0.2.10:8080/report_dba/"


def test_a_url_with_no_scheme_is_refused(tmp_path):
    """`192.0.2.10:8080/report_dba/` is read by a browser as a relative path, so every published
    link 404s — found in somebody's inbox rather than here, unless this refuses."""
    with pytest.raises(UseBaseUrlError, match="scheme"):
        use_base_url("192.0.2.10:8080/report_dba/", data_dir=tmp_path)

    assert not (tmp_path / "reports_config.json").exists()


def test_giving_nothing_is_refused(tmp_path):
    with pytest.raises(UseBaseUrlError, match="exactly one"):
        use_base_url(data_dir=tmp_path)


def test_giving_two_answers_is_refused(tmp_path):
    with pytest.raises(UseBaseUrlError, match="exactly one"):
        use_base_url("http://192.0.2.10:8080/", data_dir=tmp_path, clear=True)


def test_this_node_writes_the_address_this_node_answers_on(tmp_path):
    result = use_base_url(data_dir=tmp_path, this_node=True, host="192.0.2.10",
                          runtime="host", port=8080, mount="report_dba")

    assert result["now"] == "http://192.0.2.10:8080/report_dba/"


def test_this_node_is_refused_in_a_container(tmp_path):
    """The address a socket reports there is on the container's private pool and nobody outside
    reaches it. v0.4.0 shipped one as a clickable link."""
    with pytest.raises(UseBaseUrlError, match="docker"):
        use_base_url(data_dir=tmp_path, this_node=True, host="172.30.240.2", runtime="docker")

    assert not (tmp_path / "reports_config.json").exists()


def test_clear_goes_back_to_the_derived_answer(tmp_path):
    (tmp_path / "reports_config.json").write_text(
        json.dumps({BASE_URL_KEY: "http://192.0.2.99:8080/report_dba/"}), encoding="utf-8")

    result = use_base_url(data_dir=tmp_path, clear=True)

    assert written(tmp_path)[BASE_URL_KEY] == ""
    assert result["now"] == ""
    assert result["source"] in {"derived", "relative links only"}


def test_a_dry_run_writes_nothing(tmp_path):
    result = use_base_url("http://192.0.2.10:8080/report_dba/", data_dir=tmp_path, dry_run=True)

    assert result["written"] is False
    assert result["now"] == "http://192.0.2.10:8080/report_dba/"
    assert not (tmp_path / "reports_config.json").exists()


def test_the_command_runs_through_its_own_cli(tmp_path, monkeypatch, capsys):
    """Every test above calls `use_base_url` directly, and the command shipped broken anyway.

    `call_report_function` binds a handler's parameters by name out of `vars(args)`, so a handler
    asking for `args` is handed nothing: the first version failed with
    `missing 1 required keyword-only argument: 'args'`, and the second went looking for a
    `config.data_dir` that does not exist. Both were found running a release run sheet, because
    nothing here came through the front door.
    """
    from db_ops.reports import cli as reports_cli

    monkeypatch.setattr("db_ops.lib.paths.DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr("db_ops.reports.cli.DEFAULT_DATA_DIR", tmp_path, raising=False)

    code = reports_cli.main(["--config", "config.json", "use-base-url",
                            "http://192.0.2.10:8080/report_dba/", "--dry-run"])

    assert code == 0, capsys.readouterr().err
    assert "192.0.2.10" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Through the CLI handler, not the function under it. Everything above calls
# `use_base_url` directly, and that gap is why `--this-node` shipped twice broken: the handler
# asked for `args`, which `call_report_function` binds by name and never supplies
# (`_use_base_url_command() missing 1 required keyword-only argument: 'args'`, still in the
# estate's errors.log on 2026-09-15), and it reached this node's address by importing
# `db_ops.common.self_status` - which an app may not do.
# --------------------------------------------------------------------------- #
def test_the_command_reaches_this_node_through_the_common_cli(monkeypatch):
    """An app hands `common` a JSON object and reads one back. `self-status` is the command that
    answers what this installation is, so `--this-node` asks it rather than importing it."""
    from db_ops.lib import common_cli

    asked = []

    def fake_run(command, request, **kwargs):
        asked.append((command, request))
        return {"host": {"hostname": "node", "ip": "192.0.2.10"}, "runtime": "host"}

    monkeypatch.setattr(common_cli, "run", fake_run)

    assert _this_node_address() == ("192.0.2.10", "host")
    assert asked == [("self-status", {})]


def test_the_command_refuses_this_node_when_self_status_cannot_answer(monkeypatch):
    """Guessing the address writes links to a machine nobody can reach - the failure the whole
    command exists to stop. It has to refuse instead."""
    from db_ops.lib import common_cli

    def fake_run(command, request, **kwargs):
        raise common_cli.CommonCliError("self-status failed: no reason given")

    monkeypatch.setattr(common_cli, "run", fake_run)

    with pytest.raises(ReportWorkflowError, match="self-status did not answer"):
        _this_node_address()


def test_a_url_needs_no_address_at_all(monkeypatch, tmp_path):
    """Only `--this-node` has a use for the subprocess, so the other two forms must not pay for
    one - and must keep working on a node whose `self-status` is broken."""
    from db_ops.lib import common_cli

    def refuse(*_a, **_kw):
        raise AssertionError("use-base-url asked self-status for an address it does not need")

    monkeypatch.setattr(common_cli, "run", refuse)
    monkeypatch.setattr("db_ops.lib.paths.DEFAULT_DATA_DIR", tmp_path)

    result = _use_base_url_command(config=None, url="http://192.0.2.10:8080/report_dba/",
                                   dry_run=True)

    assert result["now"] == "http://192.0.2.10:8080/report_dba/"
