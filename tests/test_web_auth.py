"""Login must be safe to expose and impossible to sidestep, and a session must outlive the browser.

The console is the first part of db_ops a person reaches without a shell, so the properties that
matter here are not the happy path — they are the ones that go wrong quietly:

* **A password is never stored, and a session token is never stored.** Whoever reads these tables
  from a backup or a replica must still be unable to log in as anybody.
* **A session survives closing the browser** for three months. That is the requirement, and it
  rests on one cookie attribute (`Max-Age`) that is easy to drop and whose absence looks like
  nothing until people start having to log in every morning.
* **Nothing past the login page is reachable without a session**, and an expired or revoked one
  stops working the moment it should — on its own timestamp, not when a sweeper happens to run.
* **The login form does not say who has an account.** Wrong password and unknown user must be
  indistinguishable, or the form becomes a way to enumerate the team.
* **The recoverable copy is a note, never a credential.** A console password is also kept in the
  encrypted secret store so an operator can look it up; the login path must never consult it, or
  the store becomes a second and softer way in.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from db_ops.db.web_auth_store import (
    recall_password,
    remember_password,
    secret_ref_for,
    REASON_BAD_PASSWORD,
    REASON_LOCKED,
    REASON_NO_USER,
    REASON_OK,
    WebAuthError,
    WebAuthStore,
    utc_now,
    utc_text,
)
from db_ops.lib import web_auth

PASSWORD = "correct-horse-battery"

#: Read at import, before the fixture below lowers it, so the production cost stays assertable.
PRODUCTION_ITERATIONS = web_auth.PBKDF2_ITERATIONS


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch):
    """Run the KDF at its floor for these tests.

    200 000 iterations is the production cost and is deliberately expensive; multiplied by every
    account and login below it is most of this file's runtime, spent proving nothing. The cost is
    read from the module at call time, so lowering it here changes only what these tests spend —
    and the cost written into each hash is asserted separately against the real constant.
    """
    monkeypatch.setattr(web_auth, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture()
def auth(tmp_path: Path) -> WebAuthStore:
    return WebAuthStore(tmp_path / "db_ops.sqlite")


@pytest.fixture()
def user(auth: WebAuthStore) -> int:
    return auth.create_user(username="Thanh", password=PASSWORD, level=100,
                            display_name="Trieu Tan Thanh", actor="test")


# --------------------------------------------------------------------------- #
# Password handling
# --------------------------------------------------------------------------- #
def test_a_password_is_stored_only_as_a_pbkdf2_hash(auth: WebAuthStore, user: int) -> None:
    row = auth.get_user_by_id(user)
    stored = str(row["password_hash"])
    assert PASSWORD not in stored
    assert stored.startswith("pbkdf2_sha256$")
    scheme, iterations, salt, digest = stored.split("$")
    assert int(iterations) == web_auth.PBKDF2_ITERATIONS
    assert salt and digest


def test_the_production_cost_is_the_one_the_secret_store_already_uses() -> None:
    """Held against the value read at import, so the cheap-KDF fixture cannot hide a change.

    The number matters: it is what makes a stolen hash table expensive to attack, and one KDF
    setting for the whole tool is why it has to be the secret store's number and not a second one.
    """
    from db_ops.lib import secret_text

    assert PRODUCTION_ITERATIONS == 200_000
    assert PRODUCTION_ITERATIONS == secret_text._PBKDF2_ITERATIONS


def test_the_same_password_twice_produces_two_different_hashes() -> None:
    """A shared hash would tell an attacker which accounts share a password."""
    assert web_auth.hash_password(PASSWORD) != web_auth.hash_password(PASSWORD)
    assert web_auth.verify_password(PASSWORD, web_auth.hash_password(PASSWORD))


def test_a_malformed_hash_is_a_mismatch_not_a_crash() -> None:
    """A corrupt row must fail the login, not take the login page down with it."""
    for broken in ("", "nonsense", "pbkdf2_sha256$abc", "argon2$1$a$b", "pbkdf2_sha256$x$y$z"):
        assert web_auth.verify_password(PASSWORD, broken) is False


def test_a_hash_written_with_a_weaker_cost_is_upgraded_on_the_next_login(auth: WebAuthStore,
                                                                         monkeypatch) -> None:
    """Raising the KDF cost must not lock anyone out, so it happens when the plaintext is in hand."""
    auth.create_user(username="olduser", password=PASSWORD, level=10)
    weak = web_auth.hash_password(PASSWORD, iterations=1000)
    # Raise the bar *after* the weak hash exists — this is exactly the sequence a real cost
    # increase produces: rows written under the old number, a new number in force.
    monkeypatch.setattr(web_auth, "PBKDF2_ITERATIONS", 2000)
    with auth.connect() as conn:
        conn.execute("UPDATE web_users SET password_hash = ? WHERE username = ?", (weak, "olduser"))
    assert web_auth.needs_rehash(weak) is True

    row, reason = auth.authenticate(username="olduser", password=PASSWORD)
    assert reason == REASON_OK and row is not None
    assert web_auth.needs_rehash(str(row["password_hash"])) is False


def test_a_short_password_is_refused(auth: WebAuthStore) -> None:
    with pytest.raises(web_auth.WebAuthError):
        auth.create_user(username="tiny", password="abc", level=1)


@pytest.mark.parametrize("level", [0, -1, 101, 1000, "high", None])
def test_a_level_outside_one_to_a_hundred_is_refused(auth: WebAuthStore, level) -> None:
    """Clamping would silently grant or remove access nobody asked for."""
    with pytest.raises(web_auth.WebAuthError):
        auth.create_user(username=f"u{level}", password=PASSWORD, level=level)


def test_a_username_is_an_identity_and_case_does_not_make_a_second_one(auth: WebAuthStore,
                                                                      user: int) -> None:
    assert auth.get_user("THANH") is not None
    with pytest.raises(WebAuthError):
        auth.create_user(username="THANH", password=PASSWORD, level=1)


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def test_the_right_password_authenticates_and_the_wrong_one_does_not(auth: WebAuthStore,
                                                                     user: int) -> None:
    row, reason = auth.authenticate(username="thanh", password=PASSWORD)
    assert reason == REASON_OK and int(row["web_user_id"]) == user

    row, reason = auth.authenticate(username="thanh", password="not-the-password")
    assert row is None and reason == REASON_BAD_PASSWORD


def test_an_unknown_user_and_a_wrong_password_are_recorded_apart_but_answered_alike(
        auth: WebAuthStore, user: int) -> None:
    """The reason is for the audit table; the browser gets one message for both — see app.py."""
    _, unknown = auth.authenticate(username="nobody", password=PASSWORD)
    _, wrong = auth.authenticate(username="thanh", password="wrong-password")
    assert unknown == REASON_NO_USER and wrong == REASON_BAD_PASSWORD
    assert {row["reason"] for row in auth.recent_attempts()} >= {REASON_NO_USER, REASON_BAD_PASSWORD}


def test_repeated_failures_lock_the_account_and_a_lockout_outranks_the_right_password(
        auth: WebAuthStore, user: int) -> None:
    for _ in range(3):
        auth.authenticate(username="thanh", password="wrong", max_failed=3, lockout_minutes=15)
    row, reason = auth.authenticate(username="thanh", password=PASSWORD, max_failed=3)
    assert row is None and reason == REASON_LOCKED, (
        "a locked account must stay shut even for the correct password, or the lockout is decorative")


def test_a_successful_login_clears_the_failure_count(auth: WebAuthStore, user: int) -> None:
    auth.authenticate(username="thanh", password="wrong", max_failed=8)
    auth.authenticate(username="thanh", password=PASSWORD, max_failed=8)
    row = auth.get_user_by_id(user)
    assert int(row["failed_login_count"]) == 0
    assert row["locked_until"] is None
    assert row["last_login_at"]


def test_every_attempt_is_recorded_with_who_and_from_where(auth: WebAuthStore, user: int) -> None:
    auth.authenticate(username="thanh", password=PASSWORD, client_ip="10.0.0.9",
                      user_agent="Firefox/1")
    attempt = auth.recent_attempts(limit=1)[0]
    assert attempt["client_ip"] == "10.0.0.9"
    assert attempt["user_agent"] == "Firefox/1"
    assert int(attempt["succeeded"]) == 1


def test_a_disabled_account_cannot_log_in_and_keeps_its_row(auth: WebAuthStore, user: int) -> None:
    auth.deactivate_user(username="thanh", actor="test", note="left the team")
    row, reason = auth.authenticate(username="thanh", password=PASSWORD)
    assert row is None and reason == REASON_NO_USER

    kept = auth.get_user_by_id(user)
    assert kept is not None and int(kept["is_active"]) == 0
    assert kept["deactivated_at"] and kept["note"] == "left the team"


def test_a_disabled_username_can_be_issued_to_someone_else(auth: WebAuthStore, user: int) -> None:
    """The partial unique index, same rule as config: the row stays, the name is freed."""
    auth.deactivate_user(username="thanh", actor="test")
    new_id = auth.create_user(username="thanh", password="a-different-password", level=1)
    assert new_id != user
    assert int(auth.get_user("thanh")["web_user_id"]) == new_id
    assert len(auth.list_users(include_inactive=True)) == 2


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def test_the_session_token_is_never_written_to_the_store(auth: WebAuthStore, user: int) -> None:
    issued = auth.issue_session(web_user_id=user)
    with auth.connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM web_sessions")]
    stored = json.dumps(rows)
    assert issued["token"] not in stored, "the session table must not hold the token itself"
    assert web_auth.token_fingerprint(issued["token"]) in stored


def test_a_session_lasts_three_months_by_default(auth: WebAuthStore, user: int) -> None:
    """The requirement: log in once, stay logged in — the cookie carries the same span."""
    issued = auth.issue_session(web_user_id=user)
    days = issued["max_age_seconds"] / 86400
    assert 89.9 < days < 90.1
    assert web_auth.DEFAULT_SESSION_DAYS == 90


def test_a_session_resolves_to_its_user_and_level(auth: WebAuthStore, user: int) -> None:
    issued = auth.issue_session(web_user_id=user)
    session = auth.resolve_session(issued["token"])
    assert session["username"] == "thanh"
    assert session["user_level"] == 100
    assert session["csrf_token"]


def test_a_wrong_or_empty_token_resolves_to_nothing(auth: WebAuthStore, user: int) -> None:
    auth.issue_session(web_user_id=user)
    assert auth.resolve_session("") is None
    assert auth.resolve_session("not-a-real-token") is None


def test_an_expired_session_stops_working_and_is_retired_on_sight(auth: WebAuthStore,
                                                                  user: int) -> None:
    """Expiry is a property of the timestamp, so it holds with no sweeper job running."""
    issued = auth.issue_session(web_user_id=user)
    with auth.connect() as conn:
        conn.execute("UPDATE web_sessions SET expires_at = ? WHERE web_session_id = ?",
                     (utc_text(utc_now() - timedelta(minutes=1)), issued["web_session_id"]))

    assert auth.resolve_session(issued["token"]) is None
    with auth.connect() as conn:
        row = conn.execute("SELECT is_active, revoked_reason FROM web_sessions "
                           "WHERE web_session_id = ?", (issued["web_session_id"],)).fetchone()
    assert int(row["is_active"]) == 0 and row["revoked_reason"] == "expired"


def test_logging_out_revokes_that_session_only(auth: WebAuthStore, user: int) -> None:
    phone = auth.issue_session(web_user_id=user)
    laptop = auth.issue_session(web_user_id=user)
    assert auth.revoke_session(phone["token"]) is True
    assert auth.resolve_session(phone["token"]) is None
    assert auth.resolve_session(laptop["token"]) is not None


def test_disabling_an_account_ends_its_sessions(auth: WebAuthStore, user: int) -> None:
    issued = auth.issue_session(web_user_id=user)
    auth.deactivate_user(username="thanh", actor="test")
    assert auth.resolve_session(issued["token"]) is None, (
        "a three-month cookie must not outlive the account it belongs to")


def test_changing_a_password_ends_every_session_by_default(auth: WebAuthStore, user: int) -> None:
    """A password is changed because it leaked or somebody left; a live cookie undoes both."""
    issued = auth.issue_session(web_user_id=user)
    auth.set_password(username="thanh", password="a-brand-new-password", actor="test")
    assert auth.resolve_session(issued["token"]) is None

    kept = auth.issue_session(web_user_id=user)
    auth.set_password(username="thanh", password="another-new-password", actor="test",
                      revoke_sessions=False)
    assert auth.resolve_session(kept["token"]) is not None


def test_resolving_a_session_records_that_it_was_seen(auth: WebAuthStore, user: int) -> None:
    issued = auth.issue_session(web_user_id=user)
    with auth.connect() as conn:
        conn.execute("UPDATE web_sessions SET last_seen_at = ? WHERE web_session_id = ?",
                     (utc_text(utc_now() - timedelta(days=2)), issued["web_session_id"]))
    auth.resolve_session(issued["token"])
    with auth.connect() as conn:
        row = conn.execute("SELECT last_seen_at FROM web_sessions WHERE web_session_id = ?",
                           (issued["web_session_id"],)).fetchone()
    assert str(row["last_seen_at"]) > utc_text(utc_now() - timedelta(minutes=5))


def test_two_sessions_cannot_share_a_token_fingerprint(auth: WebAuthStore, user: int) -> None:
    """The uniqueness is the database's, so a bug that reused a token fails loudly."""
    import sqlite3

    issued = auth.issue_session(web_user_id=user)
    with pytest.raises(sqlite3.IntegrityError):
        with auth.connect() as conn:
            conn.execute(
                "INSERT INTO web_sessions (web_user_id, token_fingerprint, issued_at, "
                "expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (user, web_auth.token_fingerprint(issued["token"]), utc_text(utc_now()),
                 utc_text(utc_now()), utc_text(utc_now())),
            )


def test_has_any_user_answers_whether_the_console_is_usable_yet(tmp_path: Path) -> None:
    store = WebAuthStore(tmp_path / "empty.sqlite")
    assert store.has_any_user() is False
    store.create_user(username="first", password=PASSWORD, level=100)
    assert store.has_any_user() is True


# --------------------------------------------------------------------------- #
# The recoverable copy
# --------------------------------------------------------------------------- #
def _secret_dir(tmp_path: Path) -> Path:
    """A secret store with one existing ref, so the tests also prove nothing else is lost."""
    from db_ops.lib import secret_text

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    blob = secret_text.encrypt_secret_text({"EXISTING_REF": "keep-me"}, "passphrase")
    (data / "encrypted_secret_text.json").write_text(
        json.dumps(blob, indent=2), encoding="utf-8")
    return data


def test_a_secret_ref_is_derived_from_the_username() -> None:
    """Predictable from a psql prompt and from a runbook, rather than looked up."""
    assert secret_ref_for("Thanh") == "WEB_CONSOLE_THANH"
    assert secret_ref_for("an.nguyen-2") == "WEB_CONSOLE_AN_NGUYEN_2"


def test_a_remembered_password_can_be_read_back(tmp_path: Path) -> None:
    data = _secret_dir(tmp_path)
    ref = remember_password("thanh", PASSWORD, data_dir=data, key="passphrase",
                            plaintext_store=tmp_path / "absent.json")

    assert ref == "WEB_CONSOLE_THANH"
    assert recall_password("thanh", data_dir=data, key="passphrase") == PASSWORD


def test_remembering_a_password_keeps_every_other_secret(tmp_path: Path) -> None:
    """The whole store is decrypted, updated and re-encrypted; losing a ref here loses a login."""
    from db_ops.lib import secret_text

    data = _secret_dir(tmp_path)
    remember_password("thanh", PASSWORD, data_dir=data, key="passphrase",
                      plaintext_store=tmp_path / "absent.json")
    secrets = secret_text.load_secret_text(data, key="passphrase")
    assert secrets["EXISTING_REF"] == "keep-me"
    assert secrets["WEB_CONSOLE_THANH"] == PASSWORD


def test_the_plaintext_source_is_updated_too_when_it_exists(tmp_path: Path) -> None:
    """The deploy regenerates the encrypted store from this file; writing one alone is undone."""
    data = _secret_dir(tmp_path)
    source = tmp_path / "secret_text.json"
    source.write_text(json.dumps({"EXISTING_REF": "keep-me"}), encoding="utf-8")

    remember_password("thanh", PASSWORD, data_dir=data, key="passphrase", plaintext_store=source)
    written = json.loads(source.read_text(encoding="utf-8"))
    assert written["WEB_CONSOLE_THANH"] == PASSWORD
    assert written["EXISTING_REF"] == "keep-me"


def test_an_absent_plaintext_source_is_not_an_error(tmp_path: Path) -> None:
    """The worker carries the encrypted store only; a write there is picked up by the merge."""
    data = _secret_dir(tmp_path)
    remember_password("thanh", PASSWORD, data_dir=data, key="passphrase",
                      plaintext_store=tmp_path / "not-here.json")
    assert recall_password("thanh", data_dir=data, key="passphrase") == PASSWORD


def test_recalling_a_password_that_was_never_kept_is_empty(tmp_path: Path) -> None:
    """An account created with --no-remember has a hash and nothing else. That is the answer."""
    data = _secret_dir(tmp_path)
    assert recall_password("nobody", data_dir=data, key="passphrase") == ""


def test_the_account_row_points_at_the_secret_that_holds_its_password(auth: WebAuthStore) -> None:
    auth.create_user(username="thanh", password=PASSWORD, level=100,
                     password_ref=secret_ref_for("thanh"))
    row = auth.get_user("thanh")
    assert row["password_ref"] == "WEB_CONSOLE_THANH"
    assert PASSWORD not in str(row["password_hash"]), (
        "the row still holds a hash; the ref only says where the readable copy lives")


def test_authenticating_never_consults_the_secret_store(tmp_path: Path, monkeypatch) -> None:
    """The hash is the credential. A fallback here would make the secret store a second way in.

    Proved by making the secret store explode: a login that touched it could not pass.
    """
    def explode(*args, **kwargs):
        raise AssertionError("authenticate must not read the secret store")

    monkeypatch.setattr("db_ops.db.web_auth_store.recall_password", explode)
    store = WebAuthStore(tmp_path / "db_ops.sqlite")
    store.create_user(username="thanh", password=PASSWORD, level=100,
                      password_ref="WEB_CONSOLE_THANH")

    row, reason = store.authenticate(username="thanh", password=PASSWORD)
    assert reason == REASON_OK and row is not None
    row, reason = store.authenticate(username="thanh", password="wrong")
    assert row is None and reason == REASON_BAD_PASSWORD
