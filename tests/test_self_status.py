"""Which build am I talking to, on which machine, and is it out of room?

The first question asked when something looks wrong, and the toolkit could not answer it. Three
status answers already existed and none of them is this one: ``ops-status`` reads the store for
whether the apps ran, ``host-facts`` reaches a *monitored* host over its cmd_access,
``worker-status`` drives the worker from the master. All three describe something else.

It became urgent on 2026-09-03, when two builds of this toolkit ran the same estate within the hour
— a container built from the private tree and a pip install of the published wheel. They number
differently (``2.87.01`` against ``0.5.0``), so the version alone does not say which is which, and
telling them apart took a shell on the worker.

The tests below are mostly about *not lying*: a memory figure that is really the host's while the
process is capped by a cgroup, a distribution name guessed from the version number, a platform
string that says "Linux" to both Ubuntu and RHEL. An honest "unavailable" beats all three.
"""

from __future__ import annotations

from pathlib import Path

from db_ops.common import self_status


def test_a_container_reports_its_cgroup_limit_not_the_hosts_memory(tmp_path, monkeypatch):
    """64 GiB quoted while the cgroup allows 2 is worse than no number at all: it is the figure
    somebody uses to rule memory out as the cause."""
    limit = tmp_path / "memory.max"
    used = tmp_path / "memory.current"
    limit.write_text("2147483648", encoding="utf-8")     # 2 GiB
    used.write_text("1073741824", encoding="utf-8")      # 1 GiB
    monkeypatch.setattr(self_status, "_CGROUP_V2", (str(limit), str(used)))

    facts = self_status.memory()

    assert facts["source"] == "cgroup"
    assert facts["total_bytes"] == 2 * 1024 ** 3
    assert facts["available_bytes"] == 1 * 1024 ** 3


def test_an_unlimited_cgroup_falls_through_instead_of_reporting_a_nonsense_ceiling(
    tmp_path, monkeypatch
):
    """A container given no limit reports `max` in v2 and a number near 2**63 in v1. Both mean "the
    host's memory", and printing 8 EiB as the ceiling is not a report."""
    limit = tmp_path / "memory.limit_in_bytes"
    used = tmp_path / "memory.usage_in_bytes"
    limit.write_text(str(2 ** 63 - 1), encoding="utf-8")
    used.write_text("1073741824", encoding="utf-8")
    monkeypatch.setattr(self_status, "_CGROUP_V2", ("/nonexistent", "/nonexistent"))
    monkeypatch.setattr(self_status, "_CGROUP_V1", (str(limit), str(used)))

    facts = self_status.memory()

    assert facts["source"] != "cgroup"


def test_every_memory_figure_says_where_it_came_from():
    """Whether the number means anything inside a container depends entirely on its source, so the
    source travels with it rather than being inferred by the reader."""
    assert "source" in self_status.memory()


def test_the_product_is_named_because_the_version_number_cannot_say_it():
    """`2.87.01` and `0.5.0` are the same toolkit under two distribution names. A reader who is
    handed only a version has to already know the numbering scheme to decode it."""
    facts = self_status.distribution()

    assert facts["product"], "the report must name the product, not only a version"
    assert facts["source"], "and say whether it is installed or run from a tree"


def test_a_tree_that_is_not_installed_is_an_answer_rather_than_a_failure(monkeypatch):
    """A source checkout and a container running from a path have no distribution metadata. That is
    a fact about the install, and reporting it beats raising."""
    def missing(_name):
        raise ModuleNotFoundError("no distribution")

    monkeypatch.setattr("importlib.metadata.distribution", missing)

    facts = self_status.distribution()

    assert facts["name"] is None
    assert "source tree" in facts["product"]


def test_the_render_leads_with_the_product_then_the_version():
    """In that order on purpose: the version is only readable once the numbering scheme is known."""
    facts = self_status.collect(tool_root=Path("."), version="2.87.01", public_version="0.5.0",
                                store="postgresql postgres@192.0.2.10:5433/db_ops")
    text = self_status.render(facts)

    lines = text.splitlines()
    assert lines[1].startswith("product   :")
    assert lines[2].startswith("version   :")
    assert "2.87.01" in lines[2] and "0.5.0" in lines[2]
    assert "postgresql postgres@192.0.2.10:5433/db_ops" in text


def test_the_report_says_docker_or_the_operating_system_and_which_one():
    """"Linux" answers Ubuntu and RHEL identically, which does not help anyone deciding whether a
    package name applies; and "in a container" changes what every other figure means."""
    facts = self_status.collect(tool_root=Path("."), version="2.87.01")
    text = self_status.render(facts)

    running = next(line for line in text.splitlines() if line.startswith("running   :"))
    assert ("in Docker" in running) or ("on the OS directly" in running) or (" in " in running)
    assert self_status.runtime() in {"docker", "kubernetes", "containerd", "lxc", "host"}
    assert self_status.operating_system(), "the OS line is never blank"


def test_load_average_is_absent_rather_than_faked_where_the_platform_has_none():
    """`os.getloadavg` does not exist on Windows. A zero would read as an idle machine."""
    facts = self_status.cpu()

    assert "cores" in facts
    assert facts["load_avg"] is None or len(facts["load_avg"]) == 3


def test_the_report_never_carries_a_store_password():
    """The store line names user, host and database so somebody can tell two estates apart. The
    password is in the secret store and has no business in a chat message."""
    facts = self_status.collect(tool_root=Path("."), version="1.0",
                                store="postgresql postgres@192.0.2.10:5433/db_ops")

    assert "password" not in self_status.render(facts).lower()


# --------------------------------------------------------------------------------------------- #
# How long it has been up
# --------------------------------------------------------------------------------------------- #
def test_uptime_is_reported_in_hours_and_utc(monkeypatch):
    """Two figures, and the second is what makes the first comparable.

    Hours answer "has this been up long enough to trust what it says"; the instant answers "up
    since when", which is the one that lines up against an incident, a deploy or another node.
    Written in UTC for the same reason every other timestamp in this toolkit is: an estate spans
    machines and a local-time answer cannot be compared with anything.
    """
    from datetime import datetime, timezone

    from db_ops.common import self_status

    monkeypatch.setattr(self_status, "_uptime_seconds_from_proc", lambda: 15_412.5)
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)

    facts = self_status.uptime(now=now)

    assert facts["hours"] == 4.28, "4h 16m 52.5s is 4.28 h to two places"
    assert facts["since"] == "2026-09-05T07:43:07Z"
    assert facts["source"] == "/proc/uptime"


def test_the_rendered_line_says_it_is_the_host_and_not_the_daemon(monkeypatch):
    """On a node whose daemon was restarted an hour ago the two are nothing like each other, and
    a bare "uptime" would be read as the one the reader came for."""
    from db_ops.common import self_status

    monkeypatch.setattr(self_status, "_uptime_seconds_from_proc", lambda: 3600.0)

    text = self_status.render({"uptime": self_status.uptime()})

    assert "uptime    : 1.00 h  (host up since " in text


def test_two_decimal_places_even_when_the_number_is_round(monkeypatch):
    """`xxxx.xx h` is the shape asked for; `1.0 h` and `1 h` are the same number differently
    formatted, and a column of them does not line up."""
    from db_ops.common import self_status

    monkeypatch.setattr(self_status, "_uptime_seconds_from_proc", lambda: 7200.0)

    assert "uptime    : 2.00 h" in self_status.render({"uptime": self_status.uptime()})


def test_a_platform_that_cannot_answer_says_so_rather_than_guessing(monkeypatch):
    """The rule this whole module follows: unavailable is a fact, zero is a lie."""
    from db_ops.common import self_status

    monkeypatch.setattr(self_status, "_uptime_seconds_from_proc", lambda: None)
    monkeypatch.setattr(self_status, "_uptime_seconds_from_windows", lambda: None)

    facts = self_status.uptime()

    assert facts == {"seconds": None, "hours": None, "since": None, "source": "unavailable"}
    assert "uptime    : unavailable" in self_status.render({"uptime": facts})


def test_windows_is_read_only_when_proc_has_nothing_to_say(monkeypatch):
    """Both readers exist and the order matters: /proc/uptime is the one that works in a
    container, and GetTickCount64 does not exist to be tried there."""
    from db_ops.common import self_status

    called = []
    monkeypatch.setattr(self_status, "_uptime_seconds_from_proc", lambda: 60.0)
    monkeypatch.setattr(self_status, "_uptime_seconds_from_windows",
                        lambda: called.append("windows") or 999.0)

    facts = self_status.uptime()

    assert called == [], "the Windows reader ran on a machine that had already answered"
    assert facts["source"] == "/proc/uptime"


def test_uptime_travels_in_collect_so_a_program_gets_it_too(tmp_path):
    """`format: txt` is the chat line; the envelope carries the same facts for anything else."""
    from db_ops.common import self_status

    facts = self_status.collect(tool_root=tmp_path, version="0.8.2")

    assert "uptime" in facts
    assert set(facts["uptime"]) == {"seconds", "hours", "since", "source"}


def test_self_status_answers_from_a_directory_with_no_config(tmp_path, monkeypatch, capsys):
    """The property this command exists for, and the one the uptime work broke.

    `self-status` must answer when the store is unreachable and when there is no config at all —
    that is exactly when somebody asks what version is running. Reading the runtime directory off
    a `config` that is only bound inside the try block raised UnboundLocalError there instead.

    It passed in the private tree for the worst reason: that tree has a `config.json` at its root,
    so the failing path was never taken. Only the public suite saw it. This runs from a directory
    that has none.
    """
    import db_ops.config as db_ops_config
    from db_ops.common import cli as common_cli

    # The config *load* has to fail, which is the condition being described. Pointing the command
    # at a missing path is not enough - `resolve_config_path` falls back to the tool root's
    # config.json, which exists in this tree and is why the first version of this test passed
    # against the broken code as happily as against the fixed one.
    def refuse(*_a, **_kw):
        raise FileNotFoundError("no config on this machine")

    monkeypatch.setattr(db_ops_config, "load_config", refuse)

    code = common_cli.main(["self-status", '{"format": "txt"}'])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "DBA Brain / db_ops - current state" in out
    assert "db_ops up :" in out, "the line must still be there, saying what it can"


def test_the_store_line_names_the_schema_so_two_nodes_in_one_database_differ(monkeypatch, capsys):
    """On PostgreSQL the database is routinely shared and the schema is what tells stores apart.

    This estate keeps the production store and every soak node inside one `db_ops` database,
    separated only by schema. The line stopped at the database name, so on 2026-09-15 an operator
    reading `postgresql postgres@...:5433/db_ops` over Telegram could not tell which of the three
    schemas in that database the node was writing to — and only one of them was live.
    """
    import db_ops.config as db_ops_config
    from db_ops.common import cli as common_cli

    class _Postgres:
        username, host, port, database, schema = "postgres", "192.0.2.10", 5433, "db_ops", "node7"

    class _Store:
        backend, postgresql, sqlite = "postgresql", _Postgres(), None

    class _Config:
        store, runtime_dir, data_dir = _Store(), None, None

    monkeypatch.setattr(db_ops_config, "load_config", lambda *_a, **_kw: _Config())

    assert common_cli.main(["self-status", '{"format": "txt"}']) == 0

    line = next(row for row in capsys.readouterr().out.splitlines() if row.startswith("store"))
    assert "/db_ops" in line and "schema=node7" in line
    assert "password" not in line.lower()


def test_a_sqlite_store_line_gains_no_empty_schema(monkeypatch, capsys):
    """SQLite has no schema, and `schema=` with nothing after it reads as a missing value."""
    import db_ops.config as db_ops_config
    from db_ops.common import cli as common_cli

    class _Sqlite:
        path = "runtime/dbabrain.sqlite"

    class _Store:
        backend, postgresql, sqlite = "sqlite", None, _Sqlite()

    class _Config:
        store, runtime_dir, data_dir = _Store(), None, None

    monkeypatch.setattr(db_ops_config, "load_config", lambda *_a, **_kw: _Config())

    assert common_cli.main(["self-status", '{"format": "txt"}']) == 0

    line = next(row for row in capsys.readouterr().out.splitlines() if row.startswith("store"))
    assert "schema" not in line


def test_the_node_role_reported_is_the_running_daemon_s(tmp_path):
    """`DB_OPS_NODE_ROLE` is an environment variable, and self-status runs in its own environment
    — a shell where nobody exported it, or a worker the daemon spawned. Reading its own env made
    the node whose daemon came up as `worker` answer `master (default)`, on the single line anyone
    reads to find out. The daemon records the role it started with; when one is running, that is
    the answer.
    """
    import os

    from db_ops.common import self_status
    from db_ops.lib import daemon_state

    daemon_state.record_start(tmp_path, version="0.17.0", node_role="worker")
    os.environ.pop("DB_OPS_NODE_ROLE", None)

    facts = self_status.collect(tool_root=tmp_path, version="0.17.0", runtime_dir=tmp_path)

    assert facts["node_role"] == "worker"


def test_with_no_daemon_running_this_process_s_environment_is_all_there_is(tmp_path, monkeypatch):
    from db_ops.common import self_status

    monkeypatch.setenv("DB_OPS_NODE_ROLE", "master")

    facts = self_status.collect(tool_root=tmp_path, version="0.17.0", runtime_dir=tmp_path)

    assert facts["node_role"] == "master"


def test_an_explicit_role_still_wins(tmp_path):
    """The caller passing one is a deliberate statement and outranks both."""
    from db_ops.common import self_status
    from db_ops.lib import daemon_state

    daemon_state.record_start(tmp_path, version="0.17.0", node_role="worker")

    facts = self_status.collect(tool_root=tmp_path, version="0.17.0", runtime_dir=tmp_path,
                                node_role="master")

    assert facts["node_role"] == "master"
