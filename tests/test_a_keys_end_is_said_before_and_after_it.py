"""A key's end is said before it and every day after it - once each, never every scan (1.0.0, K5).

Backups stopping because a key ran out is the most dangerous thing a DBA tool can do, so the warning
is the feature: 14, 7, 3 and 1 day(s) before the end (the last CRITICAL), CRITICAL every day of the
3-day grace, CRITICAL every day once new work has stopped. Said once per step or per day - a
scheduler scans every few seconds, and a warning repeated each scan is one nobody reads. What was
said is kept beside data/, so a restart does not say a step again.

The key is checked online once a day (D5); a check that fails is tried again tomorrow, not every
scan, and changes nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dbabrain.jobs import key_watch
from dbabrain.jobs.key_gate import KeyGate
from dbabrain.lib import dbabrain_key as dk
from dbabrain.lib import secret_text

from tests.test_dbabrain_key import Signer

pytestmark = pytest.mark.real_key_gate

PASSPHRASE = "a-throwaway-passphrase-for-this-test-only"
NOW = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, PASSPHRASE)
    data = tmp_path / "data"
    data.mkdir()
    signer = Signer()
    monkeypatch.setattr(dk, "SIGNING_PUBLIC_KEYS", signer.keys)
    secret_text.set_secret_text(data, dk.KEY_REF, signer.token(issued=NOW), key=PASSPHRASE)
    return data


def _scan(node, when, said, *, check=None):
    gate = KeyGate.for_scan(node, now=when)
    return key_watch.watch(node, gate, now=when, push=lambda level, text: said.append((level, text)),
                           check_online=check or (lambda: None), regate=lambda: KeyGate.for_scan(node, now=when))


def test_nothing_is_said_while_more_than_14_days_are_left(tmp_path, monkeypatch):
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, PASSPHRASE)
    data = tmp_path / "data"
    data.mkdir()
    signer = Signer()
    monkeypatch.setattr(dk, "SIGNING_PUBLIC_KEYS", signer.keys)
    secret_text.set_secret_text(data, dk.KEY_REF, signer.token(issued=NOW, days=365), key=PASSPHRASE)
    said: list = []

    _scan(data, NOW, said)

    assert said == []


def test_each_step_before_the_end_is_said_once(node):
    said: list = []
    for day in (0, 0.1, 0.2, 7.5, 7.6, 11.5, 13.2, 13.3):
        _scan(node, NOW + timedelta(days=day), said)

    assert [level for level, _ in said] == ["warning", "warning", "critical"]
    assert "ends in 6 day(s)" in said[0][1] and "dbabrain key-renew" in said[0][1]


def test_a_new_14_day_key_is_not_warned_the_day_it_arrives(node):
    said: list = []

    for hours in (0, 1, 24, 72):
        _scan(node, NOW + timedelta(hours=hours), said)

    assert said == [], "warned of its end the day it was issued - the 2.87.111 worker's first hour"


def test_a_restart_does_not_say_a_step_twice(node):
    said: list = []
    _scan(node, NOW + timedelta(days=8), said)

    _scan(node, NOW + timedelta(days=8, hours=2), said)  # the same files, as a new process finds them

    assert len(said) == 1


def test_the_grace_is_said_critical_once_a_day(node):
    said: list = []
    for hours in (0, 1, 5, 24, 25, 48):
        _scan(node, NOW + timedelta(days=14, hours=hours), said)

    assert [level for level, _ in said] == ["critical", "critical", "critical"]
    assert "still run until" in said[0][1]


def test_after_the_grace_the_stop_is_said_every_day(node):
    said: list = []
    for day in (18, 18.2, 19, 20):
        _scan(node, NOW + timedelta(days=day), said)

    assert len(said) == 3
    assert all("STOPPED" in text and "restore you ask for still runs" in text for _, text in said)


def test_the_key_is_checked_online_once_a_day_even_when_the_check_fails(node):
    tries: list[int] = []

    def check():
        tries.append(1)
        raise OSError("no route to host")

    said: list = []
    for hours in (0, 1, 2, 3):
        _scan(node, NOW + timedelta(hours=hours), said, check=check)
    _scan(node, NOW + timedelta(days=1), said, check=check)

    assert len(tries) == 2


def test_a_revocation_learnt_by_the_days_check_is_used_by_that_scan(node):
    def check():
        status = dk.status_file(node)
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text('{"kid": "k-1", "state": "revoked"}', encoding="utf-8")

    said: list = []
    gate = _scan(node, NOW + timedelta(days=1), said, check=check)

    assert gate.standing.state == "revoked"
    assert said and "STOPPED" in said[-1][1]


def test_a_node_without_a_key_is_not_told_every_day(tmp_path, monkeypatch):
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, PASSPHRASE)
    data = tmp_path / "data"
    data.mkdir()
    said: list = []
    checked: list = []

    for day in range(3):
        _scan(data, NOW + timedelta(days=day), said, check=lambda: checked.append(1))

    assert said == [] and checked == []
