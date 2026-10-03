"""The first-run ``admin`` / ``admin`` can be added to a store that already holds accounts.

dbabrain is a public tool: a fresh install signs in as ``admin`` / ``admin`` once and must set a new
password before anything else (the operator, 2026-10-03). The console creates that account itself -
but only on an empty ``web_users`` table, so a store that outlived its first node never gets one.
The 0.26 soak node reused a store whose only account was a person's, made by a test run weeks
earlier with no readable copy of its password, and nobody could sign in.

``webhost.cli bootstrap-admin`` adds it on request. It never resets an ``admin`` that exists - that
password is somebody's - and the account it adds is held to the same first sign-in as the first
run's.
"""

from __future__ import annotations

from db_ops.db.web_auth_store import BOOTSTRAP_PASSWORD, BOOTSTRAP_USERNAME, WebAuthStore


def _store(tmp_path) -> WebAuthStore:
    store = WebAuthStore(tmp_path / "console.sqlite")
    store.create_user(username="operator", password="a-long-enough-password", level=100,
                      display_name="An operator", actor="test")
    return store


def test_the_first_run_does_not_add_admin_beside_an_existing_account(tmp_path):
    assert _store(tmp_path).ensure_bootstrap_admin() is False


def test_bootstrap_admin_adds_it_and_it_must_change_its_password_first(tmp_path):
    store = _store(tmp_path)

    assert store.add_bootstrap_admin(actor="test") is True

    admin = store.get_user(BOOTSTRAP_USERNAME)
    assert int(admin["user_level"]) == 100
    assert store.bootstrap_pending() is True
    assert store.get_user("operator") is not None, "the existing account is left alone"


def test_admin_admin_signs_in_once_added(tmp_path):
    store = _store(tmp_path)
    store.add_bootstrap_admin(actor="test")

    user, reason = store.authenticate(username=BOOTSTRAP_USERNAME, password=BOOTSTRAP_PASSWORD,
                                      client_ip="127.0.0.1")

    assert user is not None, reason
    assert user["username"] == BOOTSTRAP_USERNAME


def test_an_existing_admin_is_never_reset(tmp_path):
    store = _store(tmp_path)
    store.create_user(username=BOOTSTRAP_USERNAME, password="someone-else-chose-this", level=100,
                      display_name="Administrator", actor="test")

    assert store.add_bootstrap_admin(actor="test") is False
    assert store.bootstrap_pending() is False


def test_the_command_is_offered_by_the_webhost_cli():
    from db_ops.webhost import cli

    args = cli.parse_args(["bootstrap-admin"])

    assert args.handler is cli._handle_bootstrap_admin
