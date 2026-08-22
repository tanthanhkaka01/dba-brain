"""Password hashing and session tokens for the web UI — pure functions, no store, no I/O.

This is ``lib``: values and rules, computed from the arguments. The store side lives in
:mod:`db_ops.db.web_auth_store` and the HTTP side in :mod:`db_ops.webhost.app`; both call in
here so there is exactly one answer to "is this the right password" and one answer to "what does
a session token look like".

**PBKDF2-HMAC-SHA256, 200 000 iterations**, matching what ``data/encrypted_secret_text.json``
already uses (:mod:`db_ops.lib.secret_text`). Two reasons not to reach for anything else: it is
in the standard library, so the web UI adds no dependency to an image that already ships five
database drivers; and the iteration count is stored *inside* each hash, so raising it later
re-hashes users on their next login instead of locking everyone out.

The encoded form is one self-describing string::

    pbkdf2_sha256$200000$<salt-b64>$<hash-b64>

so a column holding it needs no companion columns for the salt or the cost, and a hash written by
an older version stays verifiable after the parameters change.

**A session token is never stored.** :func:`new_session_token` returns the secret the browser
gets; the store keeps only :func:`token_fingerprint` of it. Anyone who can read the session table
— a backup, a replica, an operator running a SELECT — still cannot impersonate a logged-in user,
which is the difference between a session table and a table of passwords.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Imported rather than re-spelled: db_ops has **one** KDF setting, and the encrypted secret store
# is where it was first written down. Two copies of "200 000 iterations, 16-byte salt" is two
# settings that agree today, and the one that gets raised later is whichever the person happened
# to open — leaving passwords hashed at the old cost with nothing to say so.
from db_ops.lib.secret_text import _PBKDF2_ITERATIONS, _SALT_BYTES  # noqa: F401 - one definition

#: Cost of a new hash, read at call time by :func:`hash_password` so this is the single switch.
PBKDF2_ITERATIONS = _PBKDF2_ITERATIONS
#: Algorithm label written into the encoded form. Present so a future scheme can be told apart.
PBKDF2_SCHEME = "pbkdf2_sha256"
_HASH_BYTES = 32

#: How long a session lasts by default: three months. Long on purpose — this is an internal tool
#: whose users must not be logged out because they closed the browser, and the cookie carries the
#: same lifetime so it survives a restart of Chrome or Firefox.
DEFAULT_SESSION_DAYS = 90

#: Session tokens: 32 random bytes, URL-safe. Long enough that guessing is not a threat model.
_TOKEN_BYTES = 32

#: The permission ladder, deliberately the same shape as ``telegram_users.user_type``: 1..100,
#: higher is more. One scale for the whole tool means an operator does not have to learn a second
#: one to answer "may this person do that".
MIN_LEVEL = 1
MAX_LEVEL = 100


class WebAuthError(ValueError):
    """A credential or level cannot be honoured as given."""


def coerce_level(value: object, *, field: str = "level") -> int:
    """Validate a permission level, 1..100.

    Rejected rather than clamped: a config that says 0 or 500 means somebody had a different
    ladder in mind, and silently turning that into 1 or 100 grants or removes access nobody asked
    for.
    """
    try:
        level = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise WebAuthError(f"{field} must be a whole number from {MIN_LEVEL} to {MAX_LEVEL}.") from exc
    if not MIN_LEVEL <= level <= MAX_LEVEL:
        raise WebAuthError(
            f"{field} must be from {MIN_LEVEL} to {MAX_LEVEL}; got {level}.")
    return level


def normalize_username(value: object) -> str:
    """Lower-cased, trimmed. Usernames are an identity, not a display name.

    Case-folding here rather than in the store is what makes the unique index meaningful: without
    it ``Thanh`` and ``thanh`` are two active rows for one person, and whichever one they typed
    decides which permission level applies.
    """
    name = str(value or "").strip().lower()
    if not name:
        raise WebAuthError("username is required.")
    if len(name) > 64:
        raise WebAuthError("username must be at most 64 characters.")
    if any(char.isspace() for char in name):
        raise WebAuthError("username must not contain spaces.")
    return name


def check_password_quality(password: str) -> str:
    """The one rule: at least 8 characters. Returns the password so callers can chain it.

    Deliberately minimal. A long list of composition rules is not what protects this login —
    the KDF cost and the lockout are — and rules people work around with ``Password1!`` make the
    stored secret more predictable, not less.
    """
    text = str(password or "")
    if len(text) < 8:
        raise WebAuthError("password must be at least 8 characters.")
    return text


def hash_password(password: str, *, iterations: int | None = None,
                  salt: bytes | None = None) -> str:
    """Encode a password as ``pbkdf2_sha256$<iterations>$<salt>$<hash>``.

    The cost defaults to :data:`PBKDF2_ITERATIONS` **read at call time**, not bound when this
    function was defined. That is what makes the constant a single switch: raising it — or
    lowering it in a test that would otherwise spend its whole runtime in the KDF — takes effect
    everywhere without threading a parameter through every caller.

    ``salt`` is a parameter only so a test can produce a fixed value; every real call gets a fresh
    random one, which is what stops two users with the same password sharing a hash.
    """
    iterations = PBKDF2_ITERATIONS if iterations is None else int(iterations)
    text = check_password_quality(password)
    if iterations < 1000:
        raise WebAuthError("iterations must be at least 1000.")
    raw_salt = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", text.encode("utf-8"), raw_salt, int(iterations), _HASH_BYTES)
    return "$".join((
        PBKDF2_SCHEME,
        str(int(iterations)),
        base64.b64encode(raw_salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ))


def verify_password(password: str, encoded: str) -> bool:
    """Is ``password`` the one behind ``encoded``? Never raises — a malformed hash is a mismatch.

    The comparison is :func:`hmac.compare_digest`, not ``==``. A byte-at-a-time comparison leaks
    how much of the hash matched through its timing, and the whole point of storing a hash is that
    what the attacker gets from the table is useless.
    """
    try:
        scheme, iterations_text, salt_b64, hash_b64 = str(encoded).split("$")
        if scheme != PBKDF2_SCHEME:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", str(password or "").encode("utf-8"),
            base64.b64decode(salt_b64), int(iterations_text), _HASH_BYTES)
    except (ValueError, TypeError):
        return False
    try:
        return hmac.compare_digest(candidate, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def needs_rehash(encoded: str, *, iterations: int | None = None) -> bool:
    """Was ``encoded`` written with weaker parameters than we use now?

    Checked on a successful login, which is the only moment the plaintext is in hand to re-hash
    with. Raising the cost therefore costs nothing and locks nobody out.
    """
    target = PBKDF2_ITERATIONS if iterations is None else int(iterations)
    try:
        scheme, iterations_text, _salt, _hash = str(encoded).split("$")
    except ValueError:
        return True
    return scheme != PBKDF2_SCHEME or int(iterations_text) < target


def new_session_token() -> str:
    """A fresh session secret. Handed to the browser; never written to the store."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def new_csrf_token() -> str:
    """A per-session token for state-changing form posts.

    Separate from the session token because it is *not* secret in the same way: it is echoed back
    in page bodies, where a session token must never appear.
    """
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """SHA-256 hex of a token — what the store keeps in place of the token itself.

    Plain SHA-256 rather than a KDF, deliberately: the input is 32 bytes of system randomness, so
    there is no dictionary to run against it, and a session lookup happens on every request where
    a 200 000-iteration hash would be a per-request tax for no gain.
    """
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def tokens_match(candidate: str, expected: str) -> bool:
    """Constant-time comparison for CSRF tokens and other short-lived secrets."""
    return hmac.compare_digest(str(candidate or ""), str(expected or ""))
