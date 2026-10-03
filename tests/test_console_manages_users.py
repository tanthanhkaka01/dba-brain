"""The console manages its own accounts, and the rules that make that safe hold on the server.

Until 2026-10-02 an account could only be added, re-levelled, reset or disabled with
``webhost.cli``; the operator asked for the basic functions in the console - *add a user, set
permissions* - following a common model (Grafana's *Users* page). A page that can grant access is
the most dangerous page the console has, so each rule is held here, against the request handler and
not the form: a form can be edited in the browser, the handler cannot.

* Only an admin opens the page, and every change carries the CSRF token **and** the actor's own
  password typed again (a script in the browser knows neither).
* Nobody grants a level above their own, or touches an account above their own.
* Nobody lowers or disables their own account here - another admin does, because a slip would lock
  its owner out with nobody to undo it.
* A password reset and a disable end the target's sessions; a level change applies at once,
  because a session reads its level from the account on every request.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import pytest

from conftest import write_catalogued_data

from db_ops.db import config_sync
from db_ops.db.config_store import ConfigStore
from db_ops.db.web_auth_store import WebAuthStore
from db_ops.lib import web_auth
from db_ops.webhost.app import Request, WebApp, WebSettings

PASSWORD = "console-password-1"


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch):
    monkeypatch.setattr(web_auth, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture(scope="module")
def _template(tmp_path_factory):
    root = tmp_path_factory.mktemp("users-template")
    data = write_catalogued_data(root / "data")
    store_path = root / "template.sqlite"
    config_sync.sync(ConfigStore(store_path), data_dir=data, actor="test")
    auth = WebAuthStore(store_path)
    auth.create_user(username="owner", password=PASSWORD, level=100)
    auth.create_user(username="admin", password=PASSWORD, level=90)
    auth.create_user(username="editor", password=PASSWORD, level=50)
    auth.create_user(username="viewer", password=PASSWORD, level=1)
    checkpoint = sqlite3.connect(store_path)
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    return store_path, data


@pytest.fixture()
def console(tmp_path: Path, _template) -> WebApp:
    store_path = tmp_path / "db_ops.sqlite"
    shutil.copy(_template[0], store_path)
    data = tmp_path / "data"
    shutil.copytree(_template[1], data)
    return WebApp(auth_store=WebAuthStore(store_path), config_store=ConfigStore(store_path),
                  ops_store=None, request_store=None, data_dir=data, log_dir=tmp_path,
                  settings=WebSettings())


def _post(path: str, fields: dict[str, str], cookie: str) -> Request:
    return Request(method="POST", path=path, body=urlencode(fields).encode("utf-8"),
                   headers={"content-type": "application/x-www-form-urlencoded", "cookie": cookie},
                   client_ip="10.0.0.5")


def _get(path: str, cookie: str = "") -> Request:
    return Request(method="GET", path=path, query={}, headers={"cookie": cookie} if cookie else {})


def sign_in(console: WebApp, username: str) -> str:
    response = console.handle(Request(
        method="POST", path="/db_ops/login",
        body=urlencode({"username": username, "password": PASSWORD}).encode("utf-8"),
        headers={"content-type": "application/x-www-form-urlencoded"}, client_ip="10.0.0.5"))
    assert response.status == 303
    return next(v for n, v in response.headers if n == "Set-Cookie").split(";")[0]


def csrf(console: WebApp, cookie: str) -> str:
    return str(console.auth.resolve_session(cookie.split("=", 1)[1])["csrf_token"])


def act(console: WebApp, cookie: str, path: str, **fields: str):
    fields.setdefault("csrf", csrf(console, cookie))
    fields.setdefault("confirm_password", PASSWORD)
    return console.handle(_post(f"/db_ops/users{path}", fields, cookie))


def level_of(console: WebApp, name: str) -> int | None:
    row = console.auth.get_user(name)
    return None if row is None else int(row["user_level"])


# --------------------------------------------------------------------------- #
def test_only_an_admin_opens_the_users_page(console):
    assert console.handle(_get("/db_ops/users", sign_in(console, "editor"))).status == 403
    page = console.handle(_get("/db_ops/users", sign_in(console, "admin")))
    assert page.status == 200
    body = page.body.decode("utf-8")
    assert "owner" in body and "editor" in body
    assert "Admin</span>" in body and "Editor</span>" in body, "levels are shown as named roles"


def test_the_sidebar_offers_users_to_an_admin_only(console):
    admin = console.handle(_get("/db_ops/", sign_in(console, "admin"))).body.decode("utf-8")
    viewer = console.handle(_get("/db_ops/", sign_in(console, "viewer"))).body.decode("utf-8")
    assert "/db_ops/users" in admin
    assert "/db_ops/users" not in viewer


def test_a_new_user_is_created_with_its_role_and_must_change_its_password(console):
    cookie = sign_in(console, "admin")
    response = act(console, cookie, "", username="Newbie", role="Editor", password="initial-pass-1",
                   password_again="initial-pass-1", must_change="1")
    assert response.status == 303
    row = console.auth.get_user("newbie")
    assert int(row["user_level"]) == 50 and int(row["must_change_password"]) == 1
    assert row["created_by"] == "admin"


def test_every_change_needs_the_actors_own_password_again(console):
    cookie = sign_in(console, "admin")
    response = act(console, cookie, "", username="x1", role="Viewer", password="initial-pass-1",
                   password_again="initial-pass-1", confirm_password="not-my-password")
    assert response.status == 403
    assert console.auth.get_user("x1") is None


def test_every_change_needs_the_csrf_token(console):
    cookie = sign_in(console, "admin")
    response = act(console, cookie, "/editor/disable", csrf="forged")
    assert response.status == 403
    assert level_of(console, "editor") == 50


def test_nobody_grants_a_level_above_their_own(console):
    cookie = sign_in(console, "admin")
    assert act(console, cookie, "", username="boss", role="Owner", password="initial-pass-1",
               password_again="initial-pass-1").status == 403
    assert act(console, cookie, "/editor/level", level="95").status == 403
    assert level_of(console, "editor") == 50


def test_an_account_above_yours_cannot_be_touched(console):
    cookie = sign_in(console, "admin")
    assert act(console, cookie, "/owner/disable").status == 403
    assert act(console, cookie, "/owner/signout").status == 403
    assert level_of(console, "owner") == 100


def test_nobody_lowers_or_disables_their_own_account_here(console):
    cookie = sign_in(console, "admin")
    assert act(console, cookie, "/admin/level", role="Viewer").status == 403
    assert act(console, cookie, "/admin/disable").status == 403
    assert level_of(console, "admin") == 90


def test_a_level_change_applies_at_the_next_request(console):
    editor = sign_in(console, "editor")
    assert console.handle(_get("/db_ops/users", editor)).status == 403
    assert act(console, sign_in(console, "owner"), "/editor/level", role="Admin").status == 303
    assert console.handle(_get("/db_ops/users", editor)).status == 200, "the same session, promoted"


def test_a_password_reset_ends_the_targets_sessions(console):
    editor = sign_in(console, "editor")
    response = act(console, sign_in(console, "admin"), "/editor/password",
                   password="reset-pass-123", password_again="reset-pass-123", must_change="1")
    assert response.status == 303
    assert console.handle(_get("/db_ops/", editor)).status == 303, "back to the login form"
    assert int(console.auth.get_user("editor")["must_change_password"]) == 1


def test_signing_someone_out_everywhere_ends_their_sessions(console):
    editor = sign_in(console, "editor")
    assert act(console, sign_in(console, "admin"), "/editor/signout").status == 303
    assert console.handle(_get("/db_ops/", editor)).status == 303


def test_disabling_an_account_signs_it_out_and_frees_its_name(console):
    editor = sign_in(console, "editor")
    assert act(console, sign_in(console, "admin"), "/editor/disable", note="left").status == 303
    assert console.auth.get_user("editor") is None
    assert console.handle(_get("/db_ops/", editor)).status == 303
    console.auth.create_user(username="editor", password=PASSWORD, level=1)


def test_a_bad_value_is_said_on_the_page_not_as_a_server_error(console):
    cookie = sign_in(console, "admin")
    response = act(console, cookie, "", username="short", role="Viewer", password="x",
                   password_again="x")
    assert response.status == 400
    assert "8 characters" in response.body.decode("utf-8")


def test_the_sign_in_page_follows_the_common_card_layout_and_says_one_thing_for_every_failure(console):
    page = console.handle(_get("/db_ops/login")).body.decode("utf-8")
    assert 'class="auth-card login"' in page and ">Show</button>" in page
    wrong_user = console.handle(Request(
        method="POST", path="/db_ops/login", body=urlencode({"username": "nobody", "password": "x"}).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"}, client_ip="10.0.0.9")).body.decode()
    wrong_password = console.handle(Request(
        method="POST", path="/db_ops/login", body=urlencode({"username": "viewer", "password": "x"}).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"}, client_ip="10.0.0.9")).body.decode()
    assert "Wrong username or password." in wrong_user and "Wrong username or password." in wrong_password
