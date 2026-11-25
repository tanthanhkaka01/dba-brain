"""Seal an SSH private key: encrypted in OpenSSH's own format, its passphrase a secret (0.42.0, 1.410).

Read on the worker on 2026-11-20: four private keys lay unencrypted in ``data/ssh_keys/``, mode
644/664 - any login on the host could take them, and so could any copy of the folder (plan
``audits/20261120_plan_worker_secret_exposure.md``, F4). The operator chose *an encrypted file*
over moving the keys into the store: each key stays a file standard tools can read (``ssh-keygen
-y -f``), encrypted with bcrypt-KDF in OpenSSH's format, with a random passphrase of its own held in
the encrypted secret store - so the file alone opens nothing, and the passphrase that opens the store
is still the one secret.

What one run does, in this order, so a failure leaves the key as it was:

1. read the key (or, with ``generate``, make an ed25519 pair) - a key that is already encrypted is
   refused: sealing it again would need its old passphrase;
2. draw a random passphrase and store it under ``passphrase_ref`` in the node's encrypted store,
   then read it back and compare - a ref that already holds another value is refused;
3. encrypt the key with it, and prove the result opens with it to the same public key (with
   paramiko, the client that reads it);
4. write it in place of the old file (0600, renamed over it whole), and the ``.pub`` beside a
   generated one;
5. name the ref for this key in ``passphrases.json`` in the key's folder - names only.

The passphrase is printed nowhere and returned nowhere. Works from its request alone (rules R09):
the key's full path, the ref; the store written is the node's own, like ``secret-set``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as _secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dbabrain.lib import errors, secret_text
from dbabrain.lib.cmd_access import PASSPHRASES_FILENAME
from dbabrain.lib.json_io import atomic_write_text


class SealError(errors.RequestError):
    """A refusal: nothing was changed."""


def _crypto():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return serialization, ed25519


def load_unsealed(data: bytes):
    """The private key in *data*; :class:`SealError` when it is encrypted already or unreadable."""
    serialization, _ = _crypto()
    errors: list[str] = []
    for loader in (serialization.load_ssh_private_key, serialization.load_pem_private_key):
        try:
            return loader(data, password=None)
        except TypeError as exc:   # "Password was not given but private key is encrypted"
            raise SealError("the key is encrypted already - it is sealed, or protected by a passphrase "
                            "dbabrain does not hold; nothing was changed") from exc
        except (ValueError, Exception) as exc:  # noqa: BLE001 - try the next format
            errors.append(str(exc))
    raise SealError(f"not a private key this command can read ({'; '.join(errors)[:200]})")


def public_line(private_key, comment: str = "") -> str:
    serialization, _ = _crypto()
    line = private_key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                 serialization.PublicFormat.OpenSSH).decode("ascii")
    return f"{line} {comment}".strip()


def fingerprint(public: str) -> str:
    blob = base64.b64decode(public.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def sealed_bytes(private_key, passphrase: str) -> bytes:
    serialization, _ = _crypto()
    try:
        return private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                         serialization.BestAvailableEncryption(passphrase.encode("utf-8")))
    except Exception as exc:  # noqa: BLE001 - cryptography's UnsupportedAlgorithm, named below
        _say_the_extra(exc)
        raise


def _say_the_extra(exc: BaseException) -> None:
    """OpenSSH's format seals with bcrypt, which the [ssh] extra brings (paramiko needs it). Without it the
    answer was cryptography's own *Need bcrypt module* (found by CI on 1.0.0, 1.1.0 item 1.422)."""
    if "bcrypt" in str(exc).lower():
        from dbabrain.lib.packaging import install_hint

        raise SealError(f"sealing a key needs the [ssh] extra (bcrypt, paramiko): {install_hint('ssh')}") from exc


def prove_it_opens(data: bytes, passphrase: str, expected_public: str) -> None:
    """The sealed key opens with *passphrase* to *expected_public* - read the way the client reads it."""
    serialization, _ = _crypto()
    try:
        opened = serialization.load_ssh_private_key(data, password=passphrase.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - see _say_the_extra
        _say_the_extra(exc)
        raise
    if public_line(opened).split()[:2] != expected_public.split()[:2]:
        raise SealError("the sealed key does not open to the same public key; nothing was changed")
    from dbabrain.common.ssh import open_key_file

    handle, name = tempfile.mkstemp(suffix=".key")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        try:
            key = open_key_file(name, passphrase)
        except ImportError:
            return      # no SSH client installed here: the cryptography check above stands alone
        if key.get_base64() != expected_public.split()[1]:
            raise SealError("paramiko opens the sealed key to another public key; nothing was changed")
    finally:
        os.unlink(name)


def _write_private(path: Path, data: bytes) -> None:
    handle, name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.chmod(name, 0o600)
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def name_in_registry(key_path: Path, ref: str) -> Path:
    registry = key_path.parent / PASSPHRASES_FILENAME
    entries: dict[str, Any] = {}
    if registry.is_file():
        entries = json.loads(registry.read_text(encoding="utf-8-sig")) or {}
        if not isinstance(entries, dict):
            raise SealError(f"{registry} is not a JSON object")
    entries[key_path.name] = {"passphrase_ref": ref,
                              "sealed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    atomic_write_text(registry, json.dumps(entries, indent=2, sort_keys=True) + "\n", private=True)
    return registry


def seal(request: dict[str, Any], *, data_dir: str | Path, store_key: str,
         plaintext_store: Path | None = None) -> dict[str, Any]:
    """Seal (or with ``generate`` make and seal) one key; the answer's ``data``. Raises :class:`SealError`."""
    raw_path = str(request.get("key_file") or "").strip()
    ref = str(request.get("passphrase_ref") or "").strip()
    generate = bool(request.get("generate"))
    comment = str(request.get("comment") or "").strip()
    if not raw_path or not ref:
        raise SealError("both 'key_file' (the key's full path) and 'passphrase_ref' are required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SealError(f"key_file {raw_path!r} is not a full path; this command reads no configuration "
                        "(rules R09) - give the key's full path")
    if not store_key:
        raise SealError("the node's passphrase is not given (DBABRAIN_SECRET_KEY): the key's own "
                        "passphrase goes into the encrypted store, which it opens")

    if generate:
        if path.exists():
            raise SealError(f"{path} exists; generate makes a new key and never overwrites one")
        path.parent.mkdir(parents=True, exist_ok=True)
        _, ed25519 = _crypto()
        private_key = ed25519.Ed25519PrivateKey.generate()
    else:
        if not path.is_file():
            raise SealError(f"{path} is not a file")
        private_key = load_unsealed(path.read_bytes())
    public = public_line(private_key, comment or (path.stem if generate else ""))

    passphrase = _secrets.token_urlsafe(32)
    if plaintext_store is not None:
        secret_text.set_secret_everywhere(data_dir, ref, passphrase, key=store_key,
                                          plaintext_store=plaintext_store, overwrite=False)
    else:
        secret_text.set_secret_text(data_dir, ref, passphrase, key=store_key, overwrite=False)
    if secret_text.load_secret_text(data_dir, key=store_key).get(ref) != passphrase:
        raise SealError(f"{ref} did not read back from the store as written; the key was not changed")

    data = sealed_bytes(private_key, passphrase)
    prove_it_opens(data, passphrase, public)
    _write_private(path, data)
    if generate:
        atomic_write_text(path.with_name(path.name + ".pub"), public + "\n")
    registry = name_in_registry(path, ref)
    return {"key_file": str(path), "passphrase_ref": ref, "generated": generate,
            "public_key": public, "fingerprint": fingerprint(public), "registry": str(registry),
            "store": str(Path(data_dir) / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME)}
