"""Encrypted secret-text storage for db_ops.

Secrets (DB passwords, the Telegram bot token, ...) are stored encrypted at
``data/encrypted_secret_text.json`` so the repo never contains plaintext
secrets. The encryption passphrase is supplied at runtime. Standalone CLIs can
receive ``--key``/``--key-base64`` directly or read ``DB_OPS_SECRET_KEY``; the
app-command daemon forwards its CLI key argument to child app commands so they
decrypt on demand through this module without depending on daemon global state.

Encryption scheme: PBKDF2-HMAC-SHA256 derives a 32-byte key from the passphrase
and a random per-file salt; the secret JSON is then sealed with Fernet
(AES-128-CBC + HMAC). The on-disk file is a small JSON envelope:

    {
      "marker": "db_ops.secret_text.v1",
      "kdf": "pbkdf2_hmac_sha256",
      "iterations": 200000,
      "salt": "<base64>",
      "ciphertext": "<fernet-token>"
    }

The loader auto-detects encrypted vs. plaintext files, so a plaintext
``secret_text.json`` keeps working during migration.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any

SECRET_KEY_ENV_VAR = "DB_OPS_SECRET_KEY"
ENCRYPTED_SECRET_TEXT_FILENAME = "encrypted_secret_text.json"
SECRET_TEXT_FILENAME = "secret_text.json"

_ENCRYPTED_MARKER = "db_ops.secret_text.v1"
_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16


def _crypto():
    """Import cryptography lazily so the package is only required when secrets
    are actually encrypted/decrypted."""
    try:
        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'cryptography' package is required for encrypted secret text. "
            "Install it with: pip install cryptography"
        ) from exc
    return Fernet, InvalidToken, hashes, PBKDF2HMAC


def _derive_fernet_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    _, _, hashes, PBKDF2HMAC = _crypto()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_secret_text(secrets: dict[str, Any], key: str, *, iterations: int = _PBKDF2_ITERATIONS) -> dict[str, Any]:
    """Encrypt a secrets dict into the on-disk envelope structure."""
    if not key:
        raise ValueError("Encryption key must not be empty.")
    if not isinstance(secrets, dict):
        raise TypeError("secrets must be a dict.")
    Fernet, _, _, _ = _crypto()
    salt = os.urandom(_SALT_BYTES)
    fernet = Fernet(_derive_fernet_key(key, salt, iterations))
    payload = json.dumps(secrets, ensure_ascii=False).encode("utf-8")
    token = fernet.encrypt(payload)
    return {
        "marker": _ENCRYPTED_MARKER,
        "kdf": "pbkdf2_hmac_sha256",
        "iterations": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "ciphertext": token.decode("ascii"),
    }


def decrypt_secret_text(blob: dict[str, Any], key: str) -> dict[str, str]:
    """Decrypt an on-disk envelope back into a {name: secret} mapping."""
    if not key:
        raise RuntimeError(
            f"No decryption key provided. Pass --key or set the {SECRET_KEY_ENV_VAR} environment variable."
        )
    Fernet, InvalidToken, _, _ = _crypto()
    try:
        salt = base64.b64decode(str(blob["salt"]))
        iterations = int(blob.get("iterations", _PBKDF2_ITERATIONS))
        token = str(blob["ciphertext"]).encode("ascii")
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Malformed encrypted secret text envelope: {exc}") from exc
    try:
        plaintext = Fernet(_derive_fernet_key(key, salt, iterations)).decrypt(token)
    except InvalidToken as exc:
        raise RuntimeError("Failed to decrypt secret text: wrong key or corrupted file.") from exc
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Decrypted secret text is not a JSON object.")
    return {str(name): str(value) for name, value in data.items()}


def is_encrypted_blob(data: Any) -> bool:
    return isinstance(data, dict) and data.get("marker") == _ENCRYPTED_MARKER and "ciphertext" in data


def resolve_key(explicit_key: str | None = None) -> str:
    """Resolve the secret key: explicit argument first, then the environment."""
    if explicit_key:
        return str(explicit_key)
    return os.environ.get(SECRET_KEY_ENV_VAR, "")


def decode_key_base64(key_base64: str) -> str:
    """Decode a UTF-8 passphrase supplied as base64 text."""
    try:
        return base64.b64decode(str(key_base64), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("--key_base64 must be valid base64-encoded UTF-8 text.") from exc


def resolve_cli_key(key: str | None = None, key_base64: str | None = None) -> str | None:
    """Resolve CLI key arguments, accepting either plaintext or base64."""
    if key and key_base64:
        raise ValueError("Use only one of --key or --key_base64.")
    if key_base64:
        return decode_key_base64(key_base64)
    return key


def set_key_env(key: str | None, key_base64: str | None = None) -> None:
    """Export the key so spawned child processes (e.g. daemon app commands)
    inherit it. No-op when key is empty."""
    resolved_key = resolve_cli_key(key, key_base64)
    if resolved_key:
        os.environ[SECRET_KEY_ENV_VAR] = str(resolved_key)


def add_key_argument(parser: Any, *, inherited: bool = False) -> None:
    """Add the standard ``--key`` / ``--key-base64`` arguments to an argparse parser.

    ``inherited=True`` is for a shared ``parents=[...]`` parser whose owner **also** registers
    these flags at the top level. argparse applies a subparser's defaults *after* the top-level
    parse, so a plain ``default=None`` on the inherited copy silently overwrites a
    ``--key-base64`` that was given before the subcommand — turning a correct command line into
    "No decryption key provided". ``SUPPRESS`` leaves the attribute untouched when the flag is
    absent, so whichever level actually supplied it wins.

    A parser built with ``inherited=True`` may leave the attribute unset, so its ``main`` must
    read the value with ``getattr(args, "key", None)`` rather than ``args.key``.
    """
    default = argparse.SUPPRESS if inherited else None
    parser.add_argument(
        "--key",
        default=default,
        help=(
            "Passphrase to decrypt data/encrypted_secret_text.json. "
            f"Falls back to the {SECRET_KEY_ENV_VAR} environment variable."
        ),
    )
    parser.add_argument(
        "--key-base64",
        "--key_base64",
        dest="key_base64",
        default=default,
        help=(
            "Base64-encoded UTF-8 passphrase to decrypt data/encrypted_secret_text.json. "
            "Use this instead of --key when the passphrase contains shell-sensitive characters."
        ),
    )


def load_secret_text_file(path: str | Path, *, key: str | None = None) -> dict[str, str]:
    """Load one secret-text file. Auto-detects encrypted vs. plaintext JSON.
    Returns {} when the file does not exist."""
    path = Path(path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as file:
        raw = json.load(file)
    if is_encrypted_blob(raw):
        return decrypt_secret_text(raw, resolve_key(key))
    if isinstance(raw, dict):
        return {str(name): str(value) for name, value in raw.items()}
    raise RuntimeError(f"Secret text file must be a JSON object: {path}")


def load_secret_text(data_dir: str | Path, *, key: str | None = None) -> dict[str, str]:
    """Load secrets from ``<data_dir>/encrypted_secret_text.json``.

    Encrypted-only by design: there is NO fallback to a plaintext file. A wrong
    or missing key raises (it never silently degrades). A missing encrypted file
    returns {} so a not-yet-provisioned environment fails later with a clear
    "password ref not found" rather than here."""
    return load_secret_text_file(Path(data_dir) / ENCRYPTED_SECRET_TEXT_FILENAME, key=key)


def set_secret_text(data_dir: str | Path, ref: str, value: str, *, key: str | None = None,
                    overwrite: bool = False) -> bool:
    """Store ``value`` under ``ref`` in ``<data_dir>/encrypted_secret_text.json``.

    The whole store is decrypted, updated and re-encrypted with the same passphrase, so the
    file on disk stays a single encrypted blob — a new secret never lands in plaintext.
    Returns True when the store was written; False when ``ref`` already holds that exact value.
    Refuses to overwrite a *different* value unless ``overwrite`` is set: a lab password typo
    must not silently replace a production credential that happens to share the ref.
    """
    ref = str(ref or "").strip()
    if not ref:
        raise RuntimeError("Secret ref must not be empty.")
    if not str(value or ""):
        raise RuntimeError(f"Refusing to store an empty secret for {ref}.")

    resolved_key = resolve_key(key)
    path = Path(data_dir) / ENCRYPTED_SECRET_TEXT_FILENAME
    secrets = load_secret_text_file(path, key=resolved_key)
    existing = secrets.get(ref)
    if existing == value:
        return False
    if existing is not None and not overwrite:
        raise RuntimeError(
            f"Secret ref '{ref}' already exists with a different value. "
            "Choose another ref, or pass overwrite=True to replace it."
        )

    secrets[ref] = str(value)
    blob = encrypt_secret_text(secrets, resolved_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def set_secret_everywhere(data_dir: str | Path, ref: str, value: str, *,
                          key: str | None = None, plaintext_store: str | Path | None = None,
                          overwrite: bool = True) -> bool:
    """Store one secret in the encrypted store **and** in the plaintext source that regenerates it.

    Writing only the encrypted blob looks complete and is silently undone: ``control.cli deploy``
    regenerates ``data/encrypted_secret_text.json`` from the gitignored
    ``secrets/secret_text.json`` before it ships, so a ref that exists only in the encrypted copy
    disappears at the next deploy — and the failure surfaces later, as an authentication error on
    a node nobody was looking at.

    ``plaintext_store`` that does not exist is not an error: the worker carries the encrypted store
    only, and a write there is meant to be picked up by ``worker-pull-data-config --merge-secrets``
    rather than to have a local source to update.

    Returns True when the encrypted store changed.
    """
    resolved_dir = Path(data_dir)
    written = set_secret_text(resolved_dir, ref, value, key=key, overwrite=overwrite)

    plain_path = Path(plaintext_store) if plaintext_store else None
    if plain_path is not None and plain_path.exists():
        plain = json.loads(plain_path.read_text(encoding="utf-8"))
        if plain.get(ref) != value:
            plain[ref] = value
            plain_path.write_text(
                json.dumps({name: plain[name] for name in sorted(plain)},
                           indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            written = True
    return written


def encrypt_secret_text_file(source: str | Path, dest: str | Path, key: str) -> int:
    """Encrypt the plaintext ``source`` JSON into the encrypted ``dest`` file.

    ``source`` is a flat ``{ref: secret}`` JSON object (gitignored plaintext); ``dest`` is
    the committed ``encrypted_secret_text.json`` blob. The result is round-tripped before it
    is written, so a bad key or broken crypto library never produces an unreadable file.
    Returns the number of secrets encrypted.
    """
    if not key:
        raise ValueError("Encryption key must not be empty.")
    source = Path(source)
    dest = Path(dest)
    if not source.exists():
        raise FileNotFoundError(f"Plaintext secret file not found: {source}")

    with source.open("r", encoding="utf-8-sig") as file:
        secrets = json.load(file)
    if not isinstance(secrets, dict):
        raise RuntimeError(f"Secret file must be a JSON object: {source}")
    # Keys beginning with `_` are commentary, not secrets. `secret_text.example.json` has used
    # `_notes` since it was written, but nothing enforced it — so the notes were encrypted into the
    # store as a secret named `_notes`, and the count reported one more secret than existed. Free
    # to fix and worth fixing: a file an operator has to hand-edit needs somewhere to put a
    # sentence, and that place should not become data.
    secrets = {
        str(name): str(value)
        for name, value in secrets.items()
        if not str(name).startswith("_")
    }

    blob = encrypt_secret_text(secrets, key)
    if decrypt_secret_text(blob, key) != secrets:
        raise RuntimeError("Round-trip verification failed; refusing to write output.")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(blob, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(secrets)
