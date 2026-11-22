"""A dbabrain key decides whether a node makes reports and backups (1.0.0, plan K2).

Getting this wrong either way is costly: a forged or changed key that is read as valid gives away
what the key is for, and a valid key read as ended stops a schedule of backups - a restore that will
not exist. So the tests hold the signature (one changed character is refused), the term the operator
set (warnings 14/7/3/1, a 3-day grace, then new work stops while an asked-for restore still runs),
and the format shared with the Worker: ``tests/fixtures/dbabrain_key_vector.json`` was signed by
``connector/worker/src/key-token.ts`` and is read here byte for byte.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from dbabrain.lib import dbabrain_key as dk

VECTOR = json.loads((Path(__file__).parent / "fixtures" / "dbabrain_key_vector.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Signer:
    """What the Worker does, done here - so a test can make any key it needs."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.x = b64(self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
        self.skid = dk.skid_of(self.x)
        self.keys = {self.skid: self.x}

    def token(self, *, days: float = 14, issued: datetime = NOW, **fields) -> str:
        payload = {"v": 1, "kid": "k-1", "sub": "a@example.com", "plan": "free",
                   "features": list(dk.FEATURES), "iat": int(issued.timestamp()),
                   "exp": int((issued + timedelta(days=days)).timestamp()), "node": "n", "skid": self.skid}
        payload.update(fields)
        body = f"{dk.PREFIX}.{b64(json.dumps(payload).encode())}"
        return f"{body}.{b64(self.private.sign(body.encode()))}"


@pytest.fixture
def signer() -> Signer:
    return Signer()


def test_the_workers_vector_reads_here_as_it_was_signed():
    key = dk.parse_key(VECTOR["token"], public_keys={VECTOR["skid"]: VECTOR["public_x"]})

    assert key.email == VECTOR["payload"]["sub"]
    assert key.kid == VECTOR["payload"]["kid"]
    assert key.features == tuple(VECTOR["payload"]["features"])
    assert key.expires_at - key.issued_at == timedelta(days=14)


def test_the_features_are_the_ones_the_worker_hands_out():
    assert list(dk.FEATURES) == VECTOR["payload"]["features"]


def test_a_skid_is_the_hash_of_its_public_key():
    assert dk.skid_of(VECTOR["public_x"]) == VECTOR["skid"]


def test_one_changed_character_in_the_payload_is_refused():
    prefix, body, signature = VECTOR["token"].split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["exp"] += 365 * 86400
    forged = f"{prefix}.{b64(json.dumps(payload).encode())}.{signature}"

    with pytest.raises(dk.KeyInvalid, match="signature does not match"):
        dk.parse_key(forged, public_keys={VECTOR["skid"]: VECTOR["public_x"]})


def test_a_key_signed_by_an_unknown_signing_key_is_refused_by_name(signer):
    with pytest.raises(dk.KeyInvalid, match="does not know"):
        dk.parse_key(signer.token(), public_keys={})


def test_a_public_key_listed_under_another_skid_is_not_trusted(signer):
    other = Signer()
    token = other.token()

    with pytest.raises(dk.KeyInvalid, match="does not know"):
        dk.parse_key(token, public_keys={other.skid: signer.x})


@pytest.mark.parametrize("text", ["", "abc", "dbbk1.x.y", "dbbk2.e30.AA", "Bearer dbbk1.a.b"])
def test_anything_that_is_not_a_key_says_so(text):
    with pytest.raises(dk.KeyInvalid):
        dk.parse_key(text, public_keys={})


def test_no_key_and_a_bad_key_are_two_states():
    assert dk.standing(None, now=NOW).state == "none"
    assert dk.standing("nonsense", now=NOW, public_keys={}).state == "invalid"


def test_the_term_then_three_days_of_grace_then_ended(signer):
    token = signer.token()

    def state(days):
        return dk.standing(token, now=NOW + timedelta(days=days), public_keys=signer.keys).state

    assert state(13.9) == "active"
    assert state(14) == "grace"
    assert state(16.9) == "grace"
    assert state(17) == "ended"


@pytest.mark.parametrize(("day", "due"), [(0, None), (6.5, None), (7.5, 7), (11.5, 3), (13.2, 1), (13.99, 1)])
def test_a_14_day_key_is_warned_at_7_3_and_1_days_left_never_on_its_first_day(signer, day, due):
    s = dk.standing(signer.token(), now=NOW + timedelta(days=day), public_keys=signer.keys)

    assert s.warning_due == due


@pytest.mark.parametrize(("left", "due"), [(20, None), (14, 14), (6, 7), (2.5, 3), (0.5, 1)])
def test_a_longer_key_is_warned_at_14_7_3_and_1_days_left(signer, left, due):
    token = signer.token(days=365)

    s = dk.standing(token, now=NOW + timedelta(days=365 - left), public_keys=signer.keys)

    assert s.warning_due == due


def test_a_years_key_warns_nothing_until_its_last_two_weeks(signer):
    token = signer.token(days=365)

    assert dk.standing(token, now=NOW + timedelta(days=300), public_keys=signer.keys).warning_due is None
    assert dk.standing(token, now=NOW + timedelta(days=352), public_keys=signer.keys).warning_due == 14


def test_without_a_key_metrics_run_and_reports_and_backups_do_not():
    none = dk.standing(None, now=NOW)

    assert dk.may_run(none, "metrics").allowed
    assert dk.may_run(none, "alerts").allowed
    assert dk.may_run(none, "sql_tasks").allowed
    assert not dk.may_run(none, "reports").allowed
    assert not dk.may_run(none, "backup_restore").allowed
    assert "dbabrain key-request" in dk.may_run(none, "reports").sentence


def test_in_the_grace_everything_runs_and_says_so_critical(signer):
    s = dk.standing(signer.token(), now=NOW + timedelta(days=15), public_keys=signer.keys)

    decision = dk.may_run(s, "backup_restore")

    assert decision.allowed and decision.level == "critical"
    assert "dbabrain key-renew" in decision.sentence


def test_after_the_grace_new_backups_stop_and_an_asked_for_restore_still_runs(signer):
    s = dk.standing(signer.token(), now=NOW + timedelta(days=18), public_keys=signer.keys)

    assert not dk.may_run(s, "backup_restore").allowed
    asked = dk.may_run(s, "backup_restore", new_work=False)
    assert asked.allowed and asked.level == "critical"


def test_a_restore_asked_for_on_a_node_without_a_key_is_not_stranded():
    decision = dk.may_run(dk.standing(None, now=NOW), "backup_restore", new_work=False)

    assert decision.allowed and decision.level == "critical"


def test_a_revocation_heard_online_ends_the_key_at_once(signer):
    s = dk.standing(signer.token(), now=NOW, online={"kid": "k-1", "state": "revoked"}, public_keys=signer.keys)

    assert s.state == "revoked"
    assert not dk.may_run(s, "reports").allowed


def test_an_online_answer_about_another_key_changes_nothing(signer):
    s = dk.standing(signer.token(), now=NOW, online={"kid": "an-older-key", "state": "replaced"}, public_keys=signer.keys)

    assert s.state == "active"


def test_a_failed_online_check_is_not_a_revocation(signer):
    s = dk.standing(signer.token(), now=NOW, online=None, public_keys=signer.keys)

    assert s.state == "active" and dk.may_run(s, "reports").allowed


def test_a_feature_outside_the_keys_plan_is_refused(signer):
    s = dk.standing(signer.token(features=["reports"]), now=NOW, public_keys=signer.keys)

    assert dk.may_run(s, "reports").allowed
    assert not dk.may_run(s, "ai_relay").allowed


def test_the_last_day_is_critical_and_the_days_before_a_warning(signer):
    token = signer.token()

    last = dk.may_run(dk.standing(token, now=NOW + timedelta(days=13.5), public_keys=signer.keys), "reports")
    week = dk.may_run(dk.standing(token, now=NOW + timedelta(days=8), public_keys=signer.keys), "reports")

    assert last.level == "critical"
    assert week.level == "warning"


def test_a_standing_describes_itself_without_the_token(signer):
    token = signer.token()

    described = json.dumps(dk.standing(token, now=NOW, public_keys=signer.keys).describe())

    assert token.split(".")[2] not in described
    assert '"state": "active"' in described
