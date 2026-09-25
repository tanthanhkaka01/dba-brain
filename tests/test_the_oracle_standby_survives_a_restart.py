"""The Oracle Data Guard lab's standby comes back a mounted standby after any restart.

On 2026-09-24 a lab built at 13:22-13:25 (+07) ended its setup with *"Data Guard lab ready …
physical standby, MOUNTED"* - and after one host reboot its standby crash-looped: 10 restarts in a
minute, each `CONTAINER: database already initialized` -> `Database opened.` -> `DATABASE STARTUP
FAILED!`. The standby ran under the `gvenzl/oracle-free` entrypoint, which starts every database as
a primary; under `restart: unless-stopped` that failure repeats for ever, and the shipper sidecar
refused every cycle (`ORA-01034`). The lab's own last line promised *"The standby needs nothing
after a container restart"*.

So the standby has its own start: until the setup has converted it (a marker in its data volume)
it starts the image's way, because the setup needs an ordinary database first; after that it is
only ever MOUNTED. And its healthcheck reports a mounted physical standby as healthy - the image's
own check called it "unhealthy" for as long as the lab lived.
"""

from __future__ import annotations

import pytest

from db_ops.common.docker_db import compose as compose_mod
from db_ops.common.docker_db import templates
from db_ops.lib.docker_db_spec import DockerDbSpec


@pytest.fixture(scope="module")
def files() -> dict[str, str]:
    spec = DockerDbSpec(name="ora_dg", engine="oracle", version="23.26.3", mode="ha-lab",
                        replicas=1, host_port=15210, password_env="ORA_PW")
    plan = compose_mod.build_plan(spec, containers_dir="/opt/db_ops/containers", password=None,
                                  worker_host="192.0.2.9", dry_run=True)
    return {f.relpath: f.content for f in plan.files}


def _service(compose: str, name: str) -> str:
    start = compose.index(f"  {name}:\n")
    end = compose.find("\n  ora_dg", start + 5)
    return compose[start:end if end > 0 else None]


def test_the_standby_starts_through_its_own_script_and_the_primary_does_not(files):
    compose = files["docker-compose.yml"]
    standby, primary = _service(compose, "ora_dg-standby-1"), _service(compose, "ora_dg-primary")

    assert 'entrypoint: ["/bin/bash", "/dg/standby_entrypoint.sh"]' in standby
    assert "./dg:/dg:ro" in standby and "/dg/standby_healthcheck.sh" in standby
    assert "entrypoint" not in primary and '["CMD-SHELL", "healthcheck.sh"]' in primary
    assert '"15210:1521"' in primary and '"15211:1521"' in standby


def test_the_scripts_ship_with_the_lab(files):
    assert "dg/standby_entrypoint.sh" in files and "dg/standby_healthcheck.sh" in files


def test_before_the_conversion_it_starts_the_images_way(files):
    script = files["dg/standby_entrypoint.sh"]
    assert templates.ORACLE_DG_STANDBY_MARKER in script
    assert 'exec container-entrypoint.sh "$@"' in script.split("fi", 1)[0]


def test_after_the_conversion_it_is_mounted_and_never_opened(files):
    converted = files["dg/standby_entrypoint.sh"].split("fi", 1)[1]
    assert "STARTUP MOUNT" in converted
    assert "ALTER DATABASE OPEN" not in converted.upper()
    assert "STARTUP\n" not in converted, "a bare STARTUP opens the database"
    assert "container-entrypoint.sh" not in converted, "the image's start is what opened it"


def test_a_stop_shuts_the_standby_down_cleanly(files):
    script = files["dg/standby_entrypoint.sh"]
    assert "trap stop TERM INT" in script and "SHUTDOWN IMMEDIATE" in script


def test_a_mounted_physical_standby_is_healthy(files):
    check = files["dg/standby_healthcheck.sh"]
    assert "PHYSICAL STANDBY:MOUNTED" in check
    assert "exec healthcheck.sh" in check, "before the conversion the image's own check applies"


def test_the_setup_marks_the_standby_right_after_the_duplicate(files):
    setup = files["setup/setup_dataguard.sh"]
    marker = f'touch "{templates.ORACLE_DG_STANDBY_MARKER}"'
    assert setup.index("NOFILENAMECHECK;") < setup.index(marker) < setup.index("== 5. Silence")


def test_the_closing_lines_promise_only_what_a_restart_does(files):
    setup = files["setup/setup_dataguard.sh"]
    assert "needs nothing after a container restart" not in setup
    assert "unhealthy' from now on" not in setup
    assert "brings it back MOUNTED" in setup


def test_the_shipper_reads_the_standbys_names_every_cycle(files):
    """Read once when the sidecar started - before the setup had converted the standby - they
    stayed empty, and the sidecar of a freshly built lab skipped every cycle for ever: only the
    setup's own ONESHOT cycle ever shipped a log (2026-09-24)."""
    ship = files["ship/ship_loop.sh"]
    cycle = ship.split("ship_cycle() {", 1)[1]
    assert cycle.lstrip().startswith("read_names"), "each cycle reads them first"
    before_cycle = ship.split("ship_cycle() {", 1)[0]
    assert "RLID=$(" not in before_cycle.split("read_names() {", 1)[0], "nothing is read at start-up"

