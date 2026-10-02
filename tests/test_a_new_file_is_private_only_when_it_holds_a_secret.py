"""A file the tool creates is private only when it holds a secret.

The atomic writer makes its replacement with ``mkstemp``, which creates 0600. For a file that already
exists it then restores that file's own mode; for a **new** one nothing did, so every new file was
0600 whatever it held. That was harmless while the writer's callers only ever rewrote files, and the
secret stores relied on it. Then the scaffold, the lab registry and the daemon's state file moved
onto the writer to become atomic (review 0.25.0, B9.2), and every file ``init`` creates turned from
0644 into 0600 with them: a node whose daemon runs as another user than the one who ran ``init`` -
an operator's ``sudo dbabrain init``, then a service account - could not read its own configuration.

So privacy is stated by the caller that knows what the file holds (found reading the batch back,
2026-10-02):

* a secret store, the plaintext secrets source - ``private``, 0600 when new;
* everything else - the mode a plain write would have given it, by the umask.

An existing file keeps its mode and owner either way; that rule is older and is not this one.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from db_ops.common import scaffold
from db_ops.lib import docker_db_registry, json_io, secret_text

posix_modes = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture()
def umask_022():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@posix_modes
def test_a_new_file_gets_the_mode_a_plain_write_would_give_it(tmp_path, umask_022):
    path = tmp_path / "db_instances.json"

    json_io.atomic_write_text(path, "{}\n")

    assert _mode(path) == 0o644


@posix_modes
def test_a_hardened_host_s_umask_is_kept(tmp_path):
    """A host that says nobody else reads new files is not overruled by a fixed 0644."""
    previous = os.umask(0o077)
    try:
        path = tmp_path / "db_instances.json"
        json_io.atomic_write_text(path, "{}\n")
    finally:
        os.umask(previous)

    assert _mode(path) == 0o600


@posix_modes
def test_a_new_private_file_is_readable_by_its_owner_alone(tmp_path, umask_022):
    path = tmp_path / "secret_text.json"

    json_io.atomic_write_text(path, "{}\n", private=True)

    assert _mode(path) == 0o600


@posix_modes
def test_an_existing_file_keeps_its_mode_whatever_is_asked(tmp_path, umask_022):
    path = tmp_path / "db_instances.json"
    path.write_text("{}\n", encoding="utf-8")
    os.chmod(path, 0o640)

    json_io.atomic_write_text(path, '{"a": 1}\n')
    assert _mode(path) == 0o640
    json_io.atomic_write_text(path, '{"a": 2}\n', private=True)
    assert _mode(path) == 0o640


@posix_modes
def test_the_umask_is_read_without_being_changed(umask_022):
    """``os.umask`` reads by setting: between its two calls another thread's new file is 0666."""
    assert json_io._umask() == 0o022
    assert os.umask(0o022) == 0o022, "still what it was"


def test_the_umask_falls_back_to_the_ordinary_one_where_it_cannot_be_read(monkeypatch):
    def unreadable(*_args, **_kwargs):
        raise OSError("no /proc here")

    monkeypatch.setattr("builtins.open", unreadable)

    assert json_io._umask() == 0o022


# --------------------------------------------------------------------------- #
# Who says private
# --------------------------------------------------------------------------- #
def _writes(monkeypatch, module) -> list[tuple[str, bool]]:
    """Every atomic write ``module`` makes, as ``(file name, private)`` - checked on any platform."""
    seen: list[tuple[str, bool]] = []
    real = json_io.atomic_write_text

    def record(path, text, *, private: bool = False):
        seen.append((Path(path).name, private))
        real(Path(path), text, private=private)

    monkeypatch.setattr(module, "atomic_write_text", record)
    return seen


def test_init_creates_its_configuration_readable_and_its_secrets_source_private(tmp_path, monkeypatch):
    written = _writes(monkeypatch, scaffold)

    scaffold.initialise(tmp_path)

    seen = dict(written)
    assert seen.pop("secret_text.json") is True, "the plaintext source of every credential"
    assert seen and not any(seen.values()), f"configuration the daemon reads: {seen}"
    assert "config.json" in seen and "db_instances.json" in seen


def test_the_secret_stores_say_private(tmp_path, monkeypatch):
    seen = _writes(monkeypatch, json_io)

    secret_text.set_secret_text(tmp_path, "A", "x", key="test-passphrase")

    assert seen == [(secret_text.ENCRYPTED_SECRET_TEXT_FILENAME, True)]


def test_the_lab_registry_does_not(tmp_path, monkeypatch):
    seen = _writes(monkeypatch, docker_db_registry)

    docker_db_registry.save_registry(tmp_path / "docker_db_connections.json", {"docker_db_connections": []})

    assert seen == [("docker_db_connections.json", False)]


@posix_modes
def test_a_node_s_configuration_can_be_read_by_another_user_than_the_one_who_ran_init(tmp_path, umask_022):
    """The run this is for: ``init`` as one user, the daemon as another."""
    scaffold.initialise(tmp_path)

    assert _mode(tmp_path / "config.json") == 0o644
    assert _mode(tmp_path / "data" / "db_instances.json") == 0o644
    assert _mode(tmp_path / "secrets" / "secret_text.json") == 0o600
