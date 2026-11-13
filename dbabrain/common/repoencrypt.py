"""Encrypt ONE backup file where it lies on the repository, and prove the result (0.37.0).

The operator, 2026-11-10 (translated): *one encryption for every database type, done at the shared
repository: copy there, compare checksum and bytes, then encrypt AT the repository; once the
encryption is proven, delete the old file - on the repository and on the server_id*; and *the key
has a lifetime, 90 days by default: past it, an old key can only decrypt, never encrypt*.

Two commands, each working from its request alone (R09):

``encrypt-file`` - on the host that holds the file (POSIX: the repository is a Linux host), with the
``age`` tool there, X25519 recipients only - so the repository holds public keys and can never
decrypt what it wrote:

* the plain file is held to its stated name, bytes and sha256 **before** anything is written - a
  file that is not the one the catalog proved is never encrypted;
* the key's ``key_expires_at`` is held to this machine's clock: an expired key is refused here, so a
  caller that picked one wrongly still cannot encrypt with it (it stays good for decrypting);
* written as ``<file>.age.<pid>.partial`` then moved to ``<file>.age`` with ``mv -n`` - an ``.age``
  already there is never replaced; the plain file is checked unchanged afterwards; the answer is
  the encrypted file's bytes and sha256, measured on the host.

``verify-encrypted-file`` - the ``.age`` file streamed from the host over SFTP, its bytes and sha256
measured as they pass, and DECRYPTED on the way with the key's identity (pyrage, never written to
disk): the plaintext's bytes and sha256 must be the plain file's. Only then may the plain file go.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from datetime import datetime, timezone
from typing import Any, Callable

from dbabrain.common.hostcmd import HostCommandError, parse_host, run
from dbabrain.lib import errors
from dbabrain.lib import repository_keys as K
from dbabrain.lib.integrity import SHA256_HEX as _SHA256

ENCRYPT_FIELDS = frozenset({"host", "path", "file_name", "size_bytes", "sha256", "recipients", "key_id",
                            "key_expires_at", "must_be_under", "timeout_seconds"})
VERIFY_FIELDS = frozenset({"host", "path", "size_bytes", "sha256", "identity", "plain_size_bytes",
                           "plain_sha256", "transfer", "timeout_seconds"})
FETCH_FIELDS = frozenset({"source", "path", "size_bytes", "sha256", "identity", "target", "target_path",
                          "file_name", "plain_size_bytes", "plain_sha256", "transfer", "overwrite",
                          "stall_seconds", "timeout_seconds"})
ON_HOST_FIELDS = frozenset({"host", "path", "size_bytes", "sha256", "target_path", "file_name", "plain_size_bytes",
                            "plain_sha256", "identity", "overwrite", "must_be_under", "timeout_seconds"})
_CHUNK = 1 << 20


class RepoEncryptError(errors.RequestError):
    """The request, the host, the key or the file is not what encrypting it needs."""


# --------------------------------------------------------------------------- #
# encrypt-file
# --------------------------------------------------------------------------- #
def encrypt_file(request: dict[str, Any], *, now: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """``{path, encrypted_path, size_bytes, sha256, encrypted_size_bytes, encrypted_sha256, key_id,
    age_version}`` - or a refusal naming why, with nothing written."""
    _known(request, ENCRYPT_FIELDS, "encrypt-file")
    host = parse_host(request.get("host"))
    if host.is_windows:
        raise RepoEncryptError("encrypt-file runs on a Linux repository host (runtime linux | docker | k8s).")
    path = _path(request.get("path"))
    root = _path(request.get("must_be_under"), field="must_be_under").rstrip("/")
    if not path.startswith(root + "/"):
        raise RepoEncryptError(f"path {path!r} is not under must_be_under {root!r}.")
    file_name = request.get("file_name")
    if not isinstance(file_name, str) or file_name != path.rsplit("/", 1)[-1]:
        raise RepoEncryptError(f"file_name must be the last part of path ({path.rsplit('/', 1)[-1]!r}); got {file_name!r}.")
    if file_name.endswith(K.SUFFIX):
        raise RepoEncryptError(f"{path} is already an age file - never encrypted twice.")
    size = _size(request.get("size_bytes"), "size_bytes")
    sha = _sha(request.get("sha256"), "sha256")
    recipients = request.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        raise RepoEncryptError("recipients is required: the key's age1... public key.")
    for item in recipients:
        if not isinstance(item, str) or not K.RECIPIENT.match(item):
            raise RepoEncryptError(f"not an age public key: {item!r}.")
    key_id = request.get("key_id")
    if isinstance(key_id, bool) or not isinstance(key_id, int) or key_id < 1:
        raise RepoEncryptError(f"key_id is required: the key's number; got {key_id!r}.")
    expires = K.parse_time(request.get("key_expires_at"), "key_expires_at")
    moment = (now or (lambda: datetime.now(timezone.utc)))()
    if not K.may_encrypt(expires, moment):
        raise RepoEncryptError(f"key {key_id} expired at {K.time_text(expires)} - an expired key only decrypts, "
                               "it never encrypts again; a new key is made for new files.")
    timeout = _timeout(request.get("timeout_seconds", 3600))
    try:
        result = run(host, posix_script(path, size=size, sha=sha, recipients=recipients), timeout=timeout)
    except HostCommandError as exc:
        raise RepoEncryptError(f"{path}: the host could not be reached: {exc}") from exc
    return _encrypt_answer(path, result, size=size, sha=sha, key_id=key_id)


def posix_script(path: str, *, size: int, sha: str, recipients: list[str]) -> str:
    """One file: checked, encrypted to a partial, moved into place without replacing, re-checked."""
    p = shlex.quote(path)
    keys = " ".join(f"-r {shlex.quote(r)}" for r in recipients)
    return (
        f"p={p}; o=\"$p{K.SUFFIX}\"; part=\"$o.$$.partial\"; "
        "if [ -L \"$p\" ]; then echo 'LINK'; exit 0; fi; "
        "if [ ! -f \"$p\" ]; then echo 'MISSING'; exit 0; fi; "
        "if ! command -v age >/dev/null 2>&1; then echo 'NO_AGE'; exit 0; fi; "
        "if [ -e \"$o\" ] || [ -L \"$o\" ]; then echo 'EXISTS'; exit 0; fi; "
        "s=$(stat -c %s -- \"$p\") || exit 3; "
        f"if [ \"$s\" != {int(size)} ]; then echo \"SIZE $s\"; exit 0; fi; "
        "h=$(sha256sum < \"$p\") || exit 4; h=${h%% *}; "
        f"if [ \"$h\" != {shlex.quote(sha)} ]; then echo \"SHA $h\"; exit 0; fi; "
        f"if ! err=$(age {keys} -o \"$part\" \"$p\" 2>&1); then "
        "echo \"AGE_FAILED $(printf '%s' \"$err\" | head -c 300 | tr '\\n' ' ')\"; exit 0; fi; "
        "if ! mv -n -- \"$part\" \"$o\" || [ -e \"$part\" ]; then echo \"EXISTS_AFTER $part\"; exit 0; fi; "
        "s2=$(stat -c %s -- \"$p\"); "
        f"if [ \"$s2\" != {int(size)} ]; then echo \"CHANGED $s2\"; exit 0; fi; "
        "es=$(stat -c %s -- \"$o\") || exit 5; eh=$(sha256sum < \"$o\") || exit 6; "
        "printf 'OK %s %s %s\\n' \"$es\" \"${eh%% *}\" \"$(age --version 2>/dev/null | head -n 1 | tr ' ' '_')\""
    )


def _encrypt_answer(path: str, result: dict[str, Any], *, size: int, sha: str, key_id: int) -> dict[str, Any]:
    lines = [line.strip() for line in str(result.get("stdout") or "").splitlines() if line.strip()]
    if int(result.get("exit_code") or 0) != 0 or len(lines) != 1:
        detail = (str(result.get("stderr") or "").strip() or " | ".join(lines))[:400]
        raise RepoEncryptError(f"{path}: unexpected answer from the host (exit {result.get('exit_code')}): {detail}")
    token, _, rest = lines[0].partition(" ")
    reasons = {
        "LINK": "is a link - never followed",
        "MISSING": "is not there",
        "NO_AGE": "the host has no `age` tool - nothing was encrypted (install it: apt install age)",
        "EXISTS": f"{path}{K.SUFFIX} is already there - never replaced",
        "SIZE": f"is {rest} bytes, not the {size} stated - not the file the catalog proved; not encrypted",
        "SHA": f"has sha256 {rest}, not the {sha} stated - not the file the catalog proved; not encrypted",
        "AGE_FAILED": f"age failed: {rest}",
        "EXISTS_AFTER": f"the encrypted file could not be moved into place; its partial is {rest}",
        "CHANGED": f"changed while it was encrypted ({rest} bytes) - the encrypted file is not trusted",
    }
    if token != "OK":
        raise RepoEncryptError(f"{path}: {reasons.get(token, f'unexpected answer from the host: {lines[0]!r}')}")
    parts = rest.split(" ")
    if len(parts) != 3 or not parts[0].isdigit() or not _SHA256.match(parts[1]):
        raise RepoEncryptError(f"{path}: unexpected answer from the host: {lines[0]!r}")
    return {"path": path, "encrypted_path": path + K.SUFFIX, "size_bytes": size, "sha256": sha,
            "encrypted_size_bytes": int(parts[0]), "encrypted_sha256": parts[1], "key_id": key_id,
            "age_version": parts[2].replace("_", " ")}


# --------------------------------------------------------------------------- #
# verify-encrypted-file
# --------------------------------------------------------------------------- #
class _Measured:
    """A reader that measures what passes through it."""

    def __init__(self, reader: Any) -> None:
        self.reader, self.hash, self.size = reader, hashlib.sha256(), 0

    def read(self, count: int = -1) -> bytes:
        data = self.reader.read(_CHUNK if count is None or count < 0 else count)
        self.hash.update(data)
        self.size += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


class _Sink:
    """Where the plaintext goes: measured, never kept."""

    def __init__(self) -> None:
        self.hash, self.size = hashlib.sha256(), 0

    def write(self, data: bytes) -> int:
        self.hash.update(data)
        self.size += len(data)
        return len(data)

    def flush(self) -> None:
        pass


def verify_encrypted_file(request: dict[str, Any], *, open_reader: Callable[[dict[str, Any], str], Any] | None = None
                          ) -> dict[str, Any]:
    """``{path, size_bytes, sha256, plain_size_bytes, plain_sha256, proven: true}`` or a refusal."""
    _known(request, VERIFY_FIELDS, "verify-encrypted-file")
    path = _path(request.get("path"))
    if not path.endswith(K.SUFFIX):
        raise RepoEncryptError(f"{path} is not an age file ({K.SUFFIX}).")
    size = _size(request.get("size_bytes"), "size_bytes")
    sha = _sha(request.get("sha256"), "sha256")
    plain_size = _size(request.get("plain_size_bytes"), "plain_size_bytes")
    plain_sha = _sha(request.get("plain_sha256"), "plain_sha256")
    identity = request.get("identity")
    if not isinstance(identity, str) or not K.IDENTITY.match(identity.strip()):
        raise RepoEncryptError("identity is required: the key's AGE-SECRET-KEY-1... (never stored by this command).")
    from dbabrain.common.filecopy import FileCopyError, transfer_of

    try:
        transfer = transfer_of(request, "verify-encrypted-file")
    except FileCopyError as exc:
        raise RepoEncryptError(str(exc)) from exc
    pyrage, x25519 = K.age()
    reader = (open_reader or (lambda host, where: _remote_reader(host, where, transfer)))(request.get("host"), path)
    measured, sink = _Measured(reader), _Sink()
    try:
        pyrage.decrypt_io(measured, sink, [x25519.Identity.from_str(identity.strip())])
    except Exception as exc:  # noqa: BLE001 - named: wrong key, damaged file, short read
        raise RepoEncryptError(f"{path}: cannot be decrypted with this key: {type(exc).__name__}: {exc}") from exc
    finally:
        close = getattr(reader, "close", None)
        if close:
            close()
    problems = []
    if (measured.size, measured.hash.hexdigest()) != (size, sha):
        problems.append(f"the encrypted file is {measured.size} bytes / {measured.hash.hexdigest()}, "
                        f"not the {size} / {sha} measured when it was written")
    if (sink.size, sink.hash.hexdigest()) != (plain_size, plain_sha):
        problems.append(f"it decrypts to {sink.size} bytes / {sink.hash.hexdigest()}, not the plain file's "
                        f"{plain_size} / {plain_sha}")
    if problems:
        raise RepoEncryptError(f"{path}: not proven - {'; '.join(problems)}.")
    return {"path": path, "size_bytes": size, "sha256": sha, "plain_size_bytes": plain_size,
            "plain_sha256": plain_sha, "proven": True}


# --------------------------------------------------------------------------- #
# fetch-decrypted-file - the way back: a restore's copy of an encrypted backup
# --------------------------------------------------------------------------- #
class _TargetSink:
    """Where the plaintext goes on its way to the restore target: measured, and written."""

    def __init__(self, out: Any) -> None:
        self.out, self.hash, self.size = out, hashlib.sha256(), 0

    def write(self, data: bytes) -> int:
        self.hash.update(data)
        self.size += len(data)
        self.out.write(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass


def fetch_decrypted_file(request: dict[str, Any], *, open_reader: Callable[[dict[str, Any], str], Any] | None = None,
                         open_target: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    """ONE ``.age`` file from the repository, decrypted on its way to a restore target (0.37.0).

    The operator, 2026-11-10: *the way back - copy from the repository to a VM, then unpack*. The
    ``.age`` is streamed from the repository (``transfer``: cat - read by a process of its own - or
    sftp), measured as it passes, decrypted on the way with the key's identity (never written
    anywhere), and the plaintext written to ``target_path`` through a partial name. It takes its name
    only when the ``.age`` is the bytes and sha256 measured when it was encrypted AND the plaintext is
    the plain file's bytes and sha256 - the very file the backup wrote. Anything else: the partial is
    removed, nothing takes the name. ``overwrite`` (required) says whether a file already at the name
    may be replaced - false leaves it as it is and the file fails."""
    from dbabrain.common import filecopy

    _known(request, FETCH_FIELDS, "fetch-decrypted-file")
    path = _path(request.get("path"))
    if not path.endswith(K.SUFFIX):
        raise RepoEncryptError(f"{path} is not an age file ({K.SUFFIX}).")
    size, sha = _size(request.get("size_bytes"), "size_bytes"), _sha(request.get("sha256"), "sha256")
    plain_size = _size(request.get("plain_size_bytes"), "plain_size_bytes")
    plain_sha = _sha(request.get("plain_sha256"), "plain_sha256")
    target_path = _path(request.get("target_path"), field="target_path")
    name = request.get("file_name")
    if not isinstance(name, str) or name != target_path.rsplit("/", 1)[-1] or path.rsplit("/", 1)[-1] != name + K.SUFFIX:
        raise RepoEncryptError(f"file_name must be the last part of target_path, and path must be it + {K.SUFFIX}; "
                               f"got {name!r} for {path!r} -> {target_path!r}.")
    identity = request.get("identity")
    if not isinstance(identity, str) or not K.IDENTITY.match(identity.strip()):
        raise RepoEncryptError("identity is required: the key's AGE-SECRET-KEY-1... (never stored by this command).")
    overwrite = request.get("overwrite")
    if not isinstance(overwrite, bool):
        raise RepoEncryptError('"overwrite" is required: false (a file at the name is left as it is) or true.')
    try:
        transfer = filecopy.transfer_of(request, "fetch-decrypted-file")
    except filecopy.FileCopyError as exc:
        raise RepoEncryptError(str(exc)) from exc
    stall = int(request.get("stall_seconds") or filecopy.STALL_SECONDS)
    pyrage, x25519 = K.age()

    target_session = (open_target or (lambda login: filecopy._ssh(login, role="target")))(request.get("target"))
    reader = None
    partial = target_path + filecopy.PARTIAL_SUFFIX
    try:
        sftp = target_session.sftp()
        if not overwrite and filecopy._exists(sftp, target_path):
            raise RepoEncryptError(f"{target_path} is already there and overwrite is false - left as it is.")
        filecopy._mkdirs(sftp, target_path.rsplit("/", 1)[0])
        reader = (open_reader or (lambda host, where: _remote_reader(host, where, transfer)))(request.get("source"), path)
        measured = _Measured(reader)
        if transfer == "cat" and hasattr(target_session, "client"):
            out_cm: Any = filecopy._CatWriter(target_session.client, partial)
        else:
            out_cm = sftp.open(partial, "wb")
            filecopy._tune(out_cm, stall=stall, pipelined=True)
        with out_cm as out:
            sink = _TargetSink(out)
            try:
                pyrage.decrypt_io(measured, sink, [x25519.Identity.from_str(identity.strip())])
            except Exception as exc:  # noqa: BLE001 - wrong key, damaged file, short read
                raise RepoEncryptError(f"{path}: cannot be decrypted with this key: {type(exc).__name__}: {exc}") from exc
        problems = []
        if (measured.size, measured.hash.hexdigest()) != (size, sha):
            problems.append(f"the encrypted file is {measured.size} bytes / {measured.hash.hexdigest()}, "
                            f"not the {size} / {sha} measured when it was written")
        if (sink.size, sink.hash.hexdigest()) != (plain_size, plain_sha):
            problems.append(f"it decrypts to {sink.size} bytes / {sink.hash.hexdigest()}, not the plain file's "
                            f"{plain_size} / {plain_sha}")
        if problems:
            raise RepoEncryptError(f"{path}: not restored - {'; '.join(problems)}.")
        if overwrite:
            filecopy._replace(sftp, partial, target_path)
        else:
            filecopy._place_new(sftp, partial, target_path)
    except BaseException:
        try:
            target_session.sftp().remove(partial)
        except Exception:  # noqa: BLE001 - nothing was written, or the link is gone
            pass
        _close_quietly(reader)
        target_session.close()
        raise
    # The reader's own end - a read that failed after its bytes - fails the file even now: it is
    # already in place, so it is removed again and the failure said.
    try:
        if reader is not None and getattr(reader, "close", None):
            reader.close()
    except RepoEncryptError:
        try:
            target_session.sftp().remove(target_path)
        finally:
            target_session.close()
        raise
    target_session.close()
    return {"path": path, "target_path": target_path, "file_name": name, "size_bytes": size, "sha256": sha,
            "plain_size_bytes": plain_size, "plain_sha256": plain_sha, "transfer": transfer, "restored": True}


# --------------------------------------------------------------------------- #
# decrypt-on-host - the restore target IS the repository host
# --------------------------------------------------------------------------- #
def decrypt_on_host(request: dict[str, Any], *, open_session: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    """ONE ``.age`` file decrypted where it lies, into ``target_path`` on the SAME host (0.37.0).

    The operator, 2026-11-10: *why copy it to my PC and back - restore directly*. When the restore
    target is the repository host itself, the bytes need not leave it: ``age -d`` runs there. The
    key's private half goes to it on stdin and into a process substitution - never on a command
    line, never written to a disk - for the minutes the decryption takes. Held exactly as
    ``fetch-decrypted-file`` holds a copy: the ``.age`` must be the bytes and sha256 measured when
    it was encrypted BEFORE anything is written; the plaintext must be the plain file's bytes and
    sha256 BEFORE it takes its name (a partial of this run's own is removed when it is not);
    ``overwrite`` (required) says whether a file at the name may be replaced."""
    from dbabrain.common import hostcmd

    _known(request, ON_HOST_FIELDS, "decrypt-on-host")
    host = hostcmd.parse_host(request.get("host"))
    if host.is_windows:
        raise RepoEncryptError("decrypt-on-host runs on a Linux host (runtime linux | docker | k8s).")
    path = _path(request.get("path"))
    if not path.endswith(K.SUFFIX):
        raise RepoEncryptError(f"{path} is not an age file ({K.SUFFIX}).")
    target = _path(request.get("target_path"), field="target_path")
    root = _path(request.get("must_be_under"), field="must_be_under").rstrip("/")
    if not target.startswith(root + "/"):
        raise RepoEncryptError(f"target_path {target!r} is not under must_be_under {root!r}.")
    name = request.get("file_name")
    if not isinstance(name, str) or name != target.rsplit("/", 1)[-1] or path.rsplit("/", 1)[-1] != name + K.SUFFIX:
        raise RepoEncryptError(f"file_name must be the last part of target_path, and path must be it + {K.SUFFIX}; "
                               f"got {name!r} for {path!r} -> {target!r}.")
    size, sha = _size(request.get("size_bytes"), "size_bytes"), _sha(request.get("sha256"), "sha256")
    plain_size = _size(request.get("plain_size_bytes"), "plain_size_bytes")
    plain_sha = _sha(request.get("plain_sha256"), "plain_sha256")
    identity = request.get("identity")
    if not isinstance(identity, str) or not K.IDENTITY.match(identity.strip()):
        raise RepoEncryptError("identity is required: the key's AGE-SECRET-KEY-1... (never stored by this command).")
    overwrite = request.get("overwrite")
    if not isinstance(overwrite, bool):
        raise RepoEncryptError('"overwrite" is required: false (a file at the name is left as it is) or true.')
    timeout = _timeout(request.get("timeout_seconds", 3600))
    script = on_host_script(path, target, size=size, sha=sha, plain_size=plain_size, plain_sha=plain_sha,
                            overwrite=overwrite)
    session = (open_session or hostcmd.open_session)(host)
    try:
        result = session.run(hostcmd.wrap(host, script), timeout_seconds=timeout, stdin=identity.strip() + "\n")
    except Exception as exc:  # noqa: BLE001 - could not run at all
        raise RepoEncryptError(f"{path}: the host could not be reached: {exc}") from exc
    finally:
        session.close()
    lines = [line.strip() for line in str(result.stdout or "").splitlines() if line.strip()]
    if int(result.exit_code or 0) != 0 or len(lines) != 1:
        raise RepoEncryptError(f"{path}: unexpected answer from the host (exit {result.exit_code}): "
                               f"{(str(result.stderr or '').strip() or ' | '.join(lines))[:400]}")
    token, _, rest = lines[0].partition(" ")
    reasons = {
        "LINK": "is a link - never followed", "MISSING": "is not there",
        "NO_AGE": "the host has no `age` tool - nothing was decrypted",
        "EXISTS": f"{target} is already there and overwrite is false - left as it is",
        "SIZE": f"is {rest} bytes, not the {size} measured when it was encrypted - not decrypted",
        "SHA": f"has sha256 {rest}, not the {sha} measured when it was encrypted - not decrypted",
        "AGE_FAILED": f"cannot be decrypted with this key: {rest}",
        "PLAIN": f"decrypts to {rest}, not the plain file's {plain_size} / {plain_sha} - not restored",
        "PLACE_FAILED": f"the decrypted file could not take its name: {rest}",
    }
    if token not in ("OK", "SAME"):
        raise RepoEncryptError(f"{path}: {reasons.get(token, f'unexpected answer from the host: {lines[0]!r}')}")
    return {"path": path, "target_path": target, "file_name": name, "size_bytes": size, "sha256": sha,
            "plain_size_bytes": plain_size, "plain_sha256": plain_sha, "restored": True,
            "already_there": token == "SAME"}


def on_host_script(path: str, target: str, *, size: int, sha: str, plain_size: int, plain_sha: str,
                   overwrite: bool) -> str:
    """bash: the identity is the first line of stdin, read into a variable and handed to age through a
    process substitution - it reaches no argument list and no file."""
    q = shlex.quote
    place = 'mv -f -- "$part" "$o"' if overwrite else '{ mv -n -- "$part" "$o" && [ ! -e "$part" ]; }'
    inner = (
        f'p={q(path)}; o={q(target)}; part="$o.$$.dbabrain_partial"; IFS= read -r KEY; '
        'if [ -L "$p" ]; then echo LINK; exit 0; fi; '
        'if [ ! -f "$p" ]; then echo MISSING; exit 0; fi; '
        'if ! command -v age >/dev/null 2>&1; then echo NO_AGE; exit 0; fi; '
        # The plain file already at the name, the very bytes and sha256 the backup wrote: nothing to do
        # (a restore run again after a later step failed - decrypting 6 GB again proves nothing new).
        + ('if [ -f "$o" ] && [ ! -L "$o" ] && [ "$(stat -c %s -- "$o")" = ' + str(int(plain_size)) + ' ]; then '
           'eh=$(sha256sum < "$o"); [ "${eh%% *}" = ' + q(plain_sha) + ' ] && { echo SAME; exit 0; }; fi; ')
        + ('' if overwrite else 'if [ -e "$o" ] || [ -L "$o" ]; then echo EXISTS; exit 0; fi; ')
        + 's=$(stat -c %s -- "$p") || exit 3; '
        f'if [ "$s" != {int(size)} ]; then echo "SIZE $s"; exit 0; fi; '
        'h=$(sha256sum < "$p") || exit 4; h=${h%% *}; '
        f'if [ "$h" != {q(sha)} ]; then echo "SHA $h"; exit 0; fi; '
        'mkdir -p -- "$(dirname -- "$o")" || exit 5; '
        'if ! err=$(age -d -i <(printf "%s\\n" "$KEY") -o "$part" "$p" 2>&1); then '
        'rm -f -- "$part"; echo "AGE_FAILED $(printf "%s" "$err" | head -c 300 | tr "\\n" " ")"; exit 0; fi; '
        'KEY=; ps=$(stat -c %s -- "$part"); ph=$(sha256sum < "$part"); ph=${ph%% *}; '
        f'if [ "$ps" != {int(plain_size)} ] || [ "$ph" != {q(plain_sha)} ]; then '
        'rm -f -- "$part"; echo "PLAIN $ps / $ph"; exit 0; fi; '
        f'if ! {place}; then echo "PLACE_FAILED $part"; exit 0; fi; '
        'echo OK'
    )
    return "bash -c " + q(inner)


def _close_quietly(reader: Any) -> None:
    try:
        if reader is not None and getattr(reader, "close", None):
            reader.close()
    except Exception:  # noqa: BLE001 - the error already raised is the one that matters
        pass


def _remote_reader(login: Any, path: str, transfer: str):
    """The ``.age`` file as a stream - ``cat`` (a large-window channel, read ahead in a thread) or
    ``sftp`` - the same two transfers ``copy-files`` takes (:data:`filecopy.TRANSFERS`)."""
    from dbabrain.common import filecopy

    sftp = None
    session = None
    try:
        if transfer == "cat":
            # Read by a child process (its own core) while this one decrypts and hashes (0.37.0).
            inner = filecopy.ProcessReader(dict(login), "", path)
        else:
            from dbabrain.common import remote_exec

            session = filecopy._ssh(login, role="host")
            sftp = remote_exec.sftp_on(session.client)
            inner = filecopy._SftpReader(sftp, path)
    except (OSError, filecopy.FileCopyError) as exc:
        if sftp is not None:
            sftp.close()
        if session is not None:
            session.close()
        raise RepoEncryptError(f"{path}: cannot be opened on the host: {exc}") from exc
    ahead = filecopy._ReadAhead(inner)

    class _Closing:
        def read(self, count: int) -> bytes:
            return ahead.read(count)

        def close(self) -> None:
            ahead.halt()
            failure = inner.finish() if transfer == "cat" else ""
            inner.close()
            if sftp is not None:
                sftp.close()
            if session is not None:
                session.close()
            if failure:
                raise RepoEncryptError(f"{path}: {failure}")
    return _Closing()


# --------------------------------------------------------------------------- #
# Checks shared by both
# --------------------------------------------------------------------------- #
def _known(request: Any, allowed: frozenset[str], command: str) -> None:
    if not isinstance(request, dict):
        raise RepoEncryptError(f"{command} takes one JSON object.")
    unknown = sorted(set(request) - allowed)
    if "paths" in unknown or "files" in unknown:
        raise RepoEncryptError(f"{command} takes ONE file per command - give `path`, never a list.")
    if unknown:
        raise RepoEncryptError(f"{command}: unknown field(s) {', '.join(unknown)}; it takes {', '.join(sorted(allowed))}.")


def _path(value: Any, *, field: str = "path") -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise RepoEncryptError(f"{field} is required: one full path, no surrounding space or control character.")
    if not value.startswith("/"):
        raise RepoEncryptError(f"{field} must be absolute: {value!r}.")
    if any(c in value for c in "*?[]"):
        raise RepoEncryptError(f"{field} must name one path, not a pattern: {value!r}.")
    if any(part in (".", "..") for part in value.split("/")):
        raise RepoEncryptError(f"{field} must not contain '.' or '..' segments: {value!r}.")
    return value


def _size(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepoEncryptError(f"{field} is required: a whole number of bytes, 0 or more; got {value!r}.")
    return value


def _sha(value: Any, field: str) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not _SHA256.match(text):
        raise RepoEncryptError(f"{field} is required: 64 hexadecimal characters; got {value!r}.")
    return text


def _timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 10:
        raise RepoEncryptError(f"timeout_seconds must be a whole number, 10 or more; got {value!r}.")
    return value
