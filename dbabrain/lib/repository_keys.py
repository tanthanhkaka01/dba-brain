"""The repository's age keys: one ring per server_id, each key with a lifetime (0.37.0).

The operator, 2026-11-10 (translated): *the key has a lifetime, 90 days by default; past it the old
key is no longer used to encrypt, only to decrypt - test it thoroughly*; and *the old keys are never
deleted*.

The rules, pure:

* a key ENCRYPTS only while ``now < encrypt_until`` - one second past it, never again
  (:func:`may_encrypt`), and the command that encrypts checks it a second time on its own clock;
* a key DECRYPTS for ever - its lifetime says nothing about reading;
* the key a new file is encrypted with is the newest key whose lifetime is open
  (:func:`key_for_encryption`); none open means a new key is made, never an old one stretched;
* a key's lifetime is fixed when it is made (``created_at + days``) - changing the entry's days
  later changes the next key, never a key already used.

``pyrage`` (age) is imported where it is used (:func:`age`), so reading these rules never needs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from dbabrain.lib import errors

#: An encrypted backup on the repository: ``<the plain file's name>.age``.
SUFFIX = ".age"
ALGORITHM = "age-x25519"
DEFAULT_KEY_DAYS = 90
RECIPIENT = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")
IDENTITY = re.compile(r"^AGE-SECRET-KEY-1[02-9AC-HJ-NP-Z]{58}$")
_TIME = "%Y-%m-%dT%H:%M:%SZ"


class RepositoryKeyError(errors.ConfigError):
    """A key, a time or a lifetime that is not what the rules need."""


@dataclass(frozen=True)
class RepositoryKey:
    key_id: int
    server_id: str
    recipient: str
    created_at: datetime
    encrypt_until: datetime


def age():
    """``(pyrage, x25519)`` - or a refusal that names what to install, never a bare ImportError."""
    try:
        import pyrage
        from pyrage import x25519
    except ImportError as exc:
        raise RepositoryKeyError("age encryption is not installed here (the pyrage package) - "
                                 "pip install pyrage; nothing was encrypted or decrypted.") from exc
    return pyrage, x25519


def parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise RepositoryKeyError(f"{field} is required: a UTC time like 2026-11-10T06:00:00Z; got {value!r}.")
    try:
        return datetime.strptime(value.strip(), _TIME).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise RepositoryKeyError(f"{field} must be a UTC time like 2026-11-10T06:00:00Z; got {value!r}.") from exc


def time_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIME)


def check_days(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650:
        raise RepositoryKeyError(f"a key's lifetime is a whole number of days, 1 to 3650; got {value!r}.")
    return value


def encrypt_until(created_at: datetime, days: int) -> datetime:
    return created_at + timedelta(days=check_days(days))


def may_encrypt(until: datetime, now: datetime) -> bool:
    """A key encrypts strictly before its end - at the end and after, it only decrypts."""
    return now < until


def key_for_encryption(keys: Iterable[RepositoryKey], *, server_id: str, now: datetime) -> RepositoryKey | None:
    """The newest key of ``server_id`` still open for encrypting; ``None`` means make a new one."""
    open_keys = [k for k in keys if k.server_id == server_id and may_encrypt(k.encrypt_until, now)]
    return max(open_keys, key=lambda k: (k.created_at, k.key_id)) if open_keys else None


def key_of(rows: Iterable[dict[str, Any]]) -> list[RepositoryKey]:
    """Store rows as keys."""
    return [RepositoryKey(key_id=int(r["key_id"]), server_id=str(r["server_id"]), recipient=str(r["recipient"]),
                          created_at=parse_time(r["created_at"], "created_at"),
                          encrypt_until=parse_time(r["encrypt_until"], "encrypt_until")) for r in rows]
