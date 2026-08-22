"""What a host listens on, and what that rules in or out.

Three throwaway socket loops had answered this question by 2026-08-19, each with its own port list
and its own reading of the result — the exact failure mode `CLAUDE.md` names when it says a task
needing a scratch script belongs in `db_ops.common.cli` instead.

The part worth testing is not the socket. It is the **verdict**, because the three outcomes lead to
three different follow-ups and two of them are routinely confused: a host answering only on RDP is
not down, and a host answering on nothing at all is not "WinRM needs enabling". The `.235`/`.236`
pair is the case that forced the distinction — both are up, both refuse 5985, and on Windows Server
2003 no amount of enabling will change that.
"""

import pytest

from db_ops.common import host_probe
from db_ops.lib.target_profile import TargetProfile


def _probe(monkeypatch, open_ports, states=None, request=None, instance=None):
    """Run a probe with the sockets faked, so only the interpretation is under test."""
    states = states or {}

    def fake_probe_port(host, port, timeout=3.0):  # noqa: ARG001
        state = states.get(port, "open" if port in open_ports else "timeout")
        return {"port": port, "service": host_probe.PORT_NAMES.get(port, ""),
                "open": state == "open", "state": state, "elapsed_ms": 1}

    monkeypatch.setattr(host_probe, "probe_port", fake_probe_port)
    payload = {"host": "10.0.0.9"}
    payload.update(request or {})
    return host_probe.probe(payload, instance=instance)


def test_an_ssh_or_winrm_answer_means_a_script_can_get_in(monkeypatch):
    outcome = _probe(monkeypatch, [22, 3389])
    assert outcome["verdict"] == "manageable"
    assert outcome["management_ports"] == [22]
    assert "ssh:22" in outcome["detail"]


def test_rdp_alone_is_administered_by_hand_not_unreachable(monkeypatch):
    """The distinction a scratch script never draws, and the difference between "the host is gone"
    and "there is no way in from a script" — which lead to completely different follow-ups."""
    outcome = _probe(monkeypatch, [3389, 445])
    assert outcome["verdict"] == "interactive_only"
    assert "administered" in outcome["detail"]


def test_windows_2003_is_told_it_cannot_be_fixed_by_enabling_a_service(monkeypatch):
    """Without the OS, "WinRM is not listening" invites somebody to go and enable it — which on
    Server 2003 is an afternoon spent discovering it cannot be done. 192.0.2.235 and .236 are
    the two hosts here, and both carry cmd_access: null for exactly this reason."""
    outcome = _probe(monkeypatch, [3389, 135, 445],
                     request={"os": "Windows Server 2003", "platform": "windows"})
    assert outcome["verdict"] == "interactive_only"
    assert "no WinRM" in outcome["detail"] and "OpenSSH" in outcome["detail"]


def test_a_newer_windows_gets_the_ordinary_wording(monkeypatch):
    """The refusal-to-hope only applies where it is true; on 2012 the service really can be
    enabled, and saying otherwise would be worse than saying nothing."""
    outcome = _probe(monkeypatch, [3389],
                     request={"os": "Windows NT 6.2 (Build 9200)", "platform": "windows"})
    assert outcome["verdict"] == "interactive_only"
    assert "no WinRM" not in outcome["detail"]


def test_a_database_port_without_a_management_port_is_its_own_answer(monkeypatch):
    outcome = _probe(monkeypatch, [1433])
    assert outcome["verdict"] == "service_only"
    assert "sqlserver:1433" in outcome["detail"]


def test_nothing_answering_says_whether_the_host_is_at_least_up(monkeypatch):
    """A refusal is the machine answering that nothing listens; a timeout is nothing answering at
    all. On .236 that distinction was the evidence the box was reachable and simply has no WinRM."""
    refused = _probe(monkeypatch, [], states={5985: "refused", 22: "refused"})
    assert refused["verdict"] == "unreachable" and "it is up" in refused["detail"]

    silent = _probe(monkeypatch, [])
    assert silent["verdict"] == "unreachable" and "host down" in silent["detail"]


def test_the_inventory_supplies_the_ip_and_os_but_the_request_still_wins(monkeypatch):
    """Same precedence as everywhere else: the caller is looking at the server, the record is a
    file somebody typed. `sources` says which side answered, per field."""
    instance = {"server_id": "ACME-192-0-2-236", "ip": "192.0.2.236",
                "os": "Windows Server 2003", "db_type": "oracle"}
    outcome = _probe(monkeypatch, [3389], request={"host": ""}, instance=instance)

    assert outcome["host"] == "192.0.2.236"
    assert outcome["server_id"] == "ACME-192-0-2-236"
    assert outcome["profile"]["sources"]["os_text"] == "config"


def test_a_request_naming_no_host_at_all_is_refused():
    with pytest.raises(host_probe.HostProbeError, match='give a "host"'):
        host_probe.probe({})


@pytest.mark.parametrize("bad", [0, 65536, "winrm", None])
def test_a_value_that_is_not_a_port_is_refused_rather_than_skipped(bad):
    """Silently dropping it would report a clean sweep of the ports that happened to parse."""
    with pytest.raises(host_probe.HostProbeError, match="not a port number"):
        host_probe.probe({"host": "10.0.0.9", "ports": [22, bad]})


def test_the_module_reads_no_file():
    """The layering rule this module was rewritten for mid-change: `common/cli.py` resolves a
    `target` to an ip and hands the record down; deciding what open ports mean is a question about
    the arguments (docs/13_common.md rule 3, tests/test_common_layers.py)."""
    import ast
    from pathlib import Path

    source = Path(host_probe.__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for name in ([a.name for a in node.names] + ([node.module or ""] if isinstance(node, ast.ImportFrom) else []))
    }
    assert "data_sources" not in imported
    assert not any(name.startswith("db_ops.common.data_sources") for name in imported)


def test_probe_port_tells_a_refusal_from_a_timeout():
    """Against a closed port on this machine: a real socket, because the whole value of the
    distinction is that it comes from the OS rather than from an interpretation."""
    result = host_probe.probe_port("127.0.0.1", 1, timeout=2.0)
    assert result["open"] is False
    assert result["state"] in {"refused", "timeout", "error"}
    assert result["port"] == 1 and "elapsed_ms" in result


def test_the_default_sweep_covers_both_management_transports_and_rdp():
    """RDP is in the default list for one reason: without it, "interactive_only" and "unreachable"
    are indistinguishable, and they are the two answers most often confused."""
    assert set(host_probe.MANAGEMENT_PORTS) <= set(host_probe.DEFAULT_PORTS)
    assert 3389 in host_probe.DEFAULT_PORTS


def test_a_windows_2003_profile_is_the_one_case_with_no_transport_at_all():
    from db_ops.lib.target_profile import windows_management_transport_available

    assert windows_management_transport_available(
        TargetProfile(platform="windows", os_major=5, os_minor=2)) is False
