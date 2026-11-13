"""A backup is encrypted with age AT the repository, by a key that encrypts for 90 days and decrypts for ever (0.37.0).

Why it matters: the operator, 2026-11-10 - *one encryption for every database type, at the shared
repository: copy, compare checksum and bytes, encrypt there, and once that is proven delete the old
file*; *the key has a lifetime, 90 days by default - one moment past it the old key can no longer
encrypt, only decrypt*; and *test with the old key, a wrong key, no key*. A key that kept encrypting
after its end, a plain file deleted before its encrypted copy was proven, or an encrypted file that
only one lost key could open would each lose backups. So: the rules at the second, the command on a
real POSIX shell with a real age (the ``age`` tool here is a shim over ``pyrage`` - the same format),
and the pass end to end on a real store.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pyrage = pytest.importorskip("pyrage")

from dbabrain.backup_restore import repository_encryption as E  # noqa: E402
from dbabrain.common import deleteverified, repoencrypt  # noqa: E402
from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore  # noqa: E402
from dbabrain.lib import repository_keys as K  # noqa: E402
from dbabrain.lib import vault_file as V  # noqa: E402

WINDOWS_HERE = os.name == "nt"
SH = (r"C:\Program Files\Git\bin\bash.exe" if WINDOWS_HERE and os.path.exists(r"C:\Program Files\Git\bin\bash.exe")
      else (shutil.which("sh") if not WINDOWS_HERE else None))
needs_sh = pytest.mark.skipif(not SH, reason="a POSIX shell runs the repository's script")
T0 = datetime(2026, 11, 10, 6, 0, 0, tzinfo=timezone.utc)
PLAIN = bytes(range(256)) * 300 + b"tail of a backup"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _posix(path: Path | str) -> str:
    text = str(path)
    if WINDOWS_HERE and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


def _local(path: str) -> Path:
    if WINDOWS_HERE and len(path) > 2 and path[0] == "/" and path[2] == "/":
        return Path(f"{path[1].upper()}:{path[2:]}")
    return Path(path)


# --------------------------------------------------------------------------- #
# The rules, to the second
# --------------------------------------------------------------------------- #
def _key(key_id, server="SRV-01", *, made=T0, days=90):
    return K.RepositoryKey(key_id=key_id, server_id=server, recipient=f"age1{'q' * 58}", created_at=made,
                           encrypt_until=K.encrypt_until(made, days))


def test_a_key_encrypts_until_the_second_before_its_end_and_never_at_or_after_it():
    until = K.encrypt_until(T0, 90)
    assert until == T0 + timedelta(days=90)
    assert K.may_encrypt(until, until - timedelta(seconds=1))
    assert not K.may_encrypt(until, until)
    assert not K.may_encrypt(until, until + timedelta(seconds=1))
    assert not K.may_encrypt(until, until + timedelta(days=4000))


def test_the_newest_open_key_of_the_server_is_chosen_and_an_expired_one_never():
    old, new, other = _key(1), _key(2, made=T0 + timedelta(days=30)), _key(3, server="SRV-02", made=T0 + timedelta(days=60))
    assert K.key_for_encryption([old, new, other], server_id="SRV-01", now=T0 + timedelta(days=31)) == new
    # Day 90 after key 1 and day 60 after key 2: key 1 has closed, key 2 is still open.
    assert K.key_for_encryption([old, new], server_id="SRV-01", now=T0 + timedelta(days=90)) == new
    # Both closed: none - a new key is made, an old one is never stretched.
    assert K.key_for_encryption([old, new], server_id="SRV-01", now=T0 + timedelta(days=120)) is None
    assert K.key_for_encryption([other], server_id="SRV-01", now=T0) is None


@pytest.mark.parametrize("days", [0, -1, 3651, True, "90", 90.0, None])
def test_a_lifetime_that_is_not_whole_days_1_to_3650_is_refused(days):
    with pytest.raises(K.RepositoryKeyError):
        K.encrypt_until(T0, days)


# --------------------------------------------------------------------------- #
# encrypt-file on a real POSIX shell, with age
# --------------------------------------------------------------------------- #
@pytest.fixture
def age_shim(tmp_path):
    """An ``age`` on PATH that writes the real age format through pyrage."""
    folder = tmp_path / "bin"
    folder.mkdir()
    helper = folder / "age_shim.py"
    helper.write_text(
        "import sys, pyrage\nfrom pyrage import x25519\nargs = sys.argv[1:]\n"
        "if args == ['--version']:\n    print('v1.2.1 (shim)'); sys.exit(0)\n"
        "rec, out, inp, i = [], None, None, 0\n"
        "while i < len(args):\n"
        "    if args[i] == '-r': rec.append(x25519.Recipient.from_str(args[i + 1])); i += 2\n"
        "    elif args[i] == '-o': out = args[i + 1]; i += 2\n"
        "    else: inp = args[i]; i += 1\n"
        "data = open(inp, 'rb').read()\nopen(out, 'wb').write(pyrage.encrypt(data, rec))\n", encoding="utf-8")
    shim = folder / "age"
    shim.write_text(f'#!/bin/sh\nexec "{_posix(sys.executable)}" "{_posix(helper)}" "$@"\n', encoding="utf-8",
                    newline="\n")
    shim.chmod(0o755)
    return folder


@pytest.fixture
def repo(tmp_path, monkeypatch, age_shim):
    """A repository folder and a host whose commands run in the local POSIX shell, ``age`` on PATH."""
    root = tmp_path / "nas"
    root.mkdir()
    state = {"path": _posix(age_shim)}

    def run(host, command, *, timeout=300, session=None):
        prefix = f'export PATH="{state["path"]}:$PATH"; ' if state["path"] else 'export PATH="/usr/bin:/bin"; '
        done = subprocess.run([SH, "-c", prefix + command], capture_output=True, text=True, timeout=120)
        return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}

    monkeypatch.setattr(repoencrypt, "run", run)
    monkeypatch.setattr(deleteverified, "run", run)
    return SimpleNamespace(root=root, state=state)


def _plain(repo, name="APPDB_FULL_20261110.bak", data=PLAIN):
    folder = repo.root / "SRV-01" / "APPDB" / "FULL"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def _encrypt_request(path: Path, repo, recipient: str, **over):
    request = {"host": {"runtime": "linux", "host": "192.0.2.30"}, "path": _posix(path), "file_name": path.name,
               "size_bytes": path.stat().st_size, "sha256": _sha(path.read_bytes()), "must_be_under": _posix(repo.root),
               "recipients": [recipient], "key_id": 1, "key_expires_at": K.time_text(T0 + timedelta(days=90))}
    request.update(over)
    return request


@needs_sh
def test_the_file_is_encrypted_beside_itself_for_the_public_key_and_the_plain_file_is_left(repo):
    identity, recipient = V.new_identity()
    path = _plain(repo)

    answer = repoencrypt.encrypt_file(_encrypt_request(path, repo, recipient), now=lambda: T0)

    encrypted = Path(str(path) + ".age")
    assert answer["encrypted_path"] == _posix(encrypted)
    assert answer["encrypted_size_bytes"] == encrypted.stat().st_size
    assert answer["encrypted_sha256"] == _sha(encrypted.read_bytes())
    assert path.read_bytes() == PLAIN, "the plain file goes only after the result is proven"
    assert pyrage.decrypt(encrypted.read_bytes(), [pyrage.x25519.Identity.from_str(identity)]) == PLAIN
    assert not list(path.parent.glob("*.partial"))


@needs_sh
@pytest.mark.parametrize("past", [timedelta(0), timedelta(seconds=1), timedelta(days=1)])
def test_a_key_at_or_past_its_end_never_encrypts_and_the_host_is_not_reached(repo, monkeypatch, past):
    _identity, recipient = V.new_identity()
    path = _plain(repo)
    monkeypatch.setattr(repoencrypt, "run", lambda *a, **k: pytest.fail("the host was reached"))
    end = T0 + timedelta(days=90)
    with pytest.raises(repoencrypt.RepoEncryptError, match="only decrypts"):
        repoencrypt.encrypt_file(_encrypt_request(path, repo, recipient, key_expires_at=K.time_text(end)),
                                 now=lambda: end + past)
    assert not Path(str(path) + ".age").exists()


@needs_sh
def test_a_key_one_second_before_its_end_still_encrypts(repo):
    _identity, recipient = V.new_identity()
    path = _plain(repo)
    end = T0 + timedelta(days=90)
    answer = repoencrypt.encrypt_file(_encrypt_request(path, repo, recipient, key_expires_at=K.time_text(end)),
                                      now=lambda: end - timedelta(seconds=1))
    assert Path(_local(answer["encrypted_path"])).exists()


@needs_sh
@pytest.mark.parametrize("change, words", [
    ({"size_bytes": len(PLAIN) + 1}, "not the file the catalog proved"),
    ({"sha256": "0" * 64}, "not the file the catalog proved"),
])
def test_a_plain_file_that_is_not_the_one_proved_is_never_encrypted(repo, change, words):
    _identity, recipient = V.new_identity()
    path = _plain(repo)
    with pytest.raises(repoencrypt.RepoEncryptError, match=words):
        repoencrypt.encrypt_file(_encrypt_request(path, repo, recipient, **change), now=lambda: T0)
    assert not Path(str(path) + ".age").exists()


@needs_sh
def test_an_age_file_already_there_is_never_replaced(repo):
    _identity, recipient = V.new_identity()
    path = _plain(repo)
    existing = Path(str(path) + ".age")
    existing.write_bytes(b"an earlier encrypted copy")
    with pytest.raises(repoencrypt.RepoEncryptError, match="never replaced"):
        repoencrypt.encrypt_file(_encrypt_request(path, repo, recipient), now=lambda: T0)
    assert existing.read_bytes() == b"an earlier encrypted copy"


@needs_sh
def test_a_repository_without_the_age_tool_encrypts_nothing_and_says_what_to_install(repo):
    _identity, recipient = V.new_identity()
    path = _plain(repo)
    repo.state["path"] = ""          # PATH without age
    with pytest.raises(repoencrypt.RepoEncryptError, match="no `age` tool"):
        repoencrypt.encrypt_file(_encrypt_request(path, repo, recipient), now=lambda: T0)
    assert not Path(str(path) + ".age").exists()


@needs_sh
def test_a_missing_file_is_refused(repo):
    _identity, recipient = V.new_identity()
    path = _plain(repo)
    request = _encrypt_request(path, repo, recipient)
    path.unlink()
    with pytest.raises(repoencrypt.RepoEncryptError, match="not there"):
        repoencrypt.encrypt_file(request, now=lambda: T0)


@pytest.mark.parametrize("change, words", [
    ({"path": "/elsewhere/a.bak", "file_name": "a.bak"}, "not under must_be_under"),
    ({"file_name": "other.bak"}, "last part of path"),
    ({"recipients": []}, "recipients is required"),
    ({"recipients": ["AGE-SECRET-KEY-1" + "Q" * 58]}, "not an age public key"),
    ({"key_id": 0}, "key_id"),
    ({"key_expires_at": "tomorrow"}, "UTC time"),
    ({"paths": ["/r/a"]}, "ONE file per command"),
    ({"overwrite": True}, "unknown field"),
    ({"host": {"runtime": "windows"}}, "Linux repository host"),
    ({"path": "/r/S/../a.bak", "file_name": "a.bak"}, "'..'"),
    ({"path": "/r/S/*.bak", "file_name": "*.bak"}, "pattern"),
])
def test_a_request_that_does_not_name_one_proven_file_and_a_live_key_is_refused_before_the_host(monkeypatch, change, words):
    monkeypatch.setattr(repoencrypt, "run", lambda *a, **k: pytest.fail("the host was reached"))
    request = {"host": {"runtime": "linux", "host": "192.0.2.30"}, "path": "/r/S/a.bak", "file_name": "a.bak",
               "size_bytes": 1, "sha256": "0" * 64, "must_be_under": "/r", "recipients": ["age1" + "q" * 58],
               "key_id": 1, "key_expires_at": "2099-01-01T00:00:00Z", **change}
    with pytest.raises((repoencrypt.RepoEncryptError, K.RepositoryKeyError, Exception), match=words):
        repoencrypt.encrypt_file(request, now=lambda: T0)


def test_an_age_file_is_never_encrypted_twice(monkeypatch):
    monkeypatch.setattr(repoencrypt, "run", lambda *a, **k: pytest.fail("the host was reached"))
    with pytest.raises(repoencrypt.RepoEncryptError, match="never encrypted twice"):
        repoencrypt.encrypt_file({"host": {"runtime": "linux", "host": "h"}, "path": "/r/a.bak.age",
                                  "file_name": "a.bak.age", "size_bytes": 1, "sha256": "0" * 64,
                                  "must_be_under": "/r", "recipients": ["age1" + "q" * 58], "key_id": 1,
                                  "key_expires_at": "2099-01-01T00:00:00Z"}, now=lambda: T0)


# --------------------------------------------------------------------------- #
# verify-encrypted-file: the right key, the old key, a wrong key, no key, no age, damage
# --------------------------------------------------------------------------- #
def _sealed(recipients, data=PLAIN):
    return pyrage.encrypt(data, [pyrage.x25519.Recipient.from_str(r) for r in recipients])


def _verify(encrypted: bytes, identity, **over):
    request = {"host": {"host": "192.0.2.30"}, "path": "/r/a.bak.age", "size_bytes": len(encrypted),
               "sha256": _sha(encrypted), "plain_size_bytes": len(PLAIN), "plain_sha256": _sha(PLAIN), "transfer": "cat",
               "identity": identity, **over}
    import io
    return repoencrypt.verify_encrypted_file(request, open_reader=lambda _h, _p: io.BytesIO(encrypted))


def test_the_right_key_proves_the_file_against_both_sets_of_facts():
    identity, recipient = V.new_identity()
    assert _verify(_sealed([recipient]), identity)["proven"] is True


def test_an_expired_key_still_decrypts_what_it_encrypted():
    """Expiry is about encrypting; a key 4000 days past its end opens its files as on day one."""
    identity, recipient = V.new_identity()
    encrypted = _sealed([recipient])
    end = T0 + timedelta(days=90)
    assert not K.may_encrypt(end, end + timedelta(days=4000))
    assert _verify(encrypted, identity)["proven"] is True


def test_a_newer_key_cannot_open_a_file_an_old_key_encrypted():
    _old, old_public = V.new_identity()
    new_identity, _ = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match="cannot be decrypted with this key"):
        _verify(_sealed([old_public]), new_identity)


def test_a_wrong_key_is_refused():
    _right, recipient = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match="cannot be decrypted with this key"):
        _verify(_sealed([recipient]), V.new_identity()[0])


@pytest.mark.parametrize("identity", ["", None, "AGE-SECRET-KEY-1SHORT", "age1" + "q" * 58])
def test_no_key_or_a_key_that_is_not_one_is_refused_before_anything_is_read(identity):
    with pytest.raises(repoencrypt.RepoEncryptError, match="identity is required"):
        repoencrypt.verify_encrypted_file(
            {"host": {"host": "h"}, "path": "/r/a.bak.age", "size_bytes": 1, "sha256": "0" * 64,
             "plain_size_bytes": 1, "plain_sha256": "0" * 64, "identity": identity, "transfer": "cat"},
            open_reader=lambda *_a: pytest.fail("the file was read"))


@pytest.mark.parametrize("damage", ["flipped", "truncated", "empty", "plain"])
def test_a_damaged_or_plain_file_is_never_proven(damage):
    identity, recipient = V.new_identity()
    good = _sealed([recipient])
    bad = {"flipped": good[:-40] + bytes([good[-40] ^ 1]) + good[-39:], "truncated": good[: len(good) // 2],
           "empty": b"", "plain": PLAIN}[damage]
    with pytest.raises(repoencrypt.RepoEncryptError):
        _verify(bad, identity, size_bytes=len(bad), sha256=_sha(bad))


def test_a_verify_that_does_not_state_its_transfer_is_refused():
    identity, recipient = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match='"transfer" is required'):
        _verify(_sealed([recipient]), identity, transfer=None)


def test_a_file_that_decrypts_to_other_bytes_is_not_proven():
    identity, recipient = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match="not the plain file's"):
        _verify(_sealed([recipient], data=PLAIN + b"x"), identity)


def test_an_encrypted_file_other_than_the_one_measured_is_not_proven():
    identity, recipient = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match="not the .* measured when it was written"):
        _verify(_sealed([recipient]), identity, sha256="0" * 64)


def test_without_the_age_library_nothing_is_decrypted_and_it_says_what_to_install(monkeypatch):
    identity, recipient = V.new_identity()
    encrypted = _sealed([recipient])
    monkeypatch.setitem(sys.modules, "pyrage", None)
    with pytest.raises(K.RepositoryKeyError, match="pip install pyrage"):
        _verify(encrypted, identity)


# --------------------------------------------------------------------------- #
# The pass, end to end on a real store: keys made, sealed, rotated; files encrypted, proven, dropped
# --------------------------------------------------------------------------- #
@pytest.fixture
def world(tmp_path, repo, monkeypatch):
    owner, _ = V.new_identity()
    recovery, recovery_public = V.new_identity()
    store_config = tmp_path / "store_config.json"
    store_config.write_text(json.dumps({"backend": "sqlite", V.BLOCK: {
        "file_folder": str(tmp_path / "vault"), "identity_ref": "VAULT_ID",
        "recovery_recipients": [recovery_public]}}), encoding="utf-8")
    app_config = SimpleNamespace(store=SimpleNamespace(config_file=store_config))
    store = RepositoryEncryptionStore(tmp_path / "store.sqlite")

    def run(command, request, **_k):
        if command == "encrypt-file":
            world.encrypted += 1
            return repoencrypt.encrypt_file(request, now=lambda: world.now)
        if command == "verify-encrypted-file":
            return repoencrypt.verify_encrypted_file(request, open_reader=lambda _h, p: open(_local(p), "rb"))
        if command == "list-files":
            folder = _local(request["folder"])
            return {"folder": request["folder"], "folder_found": folder.exists(),
                    "files": [{"path": _posix(f), "type": "file", "size_bytes": f.stat().st_size}
                              for f in sorted(folder.rglob("*")) if f.is_file()] if folder.exists() else []}
        if command == "file-checksum":
            found = [_local(p) for p in request["paths"]]
            return {"files": [{"path": p, "size_bytes": f.stat().st_size, "sha256": _sha(f.read_bytes())}
                              for p, f in zip(request["paths"], found) if f.exists()],
                    "missing": [p for p, f in zip(request["paths"], found) if not f.exists()]}
        raise AssertionError(command)

    def run_allowing_failure(command, request, **_k):
        if command == "verify-encrypted-file":
            try:
                return True, run(command, request), ""
            except repoencrypt.RepoEncryptError as exc:
                return False, {}, str(exc)
        assert command == "delete-verified-file" and "paths" not in request
        answer = deleteverified.delete_verified_file(request)
        return True, answer, ""

    monkeypatch.setattr(E.common_cli, "run", run)
    monkeypatch.setattr(E.common_cli, "run_allowing_failure", run_allowing_failure)
    slept: list[float] = []
    monkeypatch.setattr(E, "_sleep", lambda seconds: slept.append(seconds) or (world.between() if world.between else None))
    world = SimpleNamespace(slept=slept, between=None, store=store, app_config=app_config, secrets={"VAULT_ID": owner}, recovery=recovery,
                            now=T0, repo=repo, vault=tmp_path / "vault", encrypted=0)
    return world


def _entry(world, days=90):
    return SimpleNamespace(backup_id="B1", server_id="SRV-01", offload=SimpleNamespace(
        folder=_posix(world.repo.root), server_id="REPO", encrypt=True, encrypt_key_days=days,
        hash_timeout_seconds=60, transfer="cat"))


def _row(world, file_id=1, name="APPDB_FULL_20261110.bak", data=PLAIN):
    path = _plain(world.repo, name, data)
    return {"file_id": file_id, "file_name": name, "file_type": "FULL", "offload_path": _posix(path),
            "size_bytes": len(data), "checksum_after_backup": _sha(data), "offload_state": "verified"}, path


def _pass(world, rows, days=90):
    hosts = SimpleNamespace(repository_host={"runtime": "linux", "host": "192.0.2.30"},
                            target={"host": "192.0.2.30"})
    return E.encrypt_rows(_entry(world, days), app_config=world.app_config, store=world.store, hosts=hosts,
                          rows=rows, secrets=world.secrets, passphrase="store passphrase", now=world.now)


@needs_sh
def test_a_pass_encrypts_proves_and_only_then_drops_the_plain_file(world):
    row, path = _row(world)

    answer = _pass(world, [row])

    assert answer["encrypted"] == 1 and answer["files"] == 1 and not answer["failed"]
    assert not path.exists(), "the plain file goes once the encrypted one is proven"
    recorded = world.store.files_of(1)[0]
    assert recorded["plain_sha256"] == _sha(PLAIN) and recorded["proven_at"] and recorded["plain_deleted_at"]
    key = world.store.key(recorded["key_id"])
    assert key["encrypt_until"] == K.time_text(T0 + timedelta(days=90)) and key["proven_at"]
    # The stored key opens the file; so does the recovery key, through the key's sealed file.
    encrypted = Path(str(path) + ".age").read_bytes()
    identity = world.store.identity(key["key_id"], passphrase="store passphrase")
    assert pyrage.decrypt(encrypted, [pyrage.x25519.Identity.from_str(identity)]) == PLAIN
    sealed = V.unseal(Path(key["sealed_file"]).read_bytes(), world.recovery)
    assert sealed["identity"] == identity and sealed["recipient"] == key["recipient"]


@needs_sh
def test_the_key_rotates_at_its_end_and_the_old_one_stays_for_decrypting(world):
    first, _ = _row(world, 1, "A_FULL_1.bak", PLAIN)
    _pass(world, [first])
    key1 = world.store.files_of(1)[0]["key_id"]

    world.now = T0 + timedelta(days=90) - timedelta(seconds=1)       # the last second of key 1
    second, _ = _row(world, 2, "A_FULL_2.bak", PLAIN + b"2")
    _pass(world, [second])
    assert world.store.files_of(2)[0]["key_id"] == key1

    world.now = T0 + timedelta(days=90)                               # its end: key 1 never again
    third, third_path = _row(world, 3, "A_FULL_3.bak", PLAIN + b"3")
    _pass(world, [third])
    key2 = world.store.files_of(3)[0]["key_id"]
    assert key2 != key1
    assert len(world.store.keys(server_id="SRV-01")) == 2, "the old key is kept"
    # Each file opens with its own key only.
    old_identity = world.store.identity(key1, passphrase="store passphrase")
    new_identity = world.store.identity(key2, passphrase="store passphrase")
    third_age = Path(str(third_path) + ".age").read_bytes()
    assert pyrage.decrypt(third_age, [pyrage.x25519.Identity.from_str(new_identity)]) == PLAIN + b"3"
    with pytest.raises(pyrage.DecryptError):
        pyrage.decrypt(third_age, [pyrage.x25519.Identity.from_str(old_identity)])


@needs_sh
def test_without_the_vault_files_configured_no_key_is_used_and_nothing_is_encrypted(world, tmp_path):
    world.app_config.store.config_file.write_text(json.dumps({"backend": "sqlite"}), encoding="utf-8")
    row, path = _row(world)
    with pytest.raises(Exception, match="not configured"):
        _pass(world, [row])
    assert path.exists() and not Path(str(path) + ".age").exists()
    assert all(not k["proven_at"] for k in world.store.keys(server_id="SRV-01"))


@needs_sh
def test_a_file_whose_proof_fails_keeps_its_plain_copy(world, monkeypatch):
    row, path = _row(world)
    real = E.common_cli.run

    def refuse(command, request, **_k):
        if command == "verify-encrypted-file":
            raise repoencrypt.RepoEncryptError("not proven")
        return real(command, request)
    monkeypatch.setattr(E.common_cli, "run", refuse)

    answer = _pass(world, [row])

    assert answer["encrypted"] == 0 and "not proven" in answer["failed"][0]
    assert path.read_bytes() == PLAIN
    assert world.store.files_of(1)[0]["proven_at"] is None


@needs_sh
def test_a_pass_after_a_failed_one_proves_and_finishes_without_encrypting_again(world, monkeypatch):
    row, path = _row(world)
    real = E.common_cli.run

    def once_refused(command, request, **_k):
        if command == "verify-encrypted-file":
            raise repoencrypt.RepoEncryptError("network dropped")
        return real(command, request)
    monkeypatch.setattr(E.common_cli, "run", once_refused)
    _pass(world, [row])
    monkeypatch.setattr(E.common_cli, "run", real)

    answer = _pass(world, [row])

    assert answer["encrypted"] == 1 and not path.exists()
    assert len(world.store.files_of(1)) == 1


@needs_sh
def test_a_row_already_done_is_not_touched_again(world):
    row, _ = _row(world)
    _pass(world, [row])
    assert _pass(world, [row])["rows"] == 0


# --------------------------------------------------------------------------- #
# The source is released once its copy is wholly encrypted and proven (the operator: *once encrypted ok,
# delete the old file - on the server_id too*), its .age files proven again first
# --------------------------------------------------------------------------- #
AGE_SHA = "a" * 64


def _age(file_id=7, path="/nas/SRV-01/APPDB/FULL/a.bak.age", sha=AGE_SHA, size=4321):
    return {"file_id": file_id, "encrypted_path": path, "encrypted_sha256": sha, "encrypted_size_bytes": size,
            "proven_at": "2026-11-10T06:00:00Z", "plain_deleted_at": "2026-11-10T06:00:01Z"}


class _ReleaseCatalog:
    def __init__(self, newest="2099-01-01T00:00:00Z"):
        self.newest, self.set = newest, []

    def newest_full_per_database(self, **_k):
        return [{"database_name": "APPDB", "at": self.newest}]

    def set_backup_file(self, file_id, **values):
        self.set.append((file_id, values))

    def check_backup_file(self, *_a, **_k):
        pytest.fail("an encrypted copy is never re-checked as a plain one")


def _release(monkeypatch, *, found, encrypted=frozenset({7}), sealed=None, catalog=None):
    from dbabrain.backup_restore import offload

    sent, hashed = [], []
    monkeypatch.setattr(offload, "release_request", lambda _e, row, _h: {"path": f"/b/{row['file_name']}"})

    def run_allowing_failure(command, request, **_k):
        assert command == "delete-verified-file"
        sent.append(request)
        return True, {"file": {"status": "deleted"}}, ""

    def hasher(_host):
        def hash_paths(paths):
            hashed.extend(paths)
            return {p: v for p, v in found.items() if p in paths}
        return hash_paths

    monkeypatch.setattr(offload.common_cli, "run_allowing_failure", run_allowing_failure)
    hosts = SimpleNamespace(hasher=hasher, repository_host={})
    entry = SimpleNamespace(backup_id="B1", offload=SimpleNamespace(hash_timeout_seconds=60))
    catalog = catalog or _ReleaseCatalog()
    answer = offload.release_sources(entry, catalog=catalog, hosts=hosts, encrypted=encrypted, sealed=sealed, rows=[
        {"file_id": 7, "file_name": "a.bak", "file_type": "FULL", "database_name": "APPDB",
         "offload_path": "/nas/SRV-01/APPDB/FULL/a.bak", "backup_ended_at": "2026-01-01T00:00:00Z"}])
    return answer, sent, hashed, catalog


def test_a_wholly_encrypted_and_proven_copy_releases_its_source_after_its_age_file_is_proven_again(monkeypatch):
    age = _age()
    answer, sent, hashed, catalog = _release(monkeypatch, sealed={7: [age]},
                                             found={age["encrypted_path"]: (AGE_SHA, 4321)})

    assert answer == {"deleted": 1, "kept": 0, "refused": []}
    assert hashed == [age["encrypted_path"]], "the .age file is proven again - the plain copy is gone"
    assert sent == [{"path": "/b/a.bak"}], "one file per command, its own facts"
    assert catalog.set and catalog.set[0][1]["state"] == "pruned" and catalog.set[0][1]["source_deleted_at"]


@pytest.mark.parametrize("found, words", [
    ({"/nas/SRV-01/APPDB/FULL/a.bak.age": ("b" * 64, 4321)}, "not the bytes"),
    ({"/nas/SRV-01/APPDB/FULL/a.bak.age": (AGE_SHA, 4320)}, "not the bytes"),
    ({}, "not the bytes"),
])
def test_an_encrypted_copy_of_other_bytes_or_gone_keeps_the_source(monkeypatch, found, words):
    answer, sent, _, catalog = _release(monkeypatch, sealed={7: [_age()]}, found=found)

    assert answer["deleted"] == 0 and sent == []
    assert len(answer["refused"]) == 1 and words in answer["refused"][0] and "source is kept" in answer["refused"][0]
    assert catalog.set == [], "the plain copy is not marked failed: there is no plain copy to copy again"


def test_a_copy_of_several_age_files_releases_only_when_every_one_is_proven(monkeypatch):
    one, two = _age(path="/nas/x/base.tar.zst.age"), _age(path="/nas/x/pg_wal.tar.age", sha="c" * 64, size=9)
    answer, sent, _, _ = _release(monkeypatch, sealed={7: [one, two]},
                                  found={one["encrypted_path"]: (AGE_SHA, 4321)})
    assert answer["deleted"] == 0 and sent == [] and "pg_wal.tar.age" in answer["refused"][0]


def test_a_copy_encrypted_in_part_or_not_yet_proven_keeps_the_source(monkeypatch):
    answer, sent, hashed, _ = _release(monkeypatch, sealed={}, found={})
    assert answer == {"deleted": 0, "kept": 1, "refused": []} and sent == [] and hashed == []


def test_an_encrypted_copy_in_the_newest_chain_is_still_kept(monkeypatch):
    answer, sent, _, _ = _release(monkeypatch, sealed={7: [_age()]}, found={},
                                  catalog=_ReleaseCatalog(newest="2025-01-01T00:00:00Z"))
    assert answer["deleted"] == 0 and answer["kept"] == 1 and sent == []


def _record(store, file_id, path, *, proven=True, plain_gone=True):
    made = store.record_encrypted(
        file_id=file_id, backup_id="B1", server_id="SRV-01", repository_server_id="REPO", plain_path=path,
        plain_size_bytes=10, plain_sha256="d" * 64, encrypted_path=path + ".age", encrypted_size_bytes=20,
        encrypted_sha256="e" * 64, key_id=1, age_version="", encrypted_at="2026-11-10T06:00:00Z")
    if proven:
        store.mark_proven(made["encrypted_file_id"])
    if proven and plain_gone:
        store.mark_plain_deleted(made["encrypted_file_id"])


def test_only_a_row_whose_every_file_is_proven_and_its_plain_file_gone_is_sealed(tmp_path):
    store = RepositoryEncryptionStore(tmp_path / "store.sqlite")
    _record(store, 1, "/nas/a.bak")                                  # done, proven, plain gone
    store.mark_row_done(file_id=1, backup_id="B1", repository_server_id="REPO", encrypted_files=1, key_id=1)
    _record(store, 2, "/nas/b.bak", proven=False)                    # encrypted, not proven
    store.mark_row_done(file_id=2, backup_id="B1", repository_server_id="REPO", encrypted_files=1, key_id=1)
    _record(store, 3, "/nas/c.bak", plain_gone=False)                # proven, plain still there
    store.mark_row_done(file_id=3, backup_id="B1", repository_server_id="REPO", encrypted_files=1, key_id=1)
    _record(store, 4, "/nas/d/base.tar")                             # one of two files, row not done
    _record(store, 5, "/nas/e/base.tar")                             # done says 2 files; 1 recorded
    store.mark_row_done(file_id=5, backup_id="B1", repository_server_id="REPO", encrypted_files=2, key_id=1)

    sealed = E.sealed_rows(store, backup_id="B1")

    assert set(sealed) == {1}
    assert [f["encrypted_path"] for f in sealed[1]] == ["/nas/a.bak.age"]
    assert E.is_encrypted(store, backup_id="B1") == {1, 2, 3, 4, 5}, "every other row is held at its source"


@needs_sh
def test_a_pass_seals_the_row_it_encrypts(world):
    row, _ = _row(world)
    _pass(world, [row])
    assert set(E.sealed_rows(world.store, backup_id="B1")) == {1}


# --------------------------------------------------------------------------- #
# An encryption killed after its .age was in place, before the store recorded it (1.331 b)
# --------------------------------------------------------------------------- #
@needs_sh
def test_an_age_left_by_a_killed_encryption_is_adopted_once_it_decrypts_to_the_catalogs_file(world):
    row, path = _row(world)
    _pass(world, [_row(world, 9, "OTHER_FULL.bak", PLAIN + b"x")[0]])       # makes this pass's key
    key = world.store.keys(server_id="SRV-01")[0]
    # What a killed pass leaves: the .age moved into place, nothing recorded.
    repoencrypt.encrypt_file(_encrypt_request(path, world.repo, key["recipient"], key_id=int(key["key_id"]),
                                              key_expires_at=key["encrypt_until"]), now=lambda: world.now)
    before = world.encrypted

    answer = _pass(world, [row])

    assert answer["encrypted"] == 1 and not answer["failed"], answer
    assert world.encrypted == before, "encrypt-file is not run again: it would refuse the .age as EXISTS"
    recorded = world.store.files_of(1)[0]
    assert recorded["proven_at"] and recorded["plain_deleted_at"] and not path.exists()


@needs_sh
def test_an_age_that_does_not_decrypt_to_the_catalogs_file_is_never_adopted_or_deleted(world):
    row, path = _row(world)
    _pass(world, [_row(world, 9, "OTHER_FULL.bak", PLAIN + b"x")[0]])
    stray = Path(str(path) + ".age")
    stray.write_bytes(b"not an age file of this backup")

    answer = _pass(world, [row])

    assert answer["encrypted"] == 0 and "left as it is for the operator" in answer["failed"][0]
    assert stray.read_bytes() == b"not an age file of this backup" and path.read_bytes() == PLAIN
    assert world.store.files_of(1) == []


# --------------------------------------------------------------------------- #
# A file an earlier pass is still encrypting on the repository (1.331 c, the operator 2026-11-11: wait)
# --------------------------------------------------------------------------- #
@needs_sh
def test_a_file_whose_partial_still_grows_waits_and_is_not_encrypted_a_second_time(world):
    row, path = _row(world)
    partial = Path(str(path) + ".age.4242.partial")
    partial.write_bytes(b"x" * 10)
    world.between = lambda: partial.write_bytes(b"x" * 20)            # the earlier pass still writes

    answer = _pass(world, [row])

    assert answer["encrypted"] == 0 and not answer["failed"], "waiting is not a failure"
    assert "still being encrypted" in answer["waiting"][0] and world.encrypted == 0
    assert world.slept == [E.PARTIAL_SETTLE_SECONDS] and path.read_bytes() == PLAIN


@needs_sh
def test_a_partial_that_stopped_growing_is_a_dead_one_the_pass_encrypts_and_leaves_it_for_the_check(world):
    row, path = _row(world)
    partial = Path(str(path) + ".age.4242.partial")
    partial.write_bytes(b"x" * 10)

    answer = _pass(world, [row])

    assert answer["encrypted"] == 1 and not answer["waiting"] and not answer["failed"]
    assert partial.read_bytes() == b"x" * 10, "an unknown file on the repository is never deleted here"


@needs_sh
def test_no_partial_costs_one_listing_and_no_wait(world):
    row, _path = _row(world)
    _pass(world, [row])
    assert world.slept == []


def test_a_waiting_step_reads_as_waiting_in_the_flow_message_not_as_an_error():
    from dbabrain.backup_restore import offload as O

    entry = SimpleNamespace(backup_id="B1", server_id="SRV", offload=SimpleNamespace(
        server_id="REPO", encrypt=True, delete_source=False, repository_check=False))
    flow = O.offload_flow_lines(entry, {"copy": {"files": 1, "verified": 1, "failed": []},
                                        "encrypt": {"rows": 1, "encrypted": 0, "failed": [],
                                                    "waiting": ["A.bak: still being encrypted"]}},
                                error="", status="done")
    assert flow["flow_lines"][1].startswith("⏳ 3. Encrypt: waiting - A.bak")
