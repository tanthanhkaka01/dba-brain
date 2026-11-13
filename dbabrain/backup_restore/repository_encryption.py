"""Encrypt each proven repository copy with age, AT the repository, then let the plain file go (0.37.0).

The operator, 2026-11-10 (translated): *copy to the shared repository, compare checksum and bytes;
when that is OK, encrypt at the repository; when the encryption is OK, delete the old file - on the
repository and on the server_id. The option is declared, required, off by default*; *the key has a
lifetime, 90 days by default - past it the old key no longer encrypts, it only decrypts*.

For an entry with ``offload_encrypt``, after its copies are proven on the repository, each catalog
row whose copy is not yet encrypted goes through, file by file (a base backup directory is several):

1. **the key** - the server_id's newest key whose lifetime is open and whose private half is sealed
   and proven as a file (:func:`key_for`); none open means a NEW key: made here, its private half
   stored encrypted in the runtime store AND sealed as a file for dbabrain and the offline recovery
   key - and used only once that file is proven. An expired key is never picked, and
   ``encrypt-file`` refuses one on its own clock too. No key is ever deleted;
2. ``encrypt-file`` on the repository: the plain file held to its bytes and sha256, ``<file>.age``
   written for the key's PUBLIC half only - the repository cannot decrypt what it holds;
3. ``verify-encrypted-file``: the ``.age`` streamed back, decrypted on the way with the key's private
   half, the plaintext held to the plain file's bytes and sha256;
4. only then ``delete-verified-file`` removes the plain file on the repository - one file, with
   every fact. The source is released later by the offload's own rules (``offload_delete_source``).

Each step is recorded (``repository_encrypted_files``): the plain and the encrypted file's bytes and
sha256, the key, the age version, when encrypted, proven and the plain deleted.
"""

from __future__ import annotations

import posixpath
from datetime import datetime, timezone
from typing import Any

from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore
from dbabrain.lib import errors
from dbabrain.lib import integrity as I
from dbabrain.lib import repository_keys as K
from dbabrain.lib import vault_file as V
from dbabrain.transport import common_cli

KEY_FORMAT = "dbabrain-repository-key/1"
#: Where a repository key's sealed file goes, under the vault's file_folder.
KEYS_FOLDER = "_repository_keys"
_MARGIN = 120


class EncryptRefused(errors.Refused):
    """No key could be made or proven, or a step did not prove."""


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
class Sealing:
    """A pass that encrypts its copies ON THE WAY (1.330): the key, and each copy proven to decrypt back.

    ``copy-files`` lands ``<file>.age`` with the plain bytes held to the backup's facts; the gate's
    target hash is then ``verify-encrypted-file`` - the ``.age`` streamed back and decrypted, its
    plaintext's bytes and sha256 handed to the gate as the copy's - so a row is verified only when its
    copy decrypts to the backup. :meth:`record` then writes each file as encrypted, proven, and with no
    plain file on the repository (none ever landed there): the row is sealed in the pass that copied it."""

    def __init__(self, *, store: RepositoryEncryptionStore, key: dict[str, Any], identity: str) -> None:
        self.store, self.key, self.identity = store, key, identity
        #: target (plain) path -> the copy's encrypted facts, proven
        self.proven: dict[str, dict[str, Any]] = {}

    def encrypt_block(self) -> dict[str, Any]:
        return {"recipients": [str(self.key["recipient"])], "key_id": int(self.key["key_id"]),
                "key_expires_at": str(self.key["encrypt_until"])}

    def verify(self, *, host: Any, transfer: str, path: str, facts: dict[str, Any], plain: tuple[str, int],
               timeout: int) -> tuple[str, int] | None:
        """``(plain sha256, plain bytes)`` the ``.age`` decrypts to - or None when it does not prove."""
        ok, data, error = common_cli.run_allowing_failure("verify-encrypted-file", {
            "host": host, "path": str(facts["encrypted_path"]),
            "size_bytes": int(facts["encrypted_size_bytes"]), "sha256": str(facts["encrypted_sha256"]).lower(),
            "plain_size_bytes": int(plain[1]), "plain_sha256": str(plain[0]).lower(), "transfer": transfer,
            "identity": self.identity}, timeout_seconds=timeout + _MARGIN)
        if not ok:
            facts["refused"] = error
            return None
        self.proven[path] = {**facts, "plain_sha256": str(data["plain_sha256"]).lower(),
                             "plain_size_bytes": int(data["plain_size_bytes"])}
        return self.proven[path]["plain_sha256"], self.proven[path]["plain_size_bytes"]

    def record(self, entry: Any, row: dict[str, Any], targets: list[str]) -> int:
        """Each file of a verified row as encrypted, proven, no plain on the repository; the row done."""
        recorded = {str(r["plain_path"]) for r in self.store.files_of(int(row["file_id"]))}
        moment = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = 0
        for target in targets:
            facts = self.proven.get(target)
            if facts is None:
                raise EncryptRefused(f"{target}: verified, and no proven .age is known for it.")
            if target not in recorded:
                made = self.store.record_encrypted(
                    file_id=int(row["file_id"]), backup_id=entry.backup_id, server_id=entry.server_id,
                    repository_server_id=entry.offload.server_id, plain_path=target,
                    plain_size_bytes=facts["plain_size_bytes"], plain_sha256=facts["plain_sha256"],
                    encrypted_path=str(facts["encrypted_path"]), encrypted_size_bytes=int(facts["encrypted_size_bytes"]),
                    encrypted_sha256=str(facts["encrypted_sha256"]).lower(), key_id=int(self.key["key_id"]),
                    age_version=str(facts.get("age_version") or ""), encrypted_at=moment)
                self.store.mark_proven(int(made["encrypted_file_id"]))
                # No plain file ever landed: the state the rest of the flow reads as "plain gone".
                self.store.mark_plain_deleted(int(made["encrypted_file_id"]))
            count += 1
        self.store.mark_row_done(file_id=int(row["file_id"]), backup_id=entry.backup_id,
                                 repository_server_id=entry.offload.server_id, encrypted_files=count,
                                 key_id=int(self.key["key_id"]))
        return count


def sealing_for(entry: Any, *, app_config: Any, secrets: dict[str, str], passphrase: str,
                now: datetime | None = None) -> Sealing:
    store = RepositoryEncryptionStore.from_config(app_config)
    key, identity = key_for(entry, app_config=app_config, store=store, secrets=secrets, passphrase=passphrase,
                            now=now or datetime.now(timezone.utc))
    return Sealing(store=store, key=key, identity=identity)


def key_for(entry: Any, *, app_config: Any, store: RepositoryEncryptionStore, secrets: dict[str, str],
            passphrase: str, now: datetime, made_by: str = "offload") -> tuple[dict[str, Any], str]:
    """``(key row, identity)`` to encrypt the entry's next files with - never an expired key."""
    rows = store.keys(server_id=entry.server_id)
    proven = [r for r in rows if r.get("proven_at")]
    chosen = K.key_for_encryption(K.key_of(proven), server_id=entry.server_id, now=now)
    if chosen is not None:
        return store.key(chosen.key_id), store.identity(chosen.key_id, passphrase=passphrase)
    # A key made before whose sealed file never proved, still open: sealed now, not made again.
    unsealed = K.key_for_encryption(K.key_of([r for r in rows if not r.get("proven_at")]),
                                    server_id=entry.server_id, now=now)
    if unsealed is not None:
        row, identity = store.key(unsealed.key_id), store.identity(unsealed.key_id, passphrase=passphrase)
    else:
        identity, recipient = V.new_identity()
        row = store.add_key(server_id=entry.server_id, identity=identity, recipient=recipient,
                            created_at=K.time_text(now), days=int(entry.offload.encrypt_key_days),
                            passphrase=passphrase, created_by=made_by)
    path = seal_key(app_config, row, identity, secrets=secrets)
    store.mark_key_sealed(int(row["key_id"]), sealed_file=path)
    return store.key(int(row["key_id"])), identity


def seal_key(app_config: Any, row: dict[str, Any], identity: str, *, secrets: dict[str, str]) -> str:
    """The key's private half as ONE file for dbabrain and the recovery key - written once, proven."""
    from dbabrain.backup_restore import certificate_files as F

    found = F._required(app_config)
    owner = F._identity(found, secrets)
    content = {"format": KEY_FORMAT, "key_id": int(row["key_id"]), "server_id": str(row["server_id"]),
               "algorithm": K.ALGORITHM, "recipient": str(row["recipient"]), "identity": identity,
               "created_at": str(row["created_at"]), "encrypt_until": str(row["encrypt_until"])}
    path = F.folder(app_config, found) / KEYS_FOLDER / str(row["server_id"]) / f"key_{int(row['key_id'])}{V.SUFFIX}"
    if path.exists():
        opened = V.unseal(path.read_bytes(), owner)
        if opened != content:
            raise EncryptRefused(f"{path} is there and is not this key - never overwritten.")
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(V.seal(content, [V.recipient_of(owner), *found.recovery_recipients]))
    partial.replace(path)
    if V.unseal(path.read_bytes(), owner) != content:
        raise EncryptRefused(f"{path} was written and does not open to this key.")
    return str(path)


# --------------------------------------------------------------------------- #
# The files of one catalog row on the repository
# --------------------------------------------------------------------------- #
def plain_files(entry: Any, row: dict[str, Any], hosts: Any) -> list[tuple[str, int, str]]:
    """``(path, bytes, sha256)`` of each plain file the row's copy is on the repository.

    One file: the catalog's own facts. A base backup directory: its files as the repository holds
    them now (``.age`` ones left out), its ``backup_manifest`` held to the catalog's checksum - the
    manifest is what pg_verifybackup holds the rest to."""
    path = str(row["offload_path"])
    member = I.HASHED_MEMBER.get(str(row["file_type"]).upper())
    if not member:
        return [(path, int(row["size_bytes"]), str(row["checksum_after_backup"]).lower())]
    timeout = int(entry.offload.hash_timeout_seconds)
    listing = common_cli.run("file-checksum", {"host": hosts.repository_host, "folder": path,
                                               "timeout_seconds": timeout}, timeout_seconds=timeout + _MARGIN)
    files = [(str(f["path"]), int(f["size_bytes"]), str(f["sha256"]).lower()) for f in listing.get("files") or []
             if not str(f["path"]).endswith(K.SUFFIX)]
    manifest = [f for f in files if posixpath.basename(f[0]) == member]
    if manifest and manifest[0][2] != str(row.get("checksum_after_backup") or "").lower():
        raise EncryptRefused(f"{path}/{member} is not the manifest the backup recorded - nothing in it is encrypted.")
    return files


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #
def encrypt_rows(entry: Any, *, app_config: Any, store: RepositoryEncryptionStore, hosts: Any,
                 rows: list[dict[str, Any]], secrets: dict[str, str], passphrase: str,
                 now: datetime | None = None) -> dict[str, Any]:
    """Encrypt, prove and drop the plain file of each row's copy. Answers the counts and each refusal;
    one file's failure stops its row, never the others."""
    moment = now or datetime.now(timezone.utc)
    done = store.done_rows(backup_id=entry.backup_id)
    todo = [r for r in rows if int(r["file_id"]) not in done and r.get("offload_state") == "verified"]
    answer: dict[str, Any] = {"rows": len(todo), "encrypted": 0, "files": 0, "failed": [], "waiting": []}
    if not todo:
        return answer
    key, identity = key_for(entry, app_config=app_config, store=store, secrets=secrets, passphrase=passphrase,
                            now=moment)
    answer["key_id"] = int(key["key_id"])
    for row in todo:
        try:
            count = _encrypt_row(entry, row, store=store, hosts=hosts, key=key, identity=identity,
                                 passphrase=passphrase)
        except StillEncrypting as exc:
            answer["waiting"].append(f"{row['file_name']}: {exc}"[:600])
            continue
        except Exception as exc:  # noqa: BLE001 - this row's, said by name
            answer["failed"].append(f"{row['file_name']}: {type(exc).__name__}: {exc}"[:600])
            continue
        store.mark_row_done(file_id=int(row["file_id"]), backup_id=entry.backup_id,
                            repository_server_id=entry.offload.server_id, encrypted_files=count,
                            key_id=int(key["key_id"]))
        answer["encrypted"] += 1
        answer["files"] += count
    return answer


class StillEncrypting(errors.Refused):
    """A row whose file an earlier pass is still encrypting on the repository: it waits, it has not failed."""


#: How long apart the two listings of a partial are. Sizes are compared, not modification times: the
#: repository's clock and this node's need not agree, and a growing file says it is being written
#: whatever either clock reads.
PARTIAL_SETTLE_SECONDS = 15
_sleep = __import__("time").sleep


def _partials(row: dict[str, Any], hosts: Any, timeout: int) -> dict[str, int]:
    """``{partial path: bytes}`` of the ``<file>.age.<pid>.partial`` files under the row's copy."""
    path = str(row["offload_path"])
    folder = path if I.HASHED_MEMBER.get(str(row["file_type"]).upper()) else posixpath.dirname(path)
    listing = common_cli.run("list-files", {"host": hosts.repository_host, "folder": folder,
                                            "timeout_seconds": timeout}, timeout_seconds=timeout + _MARGIN)
    return {str(f["path"]): int(f.get("size_bytes") or 0) for f in listing.get("files") or []
            if ".age." in str(f["path"]) and str(f["path"]).endswith(".partial")}


def _still_being_written(row: dict[str, Any], files: list[tuple[str, int, str]], *, recorded: dict[str, Any],
                         orphans: dict[str, Any], hosts: Any, timeout: int) -> list[str]:
    """The plain files of this row an earlier pass is still encrypting - their partial still grows.

    A pass killed while the repository encrypts (a deploy, a restart) does not stop the script there: it
    runs to its end and moves its ``.age`` into place. A pass started meanwhile found no ``.age`` yet and
    encrypted the same file a second time - an hour of disk for a 53 GB FULL, ending ``EXISTS_AFTER``
    (1.331, the operator 2026-11-11: *wait*). A partial that has stopped growing is a dead one: this
    pass encrypts, and the leftover is the repository check's to name - never deleted here."""
    wanted = {path for path, _size, _sha in files if path not in recorded and path not in orphans}
    if not wanted:
        return []
    first = {p: size for p, size in _partials(row, hosts, timeout).items()
             if p.split(".age.", 1)[0] in wanted}
    if not first:
        return []
    _sleep(PARTIAL_SETTLE_SECONDS)
    second = _partials(row, hosts, timeout)
    return sorted(p.split(".age.", 1)[0] for p, size in first.items() if second.get(p, -1) > size)


def _orphans(files: list[tuple[str, int, str]], *, recorded: dict[str, Any], hosts: Any,
             timeout: int) -> dict[str, dict[str, Any]]:
    """The ``.age`` files already on the repository for plain files the store has no record of.

    An encryption killed after its ``.age`` was moved into place - its run stopped by a deploy, an
    out-of-memory, a lost connection - and before the store recorded it left a file ``encrypt-file``
    refuses (``EXISTS``: never replaced), so every later pass failed the row for good (1.331 b,
    read from the code 2026-11-11). One ``file-checksum`` for the whole row: a base backup is
    thousands of files, and one round trip each would cost every pass, not only the rare one."""
    wanted = [path + K.SUFFIX for path, _size, _sha in files if path not in recorded]
    if not wanted:
        return {}
    found = common_cli.run("file-checksum", {"host": hosts.repository_host, "paths": wanted,
                                             "timeout_seconds": timeout}, timeout_seconds=timeout + _MARGIN)
    return {str(f["path"])[: -len(K.SUFFIX)]: f for f in found.get("files") or []
            if str(f.get("path") or "").endswith(K.SUFFIX)}


def _adopt_orphan(entry: Any, row: dict[str, Any], *, path: str, size: int, sha: str, found: dict[str, Any],
                  store: RepositoryEncryptionStore, hosts: Any, key: dict[str, Any], identity: str,
                  timeout: int) -> dict[str, Any]:
    """Record an orphan ``.age`` - only once it decrypts back to the catalog's own bytes and sha256.

    The proof is the one every encrypted file gets before its plain file may go
    (``verify-encrypted-file``), with this pass's key: an orphan is the work of a pass that died
    minutes or hours before, so it was made with the key in force now. One that does not decrypt to
    the catalog's file is never adopted and never deleted - it is named, and the row stops for the
    operator."""
    encrypted_path = path + K.SUFFIX
    ok, _data, error = common_cli.run_allowing_failure("verify-encrypted-file", {
        "host": hosts.target, "path": encrypted_path,
        "size_bytes": int(found["size_bytes"]), "sha256": str(found["sha256"]).lower(),
        "plain_size_bytes": size, "plain_sha256": sha, "transfer": entry.offload.transfer,
        "identity": identity}, timeout_seconds=timeout + _MARGIN)
    if not ok:
        raise EncryptRefused(
            f"{encrypted_path} is on the repository with no record of it, and it does not decrypt with key "
            f"#{int(key['key_id'])} to the catalog's {size} bytes and sha256 - left as it is for the operator "
            f"({error})"[:600])
    adopted = store.record_encrypted(
        file_id=int(row["file_id"]), backup_id=entry.backup_id, server_id=entry.server_id,
        repository_server_id=entry.offload.server_id, plain_path=path, plain_size_bytes=size, plain_sha256=sha,
        encrypted_path=encrypted_path, encrypted_size_bytes=int(found["size_bytes"]),
        encrypted_sha256=str(found["sha256"]).lower(), key_id=int(key["key_id"]), age_version="",
        encrypted_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    store.mark_proven(int(adopted["encrypted_file_id"]))
    return {**adopted, "proven_at": "adopted"}


def _encrypt_row(entry: Any, row: dict[str, Any], *, store: RepositoryEncryptionStore, hosts: Any,
                 key: dict[str, Any], identity: str, passphrase: str) -> int:
    recorded = {str(r["plain_path"]): r for r in store.files_of(int(row["file_id"]))}
    files = plain_files(entry, row, hosts)
    count = sum(1 for r in recorded.values() if r.get("plain_deleted_at"))
    if not files and not count:
        raise EncryptRefused(f"{row['offload_path']}: nothing on the repository to encrypt.")
    timeout = int(entry.offload.hash_timeout_seconds)
    orphans = _orphans(files, recorded=recorded, hosts=hosts, timeout=timeout)
    busy = _still_being_written(row, files, recorded=recorded, orphans=orphans, hosts=hosts, timeout=timeout)
    if busy:
        raise StillEncrypting(f"{len(busy)} file(s) still being encrypted on the repository by an earlier pass "
                              f"({posixpath.basename(busy[0])}) - this pass waits for it")
    for path, size, sha in files:
        earlier = recorded.get(path)
        if earlier is None and path in orphans:
            earlier = _adopt_orphan(entry, row, path=path, size=size, sha=sha, found=orphans[path], store=store,
                                    hosts=hosts, key=key, identity=identity, timeout=timeout)
        if earlier is None:
            made = common_cli.run("encrypt-file", {
                "host": hosts.repository_host, "path": path, "file_name": posixpath.basename(path),
                "size_bytes": size, "sha256": sha, "must_be_under": entry.offload.folder,
                "recipients": [str(key["recipient"])], "key_id": int(key["key_id"]),
                "key_expires_at": str(key["encrypt_until"]), "timeout_seconds": timeout},
                timeout_seconds=timeout + _MARGIN)
            earlier = store.record_encrypted(
                file_id=int(row["file_id"]), backup_id=entry.backup_id, server_id=entry.server_id,
                repository_server_id=entry.offload.server_id, plain_path=path, plain_size_bytes=size,
                plain_sha256=sha, encrypted_path=made["encrypted_path"],
                encrypted_size_bytes=int(made["encrypted_size_bytes"]), encrypted_sha256=made["encrypted_sha256"],
                key_id=int(key["key_id"]), age_version=made.get("age_version") or "",
                encrypted_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            item_identity = identity
        else:
            # Encrypted by an earlier pass - perhaps with a key since expired: proven with ITS key.
            item_identity = identity if int(earlier["key_id"]) == int(key["key_id"]) else None
        if not earlier.get("proven_at"):
            common_cli.run("verify-encrypted-file", {
                "host": hosts.target, "path": earlier["encrypted_path"],
                "size_bytes": int(earlier["encrypted_size_bytes"]), "sha256": earlier["encrypted_sha256"],
                "plain_size_bytes": size, "plain_sha256": sha, "transfer": entry.offload.transfer,
                "identity": item_identity or store.identity(int(earlier["key_id"]), passphrase=passphrase)},
                timeout_seconds=timeout + _MARGIN)
            store.mark_proven(int(earlier["encrypted_file_id"]))
        if not earlier.get("plain_deleted_at"):
            ok, data, error = common_cli.run_allowing_failure("delete-verified-file", {
                "host": hosts.repository_host, "path": path, "file_name": posixpath.basename(path),
                "size_bytes": size, "sha256": sha, "must_be_under": entry.offload.folder},
                timeout_seconds=timeout + _MARGIN)
            line = (data or {}).get("file") or {}
            if not ok or line.get("status") not in ("deleted", "not_found"):
                raise EncryptRefused(f"{path}: encrypted and proven, but the plain file was not deleted "
                                     f"({line.get('status') or 'refused'}: {line.get('reason') or error}).")
            store.mark_plain_deleted(int(earlier["encrypted_file_id"]))
            count += 1
    return count


def is_encrypted(store: RepositoryEncryptionStore, *, backup_id: str) -> set[int]:
    """The catalog rows of an entry whose repository copy is age files now, not plain ones."""
    return set(store.done_rows(backup_id=backup_id)) | {
        int(r["file_id"]) for r in _any_encrypted(store, backup_id)}


def sealed_rows(store: RepositoryEncryptionStore, *, backup_id: str) -> dict[int, list[dict[str, Any]]]:
    """The catalog rows of an entry whose repository copy is WHOLLY age files, every one proven to
    decrypt to the backup's bytes and its plain file gone - each with those files. Only such a row's
    source may be released: a row encrypted in part has no whole proven copy on the repository."""
    sealed: dict[int, list[dict[str, Any]]] = {}
    for file_id, done in store.done_rows(backup_id=backup_id).items():
        files = store.files_of(file_id)
        if (files and len(files) >= int(done.get("encrypted_files") or 0)
                and all(f.get("proven_at") and f.get("plain_deleted_at") for f in files)):
            sealed[file_id] = files
    return sealed


def _any_encrypted(store: RepositoryEncryptionStore, backup_id: str) -> list[dict[str, Any]]:
    store.initialize()
    with store.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT DISTINCT file_id FROM repository_encrypted_files WHERE backup_id = ?", (backup_id,)).fetchall()]
