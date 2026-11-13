"""An encrypted repository copy is restored decrypted, and only when both sets of facts hold (0.37.0).

Why it matters: the operator, 2026-11-10 - *the way back: from the repository to a VM, then
unpack*. A backup kept only as ``.age`` on the repository is worth exactly what can be restored from
it. So ``fetch-decrypted-file`` streams the ``.age`` back, decrypts it on the way with the key it was
encrypted for - an expired key still decrypts - and lays the plaintext down only when the ``.age`` is
the bytes and sha256 measured when it was written AND the plaintext is the plain file's; anything
else leaves nothing under the name. A restore from the repository decrypts the chain this way, and
refuses a file on the repository no offload recorded. Real age (pyrage), fake hosts.
"""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pytest

pyrage = pytest.importorskip("pyrage")

from dbabrain.common import repoencrypt  # noqa: E402
from dbabrain.lib import vault_file as V  # noqa: E402

PLAIN = bytes(range(256)) * 500 + b"end"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sealed(recipient: str, data: bytes = PLAIN) -> bytes:
    return pyrage.encrypt(data, [pyrage.x25519.Recipient.from_str(recipient)])


class _Sftp:
    def __init__(self, files=None):
        self.files = dict(files or {})

    def stat(self, path):
        if path not in self.files:
            raise IOError(path)

    def mkdir(self, path):
        pass

    def open(self, path, mode):
        return _Writer(self, path)

    def rename(self, a, b):
        if b in self.files:
            raise IOError("exists")
        self.files[b] = self.files.pop(a)

    def posix_rename(self, a, b):
        self.files[b] = self.files.pop(a)

    def remove(self, path):
        self.files.pop(path, None)


class _Writer:
    def __init__(self, sftp, path):
        self.sftp, self.path, self.data = sftp, path, b""

    def write(self, data):
        self.data += data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.sftp.files[self.path] = self.data


def _fetch(encrypted: bytes, identity, *, target=None, **over):
    target = target if target is not None else _Sftp()
    request = {"source": {"host": "repo"}, "path": "/r/S/base/X_FULL/base.tar.zst.age",
               "size_bytes": len(encrypted), "sha256": _sha(encrypted),
               "target": {"host": "vm"}, "target_path": "/stage/base/X_FULL/base.tar.zst",
               "file_name": "base.tar.zst", "plain_size_bytes": len(PLAIN), "plain_sha256": _sha(PLAIN),
               "identity": identity, "transfer": "sftp", "overwrite": False, **over}
    answer = repoencrypt.fetch_decrypted_file(
        request, open_reader=lambda _h, _p: io.BytesIO(encrypted),
        open_target=lambda _login: SimpleNamespace(sftp=lambda: target, close=lambda: None))
    return answer, target


def test_the_file_is_laid_down_decrypted_under_its_plain_name():
    identity, recipient = V.new_identity()
    answer, target = _fetch(_sealed(recipient), identity)
    assert answer["restored"] is True and target.files["/stage/base/X_FULL/base.tar.zst"] == PLAIN
    assert not any(p.endswith(".dbabrain_partial") for p in target.files)


def test_an_expired_key_still_restores_what_it_encrypted():
    """Expiry is about encrypting; the restore needs the key the file was encrypted for, however old."""
    identity, recipient = V.new_identity()
    assert _fetch(_sealed(recipient), identity)[0]["restored"]


def test_a_wrong_key_restores_nothing():
    _right, recipient = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match="cannot be decrypted"):
        _fetch(_sealed(recipient), V.new_identity()[0])


@pytest.mark.parametrize("identity", ["", None, "AGE-SECRET-KEY-1SHORT"])
def test_no_key_restores_nothing(identity):
    with pytest.raises(repoencrypt.RepoEncryptError, match="identity is required"):
        _fetch(b"x", identity)


def test_a_damaged_age_file_restores_nothing():
    identity, recipient = V.new_identity()
    good = _sealed(recipient)
    bad = good[:-30] + bytes([good[-30] ^ 1]) + good[-29:]
    target = _Sftp()
    with pytest.raises(repoencrypt.RepoEncryptError):
        _fetch(bad, identity, target=target, size_bytes=len(bad), sha256=_sha(bad))
    assert "/stage/base/X_FULL/base.tar.zst" not in target.files


def test_plaintext_other_than_the_backup_wrote_never_takes_the_name():
    identity, recipient = V.new_identity()
    target = _Sftp()
    with pytest.raises(repoencrypt.RepoEncryptError, match="not the plain file's"):
        _fetch(_sealed(recipient, PLAIN + b"!"), identity, target=target,
               size_bytes=len(_sealed(recipient, PLAIN + b"!")))
    assert "/stage/base/X_FULL/base.tar.zst" not in target.files


def test_an_age_file_other_than_the_one_measured_never_takes_the_name():
    identity, recipient = V.new_identity()
    target = _Sftp()
    with pytest.raises(repoencrypt.RepoEncryptError, match="measured when it was written"):
        _fetch(_sealed(recipient), identity, target=target, sha256="0" * 64)
    assert "/stage/base/X_FULL/base.tar.zst" not in target.files


def test_a_file_already_at_the_name_is_left_when_overwrite_is_false():
    identity, recipient = V.new_identity()
    target = _Sftp({"/stage/base/X_FULL/base.tar.zst": b"an earlier one"})
    with pytest.raises(repoencrypt.RepoEncryptError, match="left as it is"):
        _fetch(_sealed(recipient), identity, target=target)
    assert target.files["/stage/base/X_FULL/base.tar.zst"] == b"an earlier one"
    answer, target = _fetch(_sealed(recipient), identity, target=target, overwrite=True)
    assert target.files["/stage/base/X_FULL/base.tar.zst"] == PLAIN


@pytest.mark.parametrize("change, words", [
    ({"file_name": "other.bak"}, "file_name must be the last part"),
    ({"path": "/r/S/base/X_FULL/base.tar.zst"}, "not an age file"),
    ({"overwrite": None}, "overwrite"),
    ({"transfer": None}, "transfer"),
    ({"paths": ["/r/a"]}, "ONE file per command"),
])
def test_a_request_that_does_not_state_one_file_in_full_is_refused(change, words):
    identity, recipient = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match=words):
        _fetch(_sealed(recipient), identity, **change)


# --------------------------------------------------------------------------- #
# A restore from the repository decrypts the chain - and refuses a file nobody recorded
# --------------------------------------------------------------------------- #
@pytest.fixture
def staging(monkeypatch, tmp_path):
    from dbabrain.backup_restore import restore_script as R
    from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore

    store = RepositoryEncryptionStore(tmp_path / "store.sqlite")
    calls = []

    def run(command, request, **_k):
        calls.append((command, request))
        if command == "prepare-staging":
            # Which files the chain is, read from the repository alone: no target is opened for it.
            assert request["plan_only"] is True
            return {"files": [{"path": p} for p in staging_files]}
        if command == "fetch-decrypted-file":
            return {"target_path": request["target_path"], "plain_size_bytes": request["plain_size_bytes"],
                    "plain_sha256": request["plain_sha256"]}
        raise AssertionError(command)

    staging_files = ["base/X_FULL/base.tar.zst.age", "base/X_FULL/backup_manifest.age"]
    monkeypatch.setattr(R.common_cli, "run", run)
    monkeypatch.setattr("dbabrain.lib.config.load_config", lambda *a, **k: None)
    monkeypatch.setattr(RepositoryEncryptionStore, "from_config", classmethod(lambda cls, _c: store))
    monkeypatch.setattr(R, "_transfer_of", lambda job: "cat")
    identity, recipient = V.new_identity()
    key = store.add_key(server_id="SRC", identity=identity, recipient=recipient, created_at="2026-01-01T00:00:00Z",
                        days=90, passphrase="pw", created_by="t")
    for name in staging_files:
        row = store.record_encrypted(file_id=1, backup_id="B", server_id="SRC", repository_server_id="REPO",
                                     plain_path=f"/r/S/{name[:-4]}", plain_size_bytes=10, plain_sha256="a" * 64,
                                     encrypted_path=f"/r/S/{name}", encrypted_size_bytes=20, encrypted_sha256="b" * 64,
                                     key_id=key["key_id"], age_version="1.2.1", encrypted_at="2026-01-02T00:00:00Z")
        store.mark_proven(row["encrypted_file_id"])
    job = SimpleNamespace(label="R1", target_backup_dir="/stage", server_id="SRC")
    return SimpleNamespace(R=R, store=store, calls=calls, files=staging_files, job=job, identity=identity)


def _stage(s, monkeypatch, **more):
    monkeypatch.setenv("DBABRAIN_SECRET_KEY", "pw")
    return s.R._stage_decrypted(s.job, {"source_dir": "/r/S"}, source=SimpleNamespace(server_id="REPO"),
                                source_login={"host": "repo"}, target_login={"host": "vm"}, key=None, key_base64=None,
                                **more)


def test_a_chain_of_encrypted_files_is_fetched_decrypted_into_the_same_layout(staging, monkeypatch):
    answer = _stage(staging, monkeypatch)
    assert [c for c, _r in staging.calls][0] == "prepare-staging", "the chain's files, asked of the repository first"
    fetched = [r for c, r in staging.calls if c == "fetch-decrypted-file"]
    assert [r["target_path"] for r in fetched] == ["/stage/base/X_FULL/base.tar.zst", "/stage/base/X_FULL/backup_manifest"]
    assert all(r["identity"] == staging.identity and r["transfer"] == "cat" and r["overwrite"] is True for r in fetched)
    assert answer["files_decrypted"] == 2 and answer["encrypted"] is True
    # What every copy answers with - the restore's COPY_DONE phase reads these three.
    assert answer["copied"] == 2 and answer["bytes_copied"] == 20 and answer["skipped"] == 0


def test_a_file_on_the_repository_no_offload_recorded_refuses_the_restore(staging, monkeypatch):
    staging.files.append("base/X_FULL/stray.age")
    with pytest.raises(staging.R.CopyNotProvenError, match="stray.age"):
        _stage(staging, monkeypatch)
    assert not [c for c, _r in staging.calls if c == "fetch-decrypted-file"]


def test_a_chain_with_no_age_file_goes_the_usual_way(staging, monkeypatch):
    staging.files[:] = ["base/X_FULL/base.tar.zst", "base/X_FULL/backup_manifest"]
    assert _stage(staging, monkeypatch) is None


def test_a_plan_the_restore_already_read_is_not_read_from_the_repository_again(staging, monkeypatch):
    """The transfer reads the chain and its files once (0.38.0) and hands the answer on; walking the
    repository a second time for the same list is a round trip per restore for nothing."""
    plan = {"files": [{"path": path} for path in staging.files]}

    answer = _stage(staging, monkeypatch, plan=plan)

    assert answer["files_decrypted"] == 2
    assert {c for c, _r in staging.calls} == {"fetch-decrypted-file"}


# --------------------------------------------------------------------------- #
# decrypt-on-host: the target IS the repository host - age -d runs there, on a real shell
# --------------------------------------------------------------------------- #
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

WINDOWS_HERE = os.name == "nt"
BASH = (r"C:\Program Files\Git\bin\bash.exe" if WINDOWS_HERE and os.path.exists(r"C:\Program Files\Git\bin\bash.exe")
        else shutil.which("bash"))
needs_bash = pytest.mark.skipif(not BASH, reason="bash runs the host's script")


def _posix(path) -> str:
    text = str(path)
    if WINDOWS_HERE and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


@pytest.fixture
def host(tmp_path):
    """A 'host' whose commands run in local bash with an `age` that decrypts through pyrage."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    helper = bin_dir / "age_dec.py"
    helper.write_text(
        "import os, sys, pyrage\nfrom pyrage import x25519\nargs = sys.argv[1:]\n"
        "out, inp, i = None, None, 0\n"
        "while i < len(args):\n"
        "    if args[i] == '-d': i += 1\n"
        "    elif args[i] == '-o': out = args[i + 1]; i += 2\n"
        "    else: inp = args[i]; i += 1\n"
        "try:\n    data = pyrage.decrypt(open(inp, 'rb').read(), "
        "[x25519.Identity.from_str(os.environ['AGE_KEY'].strip())])\n"
        "except Exception as e:\n    sys.stderr.write('age: error: ' + str(e)); sys.exit(1)\n"
        "open(out, 'wb').write(data)\n", encoding="utf-8")
    # bash reads the identity file - a /dev/fd path a native Windows Python cannot open - and hands it on.
    (bin_dir / "age").write_text(
        "#!/bin/bash\nargs=()\nwhile [ $# -gt 0 ]; do\n"
        '  if [ "$1" = -i ]; then AGE_KEY=$(cat "$2"); export AGE_KEY; shift 2; continue; fi\n'
        '  args+=("$1"); shift\ndone\n'
        f'exec "{_posix(sys.executable)}" "{_posix(helper)}" "${{args[@]}}"\n',
        encoding="utf-8", newline="\n")
    seen = []

    class _Session:
        def run(self, command, *, timeout_seconds=None, stdin=None):
            seen.append({"command": command, "stdin": stdin})
            done = subprocess.run([BASH, "-c", f'export PATH="{_posix(bin_dir)}:$PATH"; {command}'],
                                  input=stdin, capture_output=True, text=True, timeout=120)
            return SimpleNamespace(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

        def close(self):
            pass
    return SimpleNamespace(root=tmp_path, session=_Session(), seen=seen)


def _on_host(host, encrypted: bytes, identity, **over):
    repo = host.root / "repo" / "base" / "X_FULL"
    repo.mkdir(parents=True, exist_ok=True)
    age_file = repo / "base.tar.zst.age"
    age_file.write_bytes(encrypted)
    target = host.root / "drill" / "stage" / "base" / "X_FULL" / "base.tar.zst"
    request = {"host": {"runtime": "linux", "host": "192.0.2.30"}, "path": _posix(age_file),
               "size_bytes": len(encrypted), "sha256": _sha(encrypted), "target_path": _posix(target),
               "file_name": "base.tar.zst", "must_be_under": _posix(host.root / "drill"),
               "plain_size_bytes": len(PLAIN), "plain_sha256": _sha(PLAIN), "identity": identity,
               "overwrite": False, **over}
    answer = repoencrypt.decrypt_on_host(request, open_session=lambda _h: host.session)
    return answer, target


@needs_bash
def test_on_the_host_the_file_is_decrypted_where_it_lies_and_proven(host):
    identity, recipient = V.new_identity()
    answer, target = _on_host(host, _sealed(recipient), identity)
    assert answer["restored"] and target.read_bytes() == PLAIN
    assert not list(target.parent.glob("*.dbabrain_partial"))


@needs_bash
def test_on_the_host_the_key_goes_on_stdin_never_on_the_command_line(host):
    identity, recipient = V.new_identity()
    _on_host(host, _sealed(recipient), identity)
    assert identity not in host.seen[0]["command"] and host.seen[0]["stdin"].strip() == identity


@needs_bash
def test_on_the_host_a_wrong_key_restores_nothing(host):
    _right, recipient = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match="cannot be decrypted with this key"):
        _on_host(host, _sealed(recipient), V.new_identity()[0])
    assert not (host.root / "drill").exists() or not any((host.root / "drill").rglob("base.tar.zst*"))


@needs_bash
def test_on_the_host_an_age_file_other_than_measured_is_never_decrypted(host):
    identity, recipient = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match="measured when it was encrypted"):
        _on_host(host, _sealed(recipient), identity, sha256="0" * 64)


@needs_bash
def test_on_the_host_plaintext_other_than_the_backup_wrote_never_takes_the_name(host):
    identity, recipient = V.new_identity()
    with pytest.raises(repoencrypt.RepoEncryptError, match="not the plain file's"):
        _on_host(host, _sealed(recipient), identity, plain_sha256="0" * 64)
    assert not any((host.root / "drill").rglob("base.tar.zst*"))


@needs_bash
def test_on_the_host_a_file_at_the_name_is_left_unless_overwrite(host):
    identity, recipient = V.new_identity()
    target = host.root / "drill" / "stage" / "base" / "X_FULL" / "base.tar.zst"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"an earlier one")
    with pytest.raises(repoencrypt.RepoEncryptError, match="left as it is"):
        _on_host(host, _sealed(recipient), identity)
    assert target.read_bytes() == b"an earlier one"
    _on_host(host, _sealed(recipient), identity, overwrite=True)
    assert target.read_bytes() == PLAIN


@pytest.mark.parametrize("change, words", [
    ({"target_path": "/elsewhere/base.tar.zst"}, "not under must_be_under"),
    ({"file_name": "x"}, "file_name must be the last part"),
    ({"identity": ""}, "identity is required"),
    ({"overwrite": "no"}, "overwrite"),
    ({"host": {"runtime": "windows"}}, "Linux host"),
])
def test_on_the_host_a_request_not_in_full_is_refused_before_the_host(change, words):
    request = {"host": {"runtime": "linux", "host": "h"}, "path": "/r/base.tar.zst.age", "size_bytes": 1,
               "sha256": "0" * 64, "target_path": "/d/s/base.tar.zst", "file_name": "base.tar.zst",
               "must_be_under": "/d", "plain_size_bytes": 1, "plain_sha256": "0" * 64,
               "identity": V.new_identity()[0], "overwrite": False, **change}
    with pytest.raises(repoencrypt.RepoEncryptError, match=words):
        repoencrypt.decrypt_on_host(request, open_session=lambda _h: pytest.fail("the host was reached"))


@needs_bash
def test_on_the_host_the_plain_file_already_there_with_its_facts_is_not_decrypted_again(host):
    identity, recipient = V.new_identity()
    _on_host(host, _sealed(recipient), identity)
    seen = len(host.seen)
    answer, target = _on_host(host, _sealed(recipient), identity, overwrite=True)
    assert answer["already_there"] is True and target.read_bytes() == PLAIN and len(host.seen) == seen + 1


@needs_bash
def test_on_the_host_a_plain_file_there_with_other_bytes_is_decrypted_over_when_overwrite(host):
    identity, recipient = V.new_identity()
    _answer, target = _on_host(host, _sealed(recipient), identity)
    target.write_bytes(b"x" * len(PLAIN))
    answer, target = _on_host(host, _sealed(recipient), identity, overwrite=True)
    assert answer["already_there"] is False and target.read_bytes() == PLAIN
