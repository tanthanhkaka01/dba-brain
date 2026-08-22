"""Moving a lab instance must move its *data*, and must never leave the source worse off.

``move-db-docker`` exists because re-provisioning is not moving. ``ora11g_lab`` holds a restored
11g R2 estate; an operator asking to move it onto another host and getting an empty database with
the same name has lost the thing they were moving, silently, and would not find out until the
first query.

Almost everything here is an ordering rule, because every one of them is a way to lose data or to
lose an evening:

* the containers stop before a volume is read and start again afterwards — a datafile copied
  while Oracle is writing to it restores to a database that opens and is wrong;
* they start again even when the packing raised, because a lab left down by a failed ``tar`` is a
  second outage caused by the tool that was supposed to prevent the first;
* the destination's guards (docker present, port free, subnet free) all run before a single byte
  is packed — each of them otherwise fails *after* the transfer, and the whole bundle has to be
  moved again;
* ``docker compose create`` runs before the volumes are filled, so the volumes carry compose's
  own labels. Filling them first and letting compose meet them afterwards ends in "volume already
  exists but was not created by Docker Compose" with the data already inside;
* the source is only stopped once the destination is healthy, and it is stopped, never removed.

None of these need a host: the mover talks to one small runner/fs interface, so a fake answers
every docker command and records the order it was asked in.
"""

from __future__ import annotations

import subprocess

import pytest

from db_ops.sre.docker_db import mover, register_config


class FakeHost:
    """One host, as the mover sees it: a command runner plus four filesystem calls.

    Commands are matched by a substring of the joined argv, longest match first, so a test only
    has to state the answers it cares about — everything else succeeds silently, which is what a
    real ``mkdir -p`` or ``rm -rf`` does.
    """

    def __init__(self, host="10.0.0.1", answers=None, failures=None, paths=()):
        self.host = host
        self.answers = dict(answers or {})
        self.failures = dict(failures or {})
        self.paths = set(paths)
        self.commands: list[str] = []
        self.written: dict[str, str] = {}

    def run(self, argv, *, cwd=None, capture_output=False, **_ignored):
        shown = " ".join(str(part) for part in argv)
        self.commands.append(shown)
        for needle in sorted(self.failures, key=len, reverse=True):
            if needle in shown:
                return subprocess.CompletedProcess(argv, self.failures[needle], "", "boom")
        for needle in sorted(self.answers, key=len, reverse=True):
            if needle in shown:
                return subprocess.CompletedProcess(argv, 0, self.answers[needle], "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    # -- fs face -------------------------------------------------------- #
    def exists(self, path):
        return str(path) in self.paths

    def write_text(self, path, content, *, mode=None):
        self.written[str(path)] = content

    def close(self):
        pass

    # -- helpers for the assertions ------------------------------------- #
    def index_of(self, needle: str) -> int:
        for index, command in enumerate(self.commands):
            if needle in command:
                return index
        raise AssertionError(f"{needle!r} was never run. Commands: {self.commands}")

    def ran(self, needle: str) -> bool:
        return any(needle in command for command in self.commands)


SOURCE_ANSWERS = {
    "docker ps -a --filter label=com.docker.compose.project=ora11g_lab": "ora11g_lab",
    "docker inspect -f {{.Config.Image}} ora11g_lab": "gvenzl/oracle-xe:11",
    'docker inspect -f {{range .Mounts}}': "ora11g_lab_ora11g_lab_data",
    "docker inspect -f {{range $p, $c := .HostConfig.PortBindings}}": "1521",
    'docker inspect -f {{index .Config.Labels "com.docker.compose.service"}}': "ora11g_lab",
    "docker inspect --size -f {{.SizeRw}}": "15240000000",
    "ls -A /dbops_src | head -1": "",
    "docker network ls --filter": "ora11g_lab_default",
    "docker network inspect": "172.30.241.0/24",
    "id -u; id -g": "1000\n1000",
    "stat -c %s": "images.tar.gz 900\ninstance.tar.gz 40\nvolume_ora11g_lab_ora11g_lab_data.tar.gz 500",
}


def _spec(**overrides):
    values = {
        "name": "ora11g_lab",
        "commit_container": True,
        "source_target": "SRC",
        "dest_target": "DST",
        "engine": "oracle-xe",
        "health_timeout": 1,
    }
    values.update(overrides)
    return mover.MoveSpec(**values)


def _source():
    return FakeHost("192.0.2.249", answers=SOURCE_ANSWERS)


DESTINATION_ANSWERS = {"id -u; id -g": "1001\n1001"}


def _destination(**kwargs):
    answers = dict(DESTINATION_ANSWERS)
    answers.update(kwargs.pop("answers", {}))
    return FakeHost("192.0.2.11", answers=answers, **kwargs)


def _facts(source=None):
    return mover.inspect_instance(source or _source(), "ora11g_lab", "/opt/db_ops/containers")


# --------------------------------------------------------------------------- #
# Reading the source
# --------------------------------------------------------------------------- #
def test_the_instance_is_described_by_docker_not_by_its_compose_file():
    facts = _facts()

    assert facts.containers == ["ora11g_lab"]
    assert facts.images == ["gvenzl/oracle-xe:11"]
    assert facts.volumes == ["ora11g_lab_ora11g_lab_data"]
    assert facts.ports == [1521]
    assert facts.subnets == ["172.30.241.0/24"]


def test_a_project_with_no_containers_is_refused_by_name():
    host = FakeHost(answers={"docker ps -a --filter": ""})

    with pytest.raises(mover.MoveError, match="No containers belong to compose project"):
        mover.inspect_instance(host, "typo_lab", "/opt/db_ops/containers")


def test_ports_are_read_from_the_declaration_so_a_stopped_container_still_answers():
    """HostConfig.PortBindings, not NetworkSettings.Ports: the destination's port check happens
    while the source is stopped for its volume copy, and the second one is empty by then."""
    facts = _facts()

    inspect = [c for c in _source().commands if "PortBindings" in c]
    assert facts.ports == [1521]
    assert not any("NetworkSettings.Ports" in c for c in inspect)


# --------------------------------------------------------------------------- #
# Guards, all of them before the transfer
# --------------------------------------------------------------------------- #
def test_a_taken_port_on_the_destination_is_refused_before_anything_is_packed():
    source = _source()
    destination = _destination(answers={"docker ps --format {{.Ports}}": "0.0.0.0:1521->1521/tcp"})

    with pytest.raises(mover.MoveError, match="already published"):
        mover.move(_spec(), source=source, destination=destination, log=lambda *_: None)

    assert not source.ran("docker save")
    assert not source.ran("docker stop")


def test_an_overlapping_pinned_subnet_is_refused_before_anything_is_packed():
    """`compose up` would fail with "Pool overlaps with other one on this address space" — after
    the volumes have been restored, which costs the whole transfer a second time."""
    source = _source()
    destination = _destination(answers={"docker network inspect -f": "172.30.241.0/24"})

    with pytest.raises(mover.MoveError, match="overlaps"):
        mover.move(_spec(), source=source, destination=destination, log=lambda *_: None)

    assert not source.ran("docker save")


def test_an_instance_of_the_same_name_on_the_destination_is_refused_without_force():
    source = _source()
    destination = _destination(paths=["/opt/db_ops/containers/ora11g_lab"])

    with pytest.raises(mover.MoveError, match="already exists"):
        mover.move(_spec(), source=source, destination=destination, log=lambda *_: None)


def test_a_destination_without_docker_is_refused_by_saying_so():
    source = _source()
    destination = _destination(failures={"docker compose version": 127})

    with pytest.raises(mover.MoveError, match="docker compose is not usable"):
        mover.move(_spec(), source=source, destination=destination, log=lambda *_: None)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def test_the_containers_stop_before_a_volume_is_read_and_start_again_after():
    source = _source()
    facts = _facts(source)
    source.commands.clear()

    mover.export_bundle(source, facts, _spec(), log=lambda *_: None)

    stop = source.index_of("docker stop")
    pack = source.index_of("volume_ora11g_lab_ora11g_lab_data.tar.gz")
    start = source.index_of("docker start")
    assert stop < pack < start


def test_the_stop_gives_the_engine_time_to_shut_itself_down():
    """Docker's default is 10 seconds; a SIGKILLed Oracle leaves datafiles mid-checkpoint, and
    the copy taken straight afterwards carries that state to the new host."""
    source = _source()
    facts = _facts(source)
    source.commands.clear()

    mover.export_bundle(source, facts, _spec(), log=lambda *_: None)

    assert f"docker stop -t {mover.STOP_TIMEOUT_SECONDS}" in source.commands[source.index_of("docker stop")]


def test_the_containers_start_again_even_when_packing_the_volume_fails():
    """A lab left down by a failed tar is a second outage caused by the tool meant to avoid one."""
    source = FakeHost("192.0.2.249", answers=SOURCE_ANSWERS,
                      failures={"tar czf /dbops_out/volume_": 2})
    facts = _facts()

    with pytest.raises(mover.MoveError):
        mover.export_bundle(source, facts, _spec(), log=lambda *_: None)

    assert source.ran("docker start")


def test_no_volumes_ships_no_volume_archive_and_never_stops_the_source():
    source = _source()
    facts = _facts(source)
    source.commands.clear()

    manifest = mover.export_bundle(source, facts, _spec(include_volumes=False,
                                                       commit_container=False),
                                   log=lambda *_: None)

    assert manifest["volumes"] == []
    assert not source.ran("docker stop")


def test_the_plan_warns_that_no_volumes_means_an_empty_database():
    text = mover.format_plan(_facts(), _spec(include_volumes=False),
                             source_host="a", dest_host="b")

    assert "EMPTY database" in text


def test_root_is_borrowed_from_the_instances_own_image_not_from_sudo():
    """The instance dir is root-owned and volume contents belong to the engine's uid. A throwaway
    `--user 0` container reads both, so neither host needs sudo rights for the SSH user."""
    source = _source()
    facts = _facts(source)
    source.commands.clear()

    mover.export_bundle(source, facts, _spec(), log=lambda *_: None)

    root_runs = [c for c in source.commands if "docker run --rm --user 0" in c]
    assert root_runs, source.commands
    assert all("gvenzl/oracle-xe:11" in c for c in root_runs)
    assert not any("sudo" in c for c in source.commands)


def test_the_bundle_is_handed_back_to_the_ssh_user_that_has_to_stream_it():
    source = _source()
    facts = _facts(source)
    source.commands.clear()

    mover.export_bundle(source, facts, _spec(), log=lambda *_: None)

    assert source.ran("chown -R 1000:1000 /dbops_out")


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def _manifest():
    return {
        "images_archive": mover.IMAGES_ARCHIVE,
        "instance_archive": mover.INSTANCE_ARCHIVE,
        "volumes": [{"volume": "ora11g_lab_ora11g_lab_data",
                     "archive": "volume_ora11g_lab_ora11g_lab_data.tar.gz"}],
        "sizes": {},
    }


def test_compose_creates_the_volumes_before_they_are_filled():
    """Compose refuses a volume it did not label — and refuses it *after* the data is in it."""
    destination = _destination()

    mover.import_bundle(destination, _facts(), _spec(), _manifest(), log=lambda *_: None)

    load = destination.index_of("docker load")
    unpack = destination.index_of("tar xzf /dbops_out/instance.tar.gz")
    create = destination.index_of("docker compose create")
    restore = destination.index_of("tar xzf /dbops_out/volume_")
    up = destination.index_of("docker compose up -d")
    assert load < unpack < create < restore < up


def test_the_moved_instance_directory_belongs_to_the_user_that_must_run_compose():
    """`.env` is 0600; left owned by root, every compose verb fails with permission denied for
    the SSH user — including the `compose create` two lines later."""
    destination = _destination()

    mover.import_bundle(destination, _facts(), _spec(), _manifest(), log=lambda *_: None)

    unpack = destination.commands[destination.index_of("tar xzf /dbops_out/instance.tar.gz")]
    assert "chown -R 1001:1001" in unpack


def test_a_restored_volume_is_emptied_first_so_the_copy_replaces_rather_than_merges():
    destination = _destination()

    mover.import_bundle(destination, _facts(), _spec(), _manifest(), log=lambda *_: None)

    restore = destination.commands[destination.index_of("tar xzf /dbops_out/volume_")]
    assert "rm -rf" in restore


# --------------------------------------------------------------------------- #
# The source's fate
# --------------------------------------------------------------------------- #
def _move(spec, source, destination, monkeypatch, *, healthy=True):
    monkeypatch.setattr(mover, "transfer_bundle", lambda *a, **k: [])
    monkeypatch.setattr(
        mover.healthcheck, "wait_healthy",
        lambda services, engine, **kwargs: {svc: ("healthy" if healthy else "timeout")
                                            for svc in services})
    monkeypatch.setattr(mover.healthcheck, "failure_detail", lambda *a, **k: "")
    return mover.move(spec, source=source, destination=destination, log=lambda *_: None)


def test_the_source_is_only_stopped_once_the_destination_is_healthy(monkeypatch):
    source, destination = _source(), _destination()

    result = _move(_spec(stop_source=True), source, destination, monkeypatch)

    # Two stops: the one that quiesced the copy, and the final one. What matters is that the
    # final one comes after the source was started again — i.e. after the destination proved
    # itself, not as a side effect of the export.
    assert result["source_stopped"] is True
    after_restart = source.commands[source.index_of("docker start") + 1:]
    assert any(command.startswith("docker stop") for command in after_restart)


def test_an_unhealthy_destination_leaves_the_source_running(monkeypatch):
    source, destination = _source(), _destination()

    with pytest.raises(mover.MoveError, match="did not become healthy"):
        _move(_spec(stop_source=True), source, destination, monkeypatch, healthy=False)

    # The export's own stop/start pair is fine; what must not have happened is a stop after it.
    assert source.commands[source.index_of("docker start"):] == source.commands[
        source.index_of("docker start"):][:1] or not any(
        c.startswith("docker stop") for c in source.commands[source.index_of("docker start"):])


def test_the_source_is_stopped_and_never_removed(monkeypatch):
    """Its containers and volumes are the only other copy of the data until a person has looked
    at the new host."""
    source, destination = _source(), _destination()

    _move(_spec(stop_source=True), source, destination, monkeypatch)

    assert not source.ran("docker rm")
    assert not source.ran("compose down")


def test_without_stop_source_the_move_is_a_clone_and_the_source_keeps_running(monkeypatch):
    source, destination = _source(), _destination()

    result = _move(_spec(), source, destination, monkeypatch)

    assert result["source_stopped"] is False
    assert source.ran("docker start")


# --------------------------------------------------------------------------- #
# What the move records afterwards
# --------------------------------------------------------------------------- #
def test_the_engine_comes_from_the_registry_so_it_need_not_be_retyped(tmp_path):
    registry = tmp_path / register_config.REGISTRY_FILENAME
    register_config.save_registry(registry, {register_config.REGISTRY_ROOT_KEY: [
        {"id": "ORA11G_LAB", "engine": "oracle-xe", "host": "", "port": 1521,
         "docker": {"compose_path": "/old/path/docker-compose.yml"}},
    ]})

    assert mover.resolve_engine(_spec(engine=""), data_dir=tmp_path) == "oracle-xe"


def test_an_unregistered_instance_asks_for_the_engine_rather_than_guessing(tmp_path):
    with pytest.raises(mover.MoveError, match="--engine"):
        mover.resolve_engine(_spec(engine=""), data_dir=tmp_path)


def test_relocating_a_connection_changes_where_it_is_and_nothing_else(tmp_path):
    """A move changes three facts. Rebuilding the entry from a spec would reset the fields an
    operator has edited since the instance was provisioned."""
    registry = tmp_path / register_config.REGISTRY_FILENAME
    register_config.save_registry(registry, {register_config.REGISTRY_ROOT_KEY: [
        {"id": "ORA11G_LAB", "engine": "oracle-xe", "host": "192.0.2.249", "port": 1521,
         "username": "system", "password_env": "ORA11G_LAB_PASSWORD",
         "docker": {"instance_name": "ora11g_lab", "version": "11",
                    "compose_path": "/opt/db_ops/containers/ora11g_lab/docker-compose.yml"}},
    ]})

    action = register_config.relocate_connection(
        registry, "ORA11G_LAB", host="192.0.2.11", worker_host="192.0.2.11",
        compose_path="/opt/db_ops/containers/ora11g_lab/docker-compose.yml")

    entry = register_config.load_registry(registry)[register_config.REGISTRY_ROOT_KEY][0]
    assert action == "updated"
    assert entry["host"] == entry["worker_host"] == "192.0.2.11"
    assert entry["port"] == 1521
    assert entry["password_env"] == "ORA11G_LAB_PASSWORD"
    assert entry["docker"]["version"] == "11"


def test_an_instance_that_predates_the_registry_is_not_an_error(tmp_path):
    registry = tmp_path / register_config.REGISTRY_FILENAME
    register_config.save_registry(registry, {register_config.REGISTRY_ROOT_KEY: []})

    assert register_config.relocate_connection(
        registry, "NOT_THERE", host="h", worker_host="h", compose_path="c") == "not_registered"


# --------------------------------------------------------------------------- #
# The empty-database guard, and moving a container whose data is inside it
# --------------------------------------------------------------------------- #
def test_a_database_that_lives_in_the_container_is_refused_rather_than_shipped_empty():
    """The failure this exists for: the destination comes up healthy, with the right name, the
    right port and the right password — and an empty database. Nothing about it looks wrong."""
    source = _source()
    facts = _facts(source)

    with pytest.raises(mover.MoveError, match="--commit-container"):
        mover.assert_data_travels(source, facts, _spec(commit_container=False))


def test_a_volume_that_holds_something_is_taken_at_its_word():
    source = FakeHost("192.0.2.249", answers={**SOURCE_ANSWERS,
                                                 "ls -A /dbops_src | head -1": "system.dbf"})
    facts = _facts(source)

    mover.assert_data_travels(source, facts, _spec(commit_container=False))


def test_an_idle_container_with_a_small_layer_needs_no_commit():
    source = FakeHost("192.0.2.249", answers={**SOURCE_ANSWERS,
                                                 "docker inspect --size -f {{.SizeRw}}": "4096"})
    facts = _facts(source)

    mover.assert_data_travels(source, facts, _spec(commit_container=False))


def test_no_volumes_says_empty_is_wanted_so_the_guard_stands_aside():
    source = _source()
    facts = _facts(source)

    mover.assert_data_travels(source, facts, _spec(commit_container=False, include_volumes=False))


def test_the_container_is_committed_while_it_is_stopped():
    """An image made from a running Oracle carries datafiles mid-write — a database that opens
    and is wrong, which is the same reason the volume copy waits for the stop."""
    source = _source()
    facts = _facts(source)
    source.commands.clear()

    manifest = mover.export_bundle(source, facts, _spec(commit_container=True), log=lambda *_: None)

    assert source.index_of("docker stop") < source.index_of("docker commit")
    assert manifest["image_overrides"] == {"ora11g_lab": manifest["images"][0]}
    assert manifest["images"][0].startswith("db_ops/ora11g_lab-ora11g_lab:moved-")


def test_the_committed_image_is_the_one_saved_not_the_stock_one():
    source = _source()
    facts = _facts(source)
    source.commands.clear()

    manifest = mover.export_bundle(source, facts, _spec(commit_container=True), log=lambda *_: None)

    save = source.commands[source.index_of("docker save")]
    assert manifest["images"][0] in save
    assert "gvenzl/oracle-xe:11" not in save


def test_the_moved_instance_is_pinned_to_the_committed_image_by_an_override_file():
    """An override rather than an edit of docker-compose.yml: the file that travelled stays the
    file the operator wrote, and compose loads the override with no extra arguments."""
    destination = _destination()
    manifest = {**_manifest(), "images": ["db_ops/ora11g_lab-ora11g_lab:moved-1"],
                "image_overrides": {"ora11g_lab": "db_ops/ora11g_lab-ora11g_lab:moved-1"}}

    mover.import_bundle(destination, _facts(), _spec(), manifest, log=lambda *_: None)

    override = destination.written["/opt/db_ops/containers/ora11g_lab/docker-compose.override.yml"]
    assert "image: db_ops/ora11g_lab-ora11g_lab:moved-1" in override
    assert "services:" in override


def test_force_frees_the_ports_before_they_are_checked(monkeypatch):
    """A second attempt at the same move is the likeliest thing holding the ports. Checking
    first made --force unable to replace any instance that publishes its own."""
    source = _source()
    destination = _destination(
        answers={"docker ps --format {{.Ports}}": "0.0.0.0:1521->1521/tcp"},
        paths=["/opt/db_ops/containers/ora11g_lab",
               "/opt/db_ops/containers/ora11g_lab/docker-compose.yml"])
    # The teardown is what frees the port, so the fake stops reporting it afterwards.
    original_run = destination.run

    def run(argv, **kwargs):
        result = original_run(argv, **kwargs)
        if "compose down" in " ".join(str(p) for p in argv):
            destination.answers.pop("docker ps --format {{.Ports}}", None)
        return result

    destination.run = run
    _move(_spec(force=True), source, destination, monkeypatch)

    assert destination.index_of("compose down -v") < destination.index_of("docker ps --format")
