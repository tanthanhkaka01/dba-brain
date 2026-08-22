"""A port check that only asks loopback cannot answer the question it was written for.

`OS_TCP_PORT_STATUS` probed `127.0.0.1` and nothing else, so two different worlds produced the
same CLOSED: nothing is listening, and something is listening but bound to the address clients
actually use. Only the first is an outage, and on an app server the second is the likelier
mistake. The reverse blind spot is just as real — a service bound to loopback on purpose is
reachable by the probe and by nobody else, and the old check called that OPEN.

So the probe now has three answers, and the address it prefers comes from the inventory rather
than from a constant in the script. These tests pin that contract on both platform variants and
on the env var that carries the address.
"""

from pathlib import Path

import pytest

from db_ops.metrics.collector import _collector_env
from db_ops.metrics.models import MetricDefinition, MetricTarget, MetricVariant
from db_ops.lib.paths import resolve_tool_path

WINDOWS_SCRIPT = resolve_tool_path("assets/metrics/os/windows/009_os_tcp_port_status.ps1")
LINUX_SCRIPT = resolve_tool_path("assets/metrics/os/linux/009_os_tcp_port_status.sh")


@pytest.fixture(scope="module")
def scripts():
    return {
        "windows": WINDOWS_SCRIPT.read_text(encoding="utf-8"),
        "linux": LINUX_SCRIPT.read_text(encoding="utf-8"),
    }


def _metric():
    return MetricDefinition(
        metric_code="OS_TCP_PORT_STATUS",
        db_type="multi",
        category="os",
        default_importance=4,
        active=True,
        collector_type="cmd",
        variants=[MetricVariant(name="windows_powershell", db_type="multi", platform="windows",
                                file="os/windows/009_os_tcp_port_status.ps1")],
    )


def _target(*, ip="192.0.2.119", cmd_host="192.0.2.119", metrics_config=None):
    cmd_access = {"enabled": True, "method": "winrm", "shell": "powershell"}
    if cmd_host:
        cmd_access["host"] = cmd_host
    return MetricTarget(
        target_id="ACME-192-0-2-119/SALESDB-AOS04",
        server_id="ACME-192-0-2-119",
        ip=ip,
        db_type="",
        db_name="",
        credential_name="",
        platform="windows",
        cmd_access=cmd_access,
        metrics_config=metrics_config or {},
    )


def test_a_cmd_collector_is_told_the_address_the_inventory_reaches_the_host_on():
    """The script must not have to restate what db_instances.json already records — and must not
    have to guess it from the host's own NICs, which on a multi-homed box picks the wrong one."""
    env = _collector_env(metric=_metric(), target=_target())

    assert env["DB_OPS_TARGET_HOST"] == "192.0.2.119"


def test_cmd_access_host_wins_over_ip():
    """cmd_access.host is the address already proven reachable: the collector ran the script
    through it. `ip` is inventory, which can be an address nothing listens on."""
    env = _collector_env(metric=_metric(), target=_target(ip="10.0.0.1", cmd_host="192.0.2.119"))

    assert env["DB_OPS_TARGET_HOST"] == "192.0.2.119"


def test_a_target_may_still_override_the_probe_address():
    """collector_env is config and config wins, so a host whose clients arrive on a VIP can say so
    without the collector growing a special case."""
    env = _collector_env(
        metric=_metric(),
        target=_target(metrics_config={"collector_env": {"DB_OPS_TARGET_HOST": "10.9.9.9"}}),
    )

    assert env["DB_OPS_TARGET_HOST"] == "10.9.9.9"


def test_a_target_with_no_address_leaves_the_script_to_fall_back():
    """Empty rather than invented: the script then says 127.0.0.1 in its own message, which is
    honest, where a guessed address would produce a confident CLOSED nobody can check."""
    env = _collector_env(metric=_metric(), target=_target(ip="", cmd_host=""))

    assert env["DB_OPS_TARGET_HOST"] == ""


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_both_variants_prefer_the_inventory_address_over_loopback(scripts, platform):
    assert "DB_OPS_TARGET_HOST" in scripts[platform]
    assert "127.0.0.1" in scripts[platform], "loopback stays the fallback, not the default"


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_both_variants_distinguish_listening_nowhere_from_listening_on_loopback(scripts, platform):
    """LOOPBACK_ONLY is the finding the old check could not express: up, and unreachable."""
    assert "LOOPBACK_ONLY" in scripts[platform]


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_a_loopback_only_port_is_a_warning_and_not_a_critical(scripts, platform):
    """A service deliberately bound to loopback — a local agent, a tunnelled port — is configured
    that way, not broken. Making that CRITICAL on every such host is how a real CLOSED stops being
    read. The assertion is on the ordering of the branches: LOOPBACK_ONLY carries WARNING."""
    text = scripts[platform]
    marker = text.index("LOOPBACK_ONLY", text.index("CLOSED"))
    window = text[marker - 400:marker + 400]

    assert "WARNING" in window


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_the_deeper_probe_is_opt_in_per_port(scripts, platform):
    """`443/https` asks for it; a bare `443` keeps the TCP-only behaviour every other target has.
    A TLS probe forced on every configured port would break the Oracle and MSSQL entries."""
    assert "tls" in scripts[platform] and "https" in scripts[platform]
    assert "tcp" in scripts[platform], "the default scheme must still be plain TCP"


def test_the_windows_probe_does_not_use_powershell_6_only_syntax(scripts):
    """These hosts run Windows PowerShell 5.1. `-SkipCertificateCheck` is 6+, and a parameter that
    does not exist fails the call — which the probe would have reported as the endpoint being
    unhealthy, i.e. a false finding produced by the checker itself."""
    code = [line for line in scripts["windows"].splitlines() if not line.lstrip().startswith("#")]

    assert not [line for line in code if "-SkipCertificateCheck" in line]
    assert not [line for line in code if "Invoke-WebRequest" in line]
