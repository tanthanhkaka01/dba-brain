"""A restore's instance metadata is a field of its own, stated - and read from where the entry says (0.40.0, 1.376).

The operator, 2026-11-15: *restoring metadata must be a field of its own, required, to choose whether to
restore metadata or not; and when yes, from the repository or from a server_id, and from which path - it
can be another folder than the one holding the backup files.* Decided 2026-11-16: ``server_metadata.enabled``
required on every restore entry (PostgreSQL and Oracle only ``false``); when true, ``source`` (``node``,
``server`` with its ``server_id`` and ``path``, or ``repository`` and its ``path``); a backup entry's
``repository_path`` is where its offload carries the bundle, sealed with age.

The bundle holds login password hashes and credentials: it never rests on the repository in the clear,
and a restore that fetched it removes its plain copy when the phase is done.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import instance_bundle_store as B
from dbabrain.backup_restore import server_metadata as SM
from dbabrain.lib.restore import stated as S

pyrage = pytest.importorskip("pyrage")
from pyrage import x25519  # noqa: E402


def _bundle(root: Path) -> Path:
    (root / "server").mkdir(parents=True)
    (root / "manifest.json").write_text('{"artifacts": ["logins"]}', encoding="utf-8")
    (root / "server" / "logins.sql").write_text("CREATE LOGIN app WITH PASSWORD = 0x0200AB HASHED;", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# The field, stated
# --------------------------------------------------------------------------- #
def _restore(**block):
    return {"script": "s", "db_type": "sqlserver", "server_id": "A", "target_server_id": "B",
            "target_backup_dir": "/stage", "server_metadata": block}


def test_an_entry_that_replays_metadata_states_where_it_reads_the_bundle():
    assert "server_metadata.source" in S.unstated(_restore(enabled=True))
    assert "server_metadata.source" not in S.unstated(_restore(enabled=False)), "off states nothing more"


@pytest.mark.parametrize("source, needs", [("server", {"server_metadata.server_id", "server_metadata.path"}),
                                           ("repository", {"server_metadata.path"}), ("node", set())])
def test_each_source_states_what_it_needs_to_be_found(source, needs):
    unstated = set(S.unstated(_restore(enabled=True, source=source)))
    assert needs <= unstated
    assert not ({"server_metadata.server_id", "server_metadata.path"} - needs) & unstated


def test_postgresql_and_oracle_state_it_too():
    for engine in ("postgresql", "oracle"):
        assert "server_metadata.enabled" in S.unstated({"script": "s", "db_type": engine, "server_id": "A"})


def test_an_upgraded_entry_reads_what_it_ran_on():
    """`upgrade-config` writes what each entry had been running on: off where it said nothing, the node's
    own export where it replayed one."""
    assert S.ran_on("server_metadata.enabled", {}, kind=S.SCRIPT_RESTORE) is False
    assert S.ran_on("server_metadata.source", _restore(enabled=True), kind=S.SCRIPT_RESTORE) == "node"


def test_a_backup_entry_that_exports_states_where_its_bundle_goes():
    entry = {"backup_id": "SQL", "db_type": "sqlserver", "server_metadata": {"enabled": True}}
    assert "server_metadata.repository_path" in S.unstated(entry, kind=S.BACKUP_ENTRY)
    assert S.ran_on("server_metadata.repository_path", entry, kind=S.BACKUP_ENTRY) == "", "not carried"


def test_the_plan_reads_the_source_and_refuses_a_typo(monkeypatch):
    monkeypatch.setattr("dbabrain.lib.data_sources.load_sqlserver_instance_policy", lambda: {})
    plan = SM.parse_server_metadata({"enabled": True, "source": "repository", "path": "/repo/A/_instance"},
                                    label="R", for_restore=True)
    assert (plan.source, plan.path, "source" in plan.extras) == ("repository", "/repo/A/_instance", False)
    with pytest.raises(SM.ServerMetadataConfigError, match="node, server, repository"):
        SM.parse_server_metadata({"enabled": True, "source": "nas"}, label="R", for_restore=True)
    backup = SM.parse_server_metadata({"enabled": True, "repository_path": "/repo/A/_instance"}, label="B")
    assert backup.repository_path == "/repo/A/_instance" and "repository_path" not in backup.extras


# --------------------------------------------------------------------------- #
# Packed, sealed, never in the clear on the repository
# --------------------------------------------------------------------------- #
def test_a_bundle_sealed_for_a_key_opens_only_with_its_identity(tmp_path):
    identity = x25519.Identity.generate()
    sealed = B.seal(B.pack(_bundle(tmp_path / "b")), str(identity.to_public()))

    assert b"CREATE LOGIN" not in sealed
    plain = B.open_sealed(sealed, str(identity))
    assert B.unpack(plain, tmp_path / "out") == 2
    assert (tmp_path / "out" / "server" / "logins.sql").read_text(encoding="utf-8").startswith("CREATE LOGIN")
    with pytest.raises(pyrage.DecryptError):
        B.open_sealed(sealed, str(x25519.Identity.generate()))


def test_a_bundle_naming_a_file_outside_its_folder_is_refused(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("../escape.sql")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(B.BundleError, match="outside its folder"):
        B.unpack(buffer.getvalue(), tmp_path / "out")


class _Repository:
    """push-file and pull-file onto a folder standing in for the repository host."""

    def __init__(self, root: Path) -> None:
        self.root, self.pushed = root, []

    def run(self, command, request, **_kw):
        if command == "push-file":
            data = Path(request["local_path"]).read_bytes()
            self.pushed.append((request["remote_path"], data))
            destination = self.root / request["remote_path"].lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            return {"verified": True}
        if command == "pull-file":
            Path(request["local_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(request["local_path"]).write_bytes((self.root / request["remote_path"].lstrip("/")).read_bytes())
            return {"verified": True}
        if command == "list-files":
            folder = self.root / request["folder"].lstrip("/")
            return {"files": [{"path": "/" + str(p.relative_to(self.root)).replace("\\", "/"), "type": "file"}
                              for p in sorted(folder.rglob("*")) if p.is_file()]}
        raise AssertionError(command)


@pytest.fixture
def keys(monkeypatch):
    identity = x25519.Identity.generate()
    store = SimpleNamespace(identity=lambda key_id, passphrase: str(identity))
    monkeypatch.setattr("dbabrain.db.repository_encryption_store.RepositoryEncryptionStore.from_config",
                        classmethod(lambda cls, _c: store))
    monkeypatch.setattr("dbabrain.backup_restore.repository_encryption.key_for",
                        lambda entry, **_k: ({"key_id": 4, "recipient": str(identity.to_public())}, str(identity)))
    return identity


def test_the_backup_carries_only_the_sealed_bundle_and_its_manifest(tmp_path, monkeypatch, keys):
    repo = _Repository(tmp_path / "repo")
    monkeypatch.setattr(B.common_cli, "run", repo.run)

    carried = B.carry_to_repository(SimpleNamespace(server_id="SRV-01"), bundle_dir=_bundle(tmp_path / "b"),
                                    repository_path="/mnt/nas/SRV-01/_instance", repository_host={"host": "repo"},
                                    app_config=None, secrets={}, passphrase="p")

    assert [path for path, _data in repo.pushed] == ["/mnt/nas/SRV-01/_instance/instance_bundle.tar.age",
                                                     "/mnt/nas/SRV-01/_instance/instance_bundle.json"]
    assert all(b"CREATE LOGIN" not in data for _path, data in repo.pushed), "never in the clear on the repository"
    manifest = json.loads(repo.pushed[1][1])
    assert manifest["key_id"] == 4 and carried["key_id"] == 4 and carried["path"] == "/mnt/nas/SRV-01/_instance"


def test_a_restore_from_the_repository_decrypts_on_the_node_and_removes_its_plain_copy(tmp_path, monkeypatch, keys):
    repo = _Repository(tmp_path / "repo")
    monkeypatch.setattr(B.common_cli, "run", repo.run)
    monkeypatch.setattr(B, "_host_of", lambda server_id, **_k: {"host": server_id})
    B.carry_to_repository(SimpleNamespace(server_id="SRV-01"), bundle_dir=_bundle(tmp_path / "b"),
                          repository_path="/mnt/nas/SRV-01/_instance", repository_host={"host": "repo"},
                          app_config=None, secrets={}, passphrase="p")
    plan = SimpleNamespace(source="repository", server_id="", path="/mnt/nas/SRV-01/_instance")

    opened = B.open_for_restore(plan, source_server_id="SRV-01", app_config=None, secrets={}, passphrase="p",
                                repository_server_id="REPO")

    assert (opened.path / "server" / "logins.sql").is_file() and "decrypted" in opened.said
    temporary = opened.path.parent
    opened.close()
    assert not temporary.exists(), "the plain copy is gone once the phase is done"


def test_a_sealed_bundle_that_is_not_its_manifests_bytes_is_refused(tmp_path, monkeypatch, keys):
    repo = _Repository(tmp_path / "repo")
    monkeypatch.setattr(B.common_cli, "run", repo.run)
    monkeypatch.setattr(B, "_host_of", lambda server_id, **_k: {"host": server_id})
    B.carry_to_repository(SimpleNamespace(server_id="S"), bundle_dir=_bundle(tmp_path / "b"),
                          repository_path="/r/S", repository_host={}, app_config=None, secrets={}, passphrase="p")
    (tmp_path / "repo" / "r" / "S" / B.SEALED_NAME).write_bytes(b"changed")

    with pytest.raises(B.BundleError, match="not the bytes its manifest names"):
        B.open_for_restore(SimpleNamespace(source="repository", server_id="", path="/r/S"), source_server_id="S",
                           app_config=None, secrets={}, passphrase="p", repository_server_id="REPO")


def test_a_restore_from_a_server_reads_its_folder(tmp_path, monkeypatch):
    repo = _Repository(tmp_path / "host")
    _bundle(tmp_path / "host" / "bundles" / "SRV-01")
    monkeypatch.setattr(B.common_cli, "run", repo.run)
    monkeypatch.setattr(B, "_host_of", lambda server_id, **_k: {"host": server_id})

    opened = B.open_for_restore(SimpleNamespace(source="server", server_id="FILES-01", path="/bundles/SRV-01"),
                                source_server_id="SRV-01", secrets={})

    assert (opened.path / "server" / "logins.sql").is_file() and opened.said == "FILES-01:/bundles/SRV-01"
    opened.close()


def test_a_restore_from_the_node_reads_the_export_where_it_was_written():
    opened = B.open_for_restore(SimpleNamespace(source="node"), source_server_id="SRV-01")
    assert opened.path == SM.instance_bundle_dir("SRV-01")
    opened.close()


# --------------------------------------------------------------------------- #
# The phase: a bundle that cannot be had is a warning, never a lost restore
# --------------------------------------------------------------------------- #
def test_a_bundle_that_cannot_be_fetched_skips_the_phase_and_says_why(monkeypatch):
    def refuse(*_a, **_k):
        raise B.BundleError("the repository did not answer")

    monkeypatch.setattr(B, "open_for_restore", refuse)
    monkeypatch.setattr(SM, "_fetch_context", lambda _d: {})
    monkeypatch.setattr(SM, "repository_of", lambda _s: "REPO")
    plan = SM.ServerMetadataPlan(enabled=True, source="repository", path="/r/S")

    line, report = SM.replay_phase(plan, phase="pre-database", label="R", source_server_id="S")

    assert report["status"] == "SKIPPED" and "the repository did not answer" in report["error"]
    assert line.startswith("PHASE=metadata-pre-database SKIPPED")


def test_the_fetched_bundle_is_replayed_and_then_removed(monkeypatch, tmp_path):
    closed = []
    bundle = _bundle(tmp_path / "fetched")
    monkeypatch.setattr(B, "open_for_restore", lambda *a, **k: SimpleNamespace(
        path=bundle, said="REPO:/r/S (decrypted)", close=lambda: closed.append(True)))
    monkeypatch.setattr(SM, "_fetch_context", lambda _d: {})
    monkeypatch.setattr(SM, "repository_of", lambda _s: "REPO")
    monkeypatch.setattr(SM, "resolve_replay_target", lambda *a, **k: "TGT")
    seen = []
    monkeypatch.setattr(SM, "replay_for_restore", lambda plan, **k: seen.append(k["bundle_dir"]) or {"ok": True})
    plan = SM.ServerMetadataPlan(enabled=True, source="repository", path="/r/S")

    _line, report = SM.replay_phase(plan, phase="pre-database", label="R", source_server_id="S")

    assert seen == [bundle] and report.get("ok", True) and closed == [True]
