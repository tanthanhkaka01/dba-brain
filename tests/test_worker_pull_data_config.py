from __future__ import annotations

import stat as stat_mod

import pytest

from db_ops.control import worker_data


class _FakeStat:
    def __init__(self, size=10):
        self.st_size = size
        self.st_mode = stat_mod.S_IFREG | 0o644


class _FakeSFTP:
    """Minimal in-memory SFTP: files live under a worker dir and get() copies
    their bytes to the local path."""

    def __init__(self, worker_files):
        self.worker_files = dict(worker_files)  # name -> content
        self.got = []

    def listdir(self, remote_dir):
        return list(self.worker_files)

    def stat(self, remote_path):
        name = remote_path.rsplit("/", 1)[-1]
        if name not in self.worker_files:
            raise IOError("missing")
        return _FakeStat(len(self.worker_files[name]))

    def get(self, remote_path, local_path):
        name = remote_path.rsplit("/", 1)[-1]
        with open(local_path, "w", encoding="utf-8") as fh:
            fh.write(self.worker_files[name])
        self.got.append(name)

    def close(self):
        pass


class _FakeClient:
    def __init__(self, sftp):
        self._sftp = sftp

    def open_sftp(self):
        return self._sftp

    def close(self):
        pass


@pytest.fixture
def patched(monkeypatch):
    sftp = _FakeSFTP({
        "docker_db_connections.json": '{"docker_db_connections": []}',
        "sql_targets.json": "{}",
        "encrypted_secret_text.json": "SECRET",
    })
    monkeypatch.setattr(worker_data, "ssh_connect", lambda *a, **k: _FakeClient(sftp))
    return sftp


def _pull(tmp_path, **over):
    kwargs = dict(host="h", user="u", password="p", to_master_path=str(tmp_path), overwrite=True)
    kwargs.update(over)
    return worker_data.pull_data_config(**kwargs)


def test_default_pulls_only_registry(patched, tmp_path):
    rc = _pull(tmp_path)
    assert rc == 0
    assert patched.got == ["docker_db_connections.json"]
    assert (tmp_path / "docker_db_connections.json").exists()


def test_all_json_excludes_secret_store(patched, tmp_path):
    _pull(tmp_path, all_json=True)
    assert "encrypted_secret_text.json" not in patched.got
    assert set(patched.got) == {"docker_db_connections.json", "sql_targets.json"}


def test_include_secrets_pulls_secret_store(patched, tmp_path):
    _pull(tmp_path, all_json=True, include_secrets=True)
    assert "encrypted_secret_text.json" in patched.got


def test_merge_secrets_synchronizes_the_configured_plaintext_source(
    patched, monkeypatch, tmp_path,
):
    plaintext_path = tmp_path / "secrets" / "secret_text.json"
    received = {}

    def fake_merge(_sftp, **kwargs):
        received.update(kwargs)
        return "MERGED"

    monkeypatch.setattr(worker_data, "_merge_secret_store", fake_merge)

    _pull(
        tmp_path,
        all_json=True,
        include_secrets=True,
        merge_secrets=True,
        secret_key="key",
        plaintext_secret_path=plaintext_path,
    )

    assert received["plaintext"] == plaintext_path


def test_dry_run_copies_nothing(patched, tmp_path):
    _pull(tmp_path, dry_run=True)
    assert patched.got == []
    assert not (tmp_path / "docker_db_connections.json").exists()


def test_overwrite_false_skips_existing(patched, tmp_path):
    existing = tmp_path / "docker_db_connections.json"
    existing.write_text("KEEP", encoding="utf-8")
    _pull(tmp_path, overwrite=False)
    assert patched.got == []            # not overwritten
    assert existing.read_text(encoding="utf-8") == "KEEP"


def test_specific_files_selection(patched, tmp_path):
    _pull(tmp_path, files=["sql_targets.json"])
    assert patched.got == ["sql_targets.json"]


def test_a_worker_file_the_manifest_does_not_know_is_never_pulled(monkeypatch, tmp_path, capsys):
    """The 2026-08-25 defect, as a test.

    This sweep used to be `sorted(sftp.listdir(...))`, so it took whatever the worker had. Without
    `--overwrite` it skips every file the master already holds, which leaves exactly one possible
    effect: creating files the master does not have. That is the set somebody has just deleted —
    and `metric_groups.json` and `notify_levels.json`, removed on 2026-08-21 because nothing read
    them, were back four days later.

    The manifest decides now, so the worker's copy of a file nobody listed cannot come home.
    """
    sftp = _FakeSFTP({
        "sql_targets.json": "{}",
        "metric_groups.json": '{"metric_groups": []}',
        "notify_levels.json": '{"notify_levels": []}',
    })
    monkeypatch.setattr(worker_data, "ssh_connect", lambda *a, **k: _FakeClient(sftp))

    _pull(tmp_path, all_json=True)

    assert sftp.got == ["sql_targets.json"]
    assert not (tmp_path / "metric_groups.json").exists()
    assert not (tmp_path / "notify_levels.json").exists()


def test_an_unknown_worker_file_is_named_rather_than_ignored_silently(monkeypatch, tmp_path,
                                                                     capsys):
    """Refusing to take it is right; saying nothing about it is not.

    A file on the worker that the master has never heard of is either something new that belongs
    in the manifest or a leftover the master already removed, and only a person can tell those
    apart. Silence would leave a genuinely new file stranded on one host with nothing to notice.
    """
    sftp = _FakeSFTP({"sql_targets.json": "{}", "something_new.json": "{}"})
    monkeypatch.setattr(worker_data, "ssh_connect", lambda *a, **k: _FakeClient(sftp))

    _pull(tmp_path, all_json=True)

    printed = capsys.readouterr().out
    assert "not in the data-file manifest" in printed
    assert "something_new.json" in printed


def test_a_master_owned_file_is_not_swept_back(monkeypatch, tmp_path):
    """`push` means the master decides, so a sweep must not overwrite it from the worker.

    `metric_definitions.json` is the tool's own catalogue: edited on the master, shipped down. A
    sweep that took the worker's copy would silently revert whatever was last changed here.
    """
    sftp = _FakeSFTP({"metric_definitions.json": '{"metrics": []}', "sql_commands.json": "{}"})
    monkeypatch.setattr(worker_data, "ssh_connect", lambda *a, **k: _FakeClient(sftp))

    _pull(tmp_path, all_json=True)

    assert sftp.got == ["sql_commands.json"]
