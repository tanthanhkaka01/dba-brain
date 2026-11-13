"""A certificate vault row as a file, sealed with ``age`` for dbabrain AND an offline recovery key (0.37.0).

The operator, 2026-11-10 (translated): *keep the certificate as a physical file as well as in the
vault*; and asked *what if the secret store's passphrase is lost?* The vault row is encrypted with
that passphrase, and so is the ``.pvk`` password in the secret store - lose the passphrase and every
encrypted backup is lost with it. So each proven vault row is also written as ONE file that stands
on its own:

* **Everything a restore needs is inside**: the ``.cer`` and the ``.pvk`` bytes, the ``.pvk``
  password, the thumbprint, and each file's bytes and sha256 - nothing has to be looked up in the
  secret store to use it.
* **Sealed with age** (https://age-encryption.org, X25519) **for at least two recipients**:
  dbabrain's own identity (kept in the secret store), and one or more *recovery* recipients whose
  private half the operator keeps OFF every machine dbabrain runs on - on paper or a USB key in a
  safe. With the passphrase gone, ``age -d -i recovery.key <file>`` opens it with the standard
  ``age`` tool, no dbabrain needed.
* **Written once, never overwritten**: a file already there is proven (unsealed, held to the row's
  bytes, sha256 and thumbprint), never replaced; one that does not prove is a stop, not a rewrite.

This module is the format and the rules - pure, like the rest of ``lib``. ``pyrage`` (the age
library) is imported where it is used, so reading this module never needs it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from dbabrain.lib import errors
from dbabrain.lib.integrity import SHA1_HEX as _SHA1

FORMAT = "dbabrain-certificate-vault/1"
SUFFIX = ".vault.age"
#: The block of ``store_config.json`` that says where the files go and who can open them.
BLOCK = "certificate_vault"
FIELDS = ("file_folder", "identity_ref", "recovery_recipients")

_RECIPIENT = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")
_IDENTITY = re.compile(r"^AGE-SECRET-KEY-1[02-9AC-HJ-NP-Z]{58}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class VaultFileError(errors.ConfigError):
    """The settings, a key, or a file is not what it must be."""


@dataclass(frozen=True)
class VaultFileSettings:
    #: Where this node writes the files - relative to the tool root, or absolute.
    file_folder: str
    #: The secret store ref holding dbabrain's own age identity (``AGE-SECRET-KEY-1...``).
    identity_ref: str
    #: The offline recovery keys' public halves (``age1...``) - at least one.
    recovery_recipients: tuple[str, ...]


def parse_settings(raw: Any) -> VaultFileSettings | None:
    """The ``certificate_vault`` block; ``None`` when it is absent - not configured, said by name."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise VaultFileError(f"{BLOCK} must be an object.")
    unknown = sorted(k for k in raw if k not in FIELDS and not str(k).startswith("_"))
    if unknown:
        raise VaultFileError(f"{BLOCK}: unknown field(s) {', '.join(unknown)}; it takes {', '.join(FIELDS)}.")
    missing = [k for k in FIELDS if k not in raw]
    if missing:
        raise VaultFileError(f"{BLOCK}: {', '.join(missing)} must be stated.")
    folder = raw["file_folder"]
    if not isinstance(folder, str) or not folder.strip():
        raise VaultFileError(f"{BLOCK}.file_folder must be a folder.")
    ref = raw["identity_ref"]
    if not isinstance(ref, str) or not ref.strip():
        raise VaultFileError(f"{BLOCK}.identity_ref must name a secret.")
    recipients = raw["recovery_recipients"]
    if not isinstance(recipients, list) or not recipients:
        # A file only dbabrain can open is lost with the passphrase - the very loss it is for.
        raise VaultFileError(f"{BLOCK}.recovery_recipients must hold at least one recovery key's "
                             "public half (age1...) - make one with `backup_restore vault-key --recovery`.")
    for item in recipients:
        check_recipient(item)
    if len(set(recipients)) != len(recipients):
        raise VaultFileError(f"{BLOCK}.recovery_recipients names one key twice.")
    return VaultFileSettings(file_folder=folder.strip(), identity_ref=ref.strip(),
                             recovery_recipients=tuple(recipients))


def check_recipient(value: Any) -> str:
    if not isinstance(value, str) or not _RECIPIENT.match(value):
        raise VaultFileError(f"not an age public key (age1 + 58 characters): {value!r}.")
    return value


def check_identity(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTITY.match(value.strip()):
        raise VaultFileError("not an age identity (AGE-SECRET-KEY-1 + 58 characters).")
    return value.strip()


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def _age():
    """``(pyrage, x25519)`` - or a refusal that names what to install, never a bare ImportError."""
    try:
        import pyrage
        from pyrage import x25519
    except ImportError as exc:
        raise VaultFileError("age encryption is not installed here (the pyrage package) - "
                             "pip install pyrage; nothing was sealed or opened.") from exc
    return pyrage, x25519


def new_identity() -> tuple[str, str]:
    """``(identity, recipient)`` - a new X25519 key pair: the private half and its ``age1`` public one."""
    _pyrage, x25519 = _age()
    identity = x25519.Identity.generate()
    return str(identity), str(identity.to_public())


def recipient_of(identity: str) -> str:
    _pyrage, x25519 = _age()
    return str(x25519.Identity.from_str(check_identity(identity)).to_public())


def identity_file_text(identity: str, recipient: str, *, created_at: str, purpose: str) -> str:
    """The identity file ``age -i`` reads: comment lines, then the key."""
    return (f"# {purpose}\n# created: {created_at}\n# public key: {recipient}\n"
            f"{check_identity(identity)}\n")


def identity_from_file_text(text: str) -> str:
    keys = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if len(keys) != 1:
        raise VaultFileError("an identity file holds exactly one AGE-SECRET-KEY-1 line.")
    return check_identity(keys[0])


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #
def relative_path(server_id: str, certificate_name: str, thumbprint: str) -> str:
    """``<server_id>/<certificate name>_<thumbprint>.vault.age`` - one file per vault row."""
    for label, value in (("server_id", server_id), ("certificate_name", certificate_name)):
        if not isinstance(value, str) or not _NAME.match(value) or ".." in value:
            raise VaultFileError(f"{label} {value!r} cannot name a file.")
    if not isinstance(thumbprint, str) or not _SHA1.match(thumbprint):
        raise VaultFileError(f"thumbprint {thumbprint!r} is not 40 hexadecimal characters.")
    return f"{server_id}/{certificate_name}_{thumbprint}{SUFFIX}"


def payload(row: Mapping[str, Any], *, cer: bytes, pvk: bytes, pvk_password: str) -> dict[str, Any]:
    """What the file holds - the row's facts and every byte a restore needs."""
    if not pvk_password:
        raise VaultFileError("the .pvk password is required - a file without it opens nothing.")
    return {
        "format": FORMAT,
        "certificate_id": int(row["certificate_id"]),
        "server_id": str(row["server_id"]),
        "certificate_name": str(row["certificate_name"]),
        "thumbprint": str(row["thumbprint"]),
        "cer": _file(row, "cer", cer),
        "pvk": _file(row, "pvk", pvk),
        "pvk_password": pvk_password,
        "password_ref": str(row.get("password_ref") or ""),
        "taken_at": str(row.get("taken_at") or ""),
        "backup_id": str(row.get("backup_id") or ""),
        "restore": ("CREATE CERTIFICATE [<name>] FROM FILE = '<.cer>' WITH PRIVATE KEY (FILE = '<.pvk>', "
                    "DECRYPTION BY PASSWORD = '<pvk_password>') - in master, after a database master key"),
    }


def _file(row: Mapping[str, Any], kind: str, data: bytes) -> dict[str, Any]:
    return {"file_name": str(row[f"{kind}_file_name"]), "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "base64": base64.b64encode(data).decode("ascii")}


def seal(content: Mapping[str, Any], recipients: Sequence[str]) -> bytes:
    """``content`` as JSON, age-encrypted for every recipient - at least two: dbabrain and a recovery key."""
    pyrage, x25519 = _age()
    if len(set(recipients)) < 2:
        raise VaultFileError("a vault file is sealed for dbabrain AND at least one recovery key.")
    keys = [x25519.Recipient.from_str(check_recipient(r)) for r in recipients]
    return pyrage.encrypt(json.dumps(content, sort_keys=True).encode("utf-8"), keys)


def unseal(data: bytes, identity: str) -> dict[str, Any]:
    """The record a file holds - only for an identity it was sealed for. A wrong key, an old key it
    was not sealed for, no key, or bytes that are not a whole age file all raise, by name."""
    if not identity:
        raise VaultFileError("no identity given - a vault file is never opened without its key.")
    if not data:
        raise VaultFileError("the file is empty.")
    pyrage, x25519 = _age()
    try:
        plain = pyrage.decrypt(data, [x25519.Identity.from_str(check_identity(identity))])
        content = json.loads(plain.decode("utf-8"))
    except VaultFileError:
        raise
    except Exception as exc:  # noqa: BLE001 - named: a file this identity cannot open, or not JSON
        raise VaultFileError(f"cannot be opened with this identity: {type(exc).__name__}: {exc}") from exc
    if not isinstance(content, dict):
        raise VaultFileError("does not hold a vault record.")
    return content


def problems(content: Mapping[str, Any], row: Mapping[str, Any]) -> list[str]:
    """Where an opened file differs from its vault row - empty when it proves."""
    found = []
    if content.get("format") != FORMAT:
        found.append(f"format {content.get('format')!r}, not {FORMAT}")
    for key in ("certificate_id", "server_id", "certificate_name", "thumbprint"):
        if str(content.get(key)) != str(row.get(key)):
            found.append(f"{key} {content.get(key)!r}, the row says {row.get(key)!r}")
    decoded = {}
    for kind in ("cer", "pvk"):
        part = content.get(kind) or {}
        try:
            data = base64.b64decode(str(part.get("base64") or ""), validate=True)
        except ValueError:
            found.append(f".{kind} is not base64")
            continue
        decoded[kind] = data
        if (len(data), hashlib.sha256(data).hexdigest()) != (int(row[f"{kind}_size_bytes"]), str(row[f"{kind}_sha256"])):
            found.append(f".{kind} is not the bytes / sha256 kept")
    if "cer" in decoded and hashlib.sha1(decoded["cer"]).hexdigest() != str(row.get("thumbprint")):  # noqa: S324
        found.append(".cer does not have the thumbprint kept")
    if not content.get("pvk_password"):
        found.append("no .pvk password")
    return found
