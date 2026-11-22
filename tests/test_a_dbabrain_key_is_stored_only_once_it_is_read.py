"""A dbabrain key from dbabrain.dev is kept only once it is read, and its standing is said (1.0.0, K3).

`key-request` is a device sign-in, and the key it ends with has already replaced the account's
earlier key on dbabrain.dev when it arrives. So the order is held here, against a fake dbabrain.dev:
the store must open BEFORE anything is asked (a key handed over and then not storable is lost), the
key must READ before it is kept (one signed by a key this release does not know would fail every
gate later), and it is read back from the store before the command says it is done.

`key-status` says the standing offline and keeps dbabrain.dev's answer for the gate - a revoked key
ends at once, and a check that could not reach dbabrain.dev changes nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dbabrain.common import dbabrain_key_admin as admin
from dbabrain.lib import dbabrain_key as dk
from dbabrain.lib import errors
from dbabrain.lib import secret_text

from tests.test_dbabrain_key import Signer

PASSPHRASE = "a-throwaway-passphrase-for-this-test-only"
NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


class FakeDev:
    """dbabrain.dev's device sign-in and key status, answering by the path asked."""

    def __init__(self, signer: Signer, *, approve_after: int = 1, key: str | None = None) -> None:
        self.signer = signer
        self.key = key if key is not None else signer.token(issued=NOW)
        self.polls_left = approve_after
        self.asked: list[tuple[str, str, dict | None, str]] = []
        self.status_answer: tuple[int, dict] = (200, {"kid": "k-1", "state": "active"})

    def __call__(self, method, url, *, body=None, bearer="", timeout=30):
        self.asked.append((method, url, body, bearer))
        if url.endswith("/node/link/start"):
            return 200, {"device_code": "dev-1", "user_code": "ABCD-EFGH", "interval": 2, "expires_in": 600,
                         "verification_uri": "https://dev.example/link?code=ABCD-EFGH"}
        if url.endswith("/node/link/poll"):
            self.polls_left -= 1
            if self.polls_left > 0:
                return 428, {"error": "authorization_pending"}
            return 200, {"email": "a@example.com", "dbabrain_key": self.key}
        if url.endswith("/key/status"):
            if isinstance(self.status_answer, Exception):
                raise self.status_answer
            return self.status_answer
        raise AssertionError(url)


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, PASSPHRASE)
    data = tmp_path / "data"
    data.mkdir()
    return data


def _request(data, fake, **kwargs):
    return admin.request_key(data, http=fake, sleep=lambda _s: None, say=lambda _t: None,
                             public_keys=fake.signer.keys, now=NOW, **kwargs)


def test_an_approved_key_is_stored_and_read_back(node):
    fake = FakeDev(Signer(), approve_after=3)

    answer = _request(node, fake, node="lab-node")

    assert secret_text.load_secret_text(node, key=PASSPHRASE)[dk.KEY_REF] == fake.key
    assert answer["state"] == "active" and answer["replaced"] is False
    assert answer["key"]["email"] == "a@example.com"
    assert fake.asked[0][2] == {"purpose": "key", "node": "lab-node"}


def test_the_key_itself_is_never_in_the_answer(node):
    fake = FakeDev(Signer())

    answer = _request(node, fake)

    assert fake.key not in json.dumps(answer)
    assert fake.key.split(".")[2] not in json.dumps(answer)


def test_a_store_that_does_not_open_stops_it_before_dbabrain_dev_is_asked(node, monkeypatch):
    secret_text.set_secret_text(node, "OTHER", "x", key="the-real-passphrase")
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, "a-wrong-passphrase")
    fake = FakeDev(Signer())

    with pytest.raises(errors.InvalidConfig):
        _request(node, fake)

    assert fake.asked == [], "a key asked for and not storable would be lost - and replace the earlier one"


def test_no_passphrase_is_said_before_anything_is_asked(node, monkeypatch):
    monkeypatch.delenv(secret_text.SECRET_KEY_ENV_VAR)
    fake = FakeDev(Signer())

    with pytest.raises(errors.NotConfigured, match="passphrase"):
        _request(node, fake)

    assert fake.asked == []


def test_a_key_this_release_cannot_read_is_refused_and_not_stored(node):
    stranger = Signer()
    fake = FakeDev(Signer(), key=stranger.token(issued=NOW))

    with pytest.raises(errors.Refused, match="upgrade dbabrain"):
        _request(node, fake)

    assert dk.KEY_REF not in secret_text.load_secret_text(node, key=PASSPHRASE)


def test_a_second_key_replaces_the_first(node):
    signer = Signer()
    _request(node, FakeDev(signer, key=signer.token(issued=NOW, kid="k-old")))

    answer = _request(node, FakeDev(signer, key=signer.token(issued=NOW, kid="k-new")))

    assert answer["replaced"] is True and answer["key"]["kid"] == "k-new"


def test_a_master_keeps_the_key_in_its_plaintext_source_too(node, tmp_path):
    source = tmp_path / "secrets" / "secret_text.json"
    source.parent.mkdir()
    source.write_text("{}", encoding="utf-8")
    fake = FakeDev(Signer())

    _request(node, fake, plaintext_store=source)

    assert json.loads(source.read_text(encoding="utf-8"))[dk.KEY_REF] == fake.key


def test_a_sign_in_that_expires_is_said(node, monkeypatch):
    fake = FakeDev(Signer(), approve_after=10**9)
    clock = iter(range(0, 10**6, 300))
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(clock))

    with pytest.raises(errors.Refused, match="expired"):
        _request(node, fake)


def _status(node, fake, *, online=True, now=NOW):
    return admin.key_status(node, http=fake, online=online, public_keys=fake.signer.keys, now=now)


def test_no_key_is_a_state_with_how_to_get_one_and_nothing_is_asked(node):
    fake = FakeDev(Signer())

    answer = _status(node, fake)

    assert answer["standing"]["state"] == "none"
    assert "key-request" in answer["how_to_get"]
    assert answer["features"]["reports"] is False
    assert fake.asked == []


def test_a_held_key_is_checked_online_and_the_answer_kept(node):
    fake = FakeDev(Signer())
    _request(node, fake)

    answer = _status(node, fake)

    assert answer["standing"]["state"] == "active"
    assert all(answer["features"].values())
    assert fake.asked[-1][3] == fake.key, "the key is the bearer of its own check"
    kept = json.loads(admin.status_file(node).read_text(encoding="utf-8"))
    assert kept["state"] == "active" and kept["kid"] == "k-1"


def test_a_revocation_heard_online_ends_the_key_and_is_remembered_offline(node):
    fake = FakeDev(Signer())
    _request(node, fake)
    fake.status_answer = (200, {"kid": "k-1", "state": "revoked"})
    _status(node, fake)

    offline = _status(node, fake, online=False)

    assert offline["standing"]["state"] == "revoked"
    assert offline["features"]["backup_restore"] is False


def test_a_check_that_cannot_reach_dbabrain_dev_changes_nothing(node):
    fake = FakeDev(Signer())
    _request(node, fake)
    fake.status_answer = OSError("no route to host")

    answer = _status(node, fake)

    assert answer["standing"]["state"] == "active"
    assert "offline answer stands" in answer["online"]["error"]


def test_the_status_file_is_beside_data_never_in_it(node):
    assert admin.status_file(node).parent.name == "runtime"
    assert admin.status_file(node).parent.parent == node.resolve().parent


def test_the_grace_is_said_by_key_status(node):
    fake = FakeDev(Signer())
    _request(node, fake)

    answer = _status(node, fake, online=False, now=NOW + timedelta(days=15))

    assert answer["standing"]["state"] == "grace"
    assert answer["features"]["backup_restore"] is True
