"""One address cannot lock somebody else's console account, and an expired lock starts from zero.

The console's lockout was per account only (review 0.25.0, B3.3):

* **Eight wrong passwords from anyone locked any account** - the only admin's included - for fifteen
  minutes, repeatably: a denial of service for the price of a loop. An address is now refused after
  ``max_failed_logins_per_ip`` failures in the window, below the account's limit, and a refused
  attempt adds nothing to the account.
* **The count outlived the lock.** ``failed_login_count`` was a running total that the lock's expiry
  did not reset, so one more miss relocked at once - and an address held below the limit could still
  lock the account slowly, a few misses per window. Both limits now count failures inside the window.
* **The locked path skipped the password check**, so its speed said "this account exists and is
  locked". It costs the same check as every other failure.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from db_ops.db import web_auth_store
from db_ops.db.web_auth_store import (
    REASON_BAD_PASSWORD,
    REASON_IP_THROTTLED,
    REASON_LOCKED,
    REASON_OK,
    WebAuthStore,
)
from db_ops.lib import web_auth

PASSWORD = "correct-horse-battery"
ATTACKER = "198.51.100.66"
OPERATOR = "192.0.2.15"


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch):
    monkeypatch.setattr(web_auth, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture()
def auth(tmp_path: Path) -> WebAuthStore:
    store = WebAuthStore(tmp_path / "db_ops.sqlite")
    store.create_user(username="admin1", password=PASSWORD, level=100, actor="test")
    return store


def _try(auth: WebAuthStore, password: str, ip: str, **limits):
    return auth.authenticate(username="admin1", password=password, client_ip=ip, **limits)


def test_one_address_is_refused_before_it_can_lock_the_account(auth):
    reasons = [_try(auth, "wrong", ATTACKER)[1] for _ in range(20)]

    assert reasons[:5] == [REASON_BAD_PASSWORD] * 5
    assert set(reasons[5:]) == {REASON_IP_THROTTLED}
    row, reason = _try(auth, PASSWORD, OPERATOR)
    assert reason == REASON_OK and row is not None


def test_knocking_on_a_refused_address_does_not_keep_it_refused(auth, monkeypatch):
    """A refusal is not a failure. Counted as one, five retries while refused bought fifteen more
    minutes each time, for everyone behind that address - the admin at the same proxy included."""
    clock = {"now": web_auth_store.utc_now()}
    monkeypatch.setattr(web_auth_store, "utc_now", lambda: clock["now"])
    for _ in range(5):
        _try(auth, "wrong", ATTACKER)
    for _minute in range(10):
        clock["now"] += timedelta(minutes=1)
        assert _try(auth, PASSWORD, ATTACKER)[1] == REASON_IP_THROTTLED

    clock["now"] += timedelta(minutes=6)

    assert _try(auth, PASSWORD, ATTACKER)[1] == REASON_OK, "sixteen minutes after its last real miss"


def test_an_address_held_below_the_limit_cannot_lock_the_account_slowly(auth, monkeypatch):
    clock = {"now": web_auth_store.utc_now()}
    monkeypatch.setattr(web_auth_store, "utc_now", lambda: clock["now"])
    for _window in range(3):
        for _ in range(5):
            _try(auth, "wrong", ATTACKER)
        clock["now"] += timedelta(minutes=16)

    assert _try(auth, PASSWORD, OPERATOR)[1] == REASON_OK


def test_a_lock_that_expired_does_not_relock_on_the_next_miss(auth, monkeypatch):
    clock = {"now": web_auth_store.utc_now()}
    monkeypatch.setattr(web_auth_store, "utc_now", lambda: clock["now"])
    for _ in range(3):
        _try(auth, "wrong", OPERATOR, max_failed=3, max_failed_per_ip=0)
    assert _try(auth, PASSWORD, OPERATOR, max_failed=3, max_failed_per_ip=0)[1] == REASON_LOCKED

    clock["now"] += timedelta(minutes=16)

    assert _try(auth, "wrong", OPERATOR, max_failed=3, max_failed_per_ip=0)[1] == REASON_BAD_PASSWORD
    assert _try(auth, PASSWORD, OPERATOR, max_failed=3, max_failed_per_ip=0)[1] == REASON_OK


def test_a_locked_account_costs_the_same_password_check(auth, monkeypatch):
    for _ in range(3):
        _try(auth, "wrong", OPERATOR, max_failed=3, max_failed_per_ip=0)
    checks: list[str] = []
    real = web_auth.verify_password
    monkeypatch.setattr(web_auth, "verify_password", lambda pw, h: checks.append(h) or real(pw, h))

    assert _try(auth, "wrong", OPERATOR, max_failed=3, max_failed_per_ip=0)[1] == REASON_LOCKED
    assert len(checks) == 1
