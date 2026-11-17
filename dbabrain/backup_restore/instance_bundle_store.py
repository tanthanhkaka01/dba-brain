"""Where a restore reads an instance's metadata bundle, and how a backup carries it to the repository (0.40.0, 1.376).

The operator, 2026-11-15: *restoring metadata must be a field of its own, required, to choose whether
to restore metadata or not; and when yes, from the repository or from a server_id, and from which path
- it can be another folder than the one holding the backup files.* Decided 2026-11-16: three sources -
the dbabrain node (``runtime/instance_bundles/<server_id>``, where the export writes it), a ``server_id``
and a path, or the repository and a path - and the backup's offload carries the bundle there.

**The bundle holds login password hashes and credentials**, so it never rests on the repository in the
clear: the node packs the bundle folder into one tar, encrypts it with age for the source server's
repository key (its public half - the repository can never decrypt it), and pushes the ``.age`` and a
manifest of the facts that prove it (sizes, sha256, the key) - each push proven on arrival. A restore
pulls them back to the node, decrypts with the key's identity from dbabrain's vault, holds the tar to
the manifest, unpacks it into a folder of its own, replays from there, and deletes that folder.

Plumbing for the workflow and the restore steps: nothing here says a step.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from dbabrain.lib import errors
from dbabrain.lib import repository_keys as K
from dbabrain.transport import common_cli

#: The files a carried bundle is, in its repository folder.
SEALED_NAME = "instance_bundle.tar.age"
MANIFEST_NAME = "instance_bundle.json"
FORMAT = "dbabrain-instance-bundle/1"

SOURCES = ("node", "server", "repository")


class BundleError(errors.Refused):
    """The bundle could not be had - named, so the metadata step says why and the restore stands."""


# --------------------------------------------------------------------------- #
# Pack, seal, open
# --------------------------------------------------------------------------- #
def pack(bundle_dir: Path) -> bytes:
    """The bundle folder as one tar - every file under it, in a stable order."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
            tar.add(path, arcname=path.relative_to(bundle_dir).as_posix(), recursive=False)
    return buffer.getvalue()


def unpack(data: bytes, into: Path) -> int:
    """A packed bundle laid out under ``into`` - never outside it. Answers how many files."""
    count = 0
    root = into.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            destination = (into / PurePosixPath(member.name)).resolve()
            if root not in destination.parents:
                raise BundleError(f"the bundle names a file outside its folder: {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            destination.write_bytes(source.read() if source else b"")
            count += 1
    return count


def _facts(data: bytes) -> dict[str, Any]:
    return {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def seal(data: bytes, recipient: str) -> bytes:
    pyrage, x25519 = K.age()
    return pyrage.encrypt(data, [x25519.Recipient.from_str(recipient.strip())])


def open_sealed(data: bytes, identity: str) -> bytes:
    pyrage, x25519 = K.age()
    return pyrage.decrypt(data, [x25519.Identity.from_str(identity.strip())])


# --------------------------------------------------------------------------- #
# The backup: carried to the repository
# --------------------------------------------------------------------------- #
def carry_to_repository(entry: Any, *, bundle_dir: Path, repository_path: str, repository_host: dict[str, Any],
                        app_config: Any, secrets: dict[str, str], passphrase: str) -> dict[str, Any]:
    """The bundle folder packed, sealed for the server's repository key and pushed with its manifest to
    ``repository_path`` - each file proven on arrival. Answers what went where."""
    from dbabrain.backup_restore.repository_encryption import key_for
    from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore

    if not (bundle_dir / "server").is_dir():
        raise BundleError(f"no bundle at {bundle_dir}: the export wrote none for this run")
    plain = pack(bundle_dir)
    store = RepositoryEncryptionStore.from_config(app_config)
    key, _identity = key_for(entry, app_config=app_config, store=store, secrets=secrets, passphrase=passphrase,
                             now=datetime.now(timezone.utc), made_by="instance-bundle")
    sealed = seal(plain, str(key["recipient"]))
    manifest = {"format": FORMAT, "server_id": entry.server_id, "key_id": int(key["key_id"]),
                "carried_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "plain": _facts(plain), "sealed": _facts(sealed)}
    folder = repository_path.rstrip("/")
    with tempfile.TemporaryDirectory(prefix="dbabrain_bundle_") as local:
        sealed_path = Path(local) / SEALED_NAME
        sealed_path.write_bytes(sealed)
        manifest_path = Path(local) / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        for path in (sealed_path, manifest_path):
            common_cli.run("push-file", {"local_path": str(path), "remote_path": f"{folder}/{path.name}",
                                         "host": repository_host})
    return {"path": folder, "key_id": manifest["key_id"], "files": len(list(bundle_dir.rglob("*.*"))),
            "sealed_bytes": manifest["sealed"]["size_bytes"]}


# --------------------------------------------------------------------------- #
# The restore: the bundle from where its entry says
# --------------------------------------------------------------------------- #
class OpenedBundle:
    """A bundle folder the replay reads - removed when it was fetched for this restore."""

    def __init__(self, path: Path, *, said: str, temporary: Path | None = None) -> None:
        self.path, self.said, self._temporary = path, said, temporary

    def close(self) -> None:
        if self._temporary is not None:
            shutil.rmtree(self._temporary, ignore_errors=True)
            self._temporary = None


def open_for_restore(plan: Any, *, source_server_id: str, data_dir: Any = None, app_config: Any = None,
                     secrets: dict[str, str] | None = None, passphrase: str = "",
                     repository_server_id: str = "") -> OpenedBundle:
    """The bundle the entry states - ``node``, ``server`` (its ``server_id`` and ``path``) or ``repository``
    (``path`` on the source's repository, decrypted on the node). Raises :class:`BundleError` by name."""
    from dbabrain.backup_restore.server_metadata import instance_bundle_dir

    source = str(getattr(plan, "source", "") or "node")
    if source == "node":
        return OpenedBundle(instance_bundle_dir(source_server_id), said=f"the dbabrain node's copy for {source_server_id}")
    temporary = Path(tempfile.mkdtemp(prefix="dbabrain_bundle_"))
    try:
        if source == "server":
            host = _host_of(str(plan.server_id), secrets=secrets or {}, data_dir=data_dir)
            _pull_folder(host, str(plan.path), into=temporary / "bundle")
            return OpenedBundle(temporary / "bundle", said=f"{plan.server_id}:{plan.path}", temporary=temporary)
        if source == "repository":
            if not repository_server_id:
                raise BundleError(f"no repository is stated for {source_server_id}: its backup entries offload "
                                  "nowhere")
            host = _host_of(repository_server_id, secrets=secrets or {}, data_dir=data_dir)
            files = _pull_sealed(host, str(plan.path), into=temporary)
            _decrypt_into(files, into=temporary / "bundle", app_config=app_config, passphrase=passphrase)
            return OpenedBundle(temporary / "bundle", said=f"{repository_server_id}:{plan.path} (decrypted)",
                                temporary=temporary)
        raise BundleError(f"server_metadata.source {source!r} - one of {', '.join(SOURCES)}")
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _host_of(server_id: str, *, secrets: dict[str, str], data_dir: Any) -> dict[str, Any]:
    from dbabrain.backup_restore.backup import resolve_ssh_target
    from dbabrain.backup_restore.spec_builder import host_block

    target = resolve_ssh_target(server_id, label=f"instance bundle on {server_id}", data_dir=data_dir,
                                require_container=False)
    return host_block(target, secrets=secrets, data_dir=data_dir)


def _pull_folder(host: dict[str, Any], folder: str, *, into: Path) -> None:
    listing = common_cli.run("list-files", {"host": host, "folder": folder, "timeout_seconds": 600})
    files = [item for item in listing.get("files") or [] if item.get("type", "file") == "file"]
    if not files:
        raise BundleError(f"{folder}: holds no bundle file")
    root = folder.rstrip("/\\")
    for item in files:
        relative = str(item["path"])[len(root):].lstrip("/\\").replace("\\", "/")
        local = into / PurePosixPath(relative)
        local.parent.mkdir(parents=True, exist_ok=True)
        common_cli.run("pull-file", {"remote_path": item["path"], "local_path": str(local), "host": host})
    if not (into / "server").is_dir():
        raise BundleError(f"{folder}: is not an instance bundle (no server/ folder)")


def _pull_sealed(host: dict[str, Any], folder: str, *, into: Path) -> dict[str, Path]:
    pulled = {}
    for name in (MANIFEST_NAME, SEALED_NAME):
        local = into / name
        common_cli.run("pull-file", {"remote_path": f"{folder.rstrip('/')}/{name}", "local_path": str(local),
                                     "host": host})
        pulled[name] = local
    return pulled


def _decrypt_into(files: dict[str, Path], *, into: Path, app_config: Any, passphrase: str) -> None:
    from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore

    manifest = json.loads(files[MANIFEST_NAME].read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise BundleError(f"the bundle's manifest is not {FORMAT}: {manifest.get('format')!r}")
    sealed = files[SEALED_NAME].read_bytes()
    if _facts(sealed) != manifest["sealed"]:
        raise BundleError("the sealed bundle is not the bytes its manifest names")
    identity = RepositoryEncryptionStore.from_config(app_config).identity(int(manifest["key_id"]),
                                                                          passphrase=passphrase)
    plain = open_sealed(sealed, identity)
    if _facts(plain) != manifest["plain"]:
        raise BundleError("the decrypted bundle is not the bytes its manifest names")
    files[SEALED_NAME].unlink()
    unpack(plain, into)
