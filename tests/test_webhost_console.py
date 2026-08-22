"""The console must refuse everything to a stranger and render the estate to a signed-in operator.

:class:`~db_ops.webhost.app.WebApp` is a plain request -> response function, so all of this runs
with no socket and no browser. What is being held down here:

* **No page past the login form is reachable without a session** — including the JSON endpoints,
  which have to answer 401 rather than hand back a login page a ``fetch()`` would parse as data.
* **The login cookie carries `Max-Age`.** One missing attribute is the whole difference between
  "signed in for three months" and "signed in until you close Chrome", and nothing else in the
  system would notice.
* **The sidebar lists all fourteen apps** on every page, in the docs' order, and clicking one
  shows that app — read from the config the store was synced with, which is what the mirror is for.
* **The overview shows what needs attention**, not everything at once: fourteen apps' worth of
  detail on one screen is how the two that are failing stop standing out.
* **Values reaching a page are escaped.** The store is now written to through a web form, so a
  config value is untrusted text on the way back out.
* **Every write is gated twice** — by the CSRF token and by the account's level — and a refusal
  says which, because an operator who cannot do a thing needs to know what it would take.
* **"Run now" queues, it does not run.** The console must never spawn the process itself; the row
  it writes is the whole of its side of that feature.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from conftest import write_catalogued_data, shipped_config

from db_ops.db import config_sync
from db_ops.db.config_store import ConfigStore
from db_ops.db.run_requests import RunRequestStore
from db_ops.db.web_auth_store import WebAuthStore
from db_ops.lib import web_auth
from db_ops.webhost.app import Request, WebApp, WebSettings, _schedule_text

REPO_ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "console-password-1"


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch):
    """The KDF at its floor. See tests/test_web_auth.py — the cost is asserted there, not here."""
    monkeypatch.setattr(web_auth, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture(scope="module")
def _template(tmp_path_factory) -> tuple[Path, Path]:
    """One synced store and one data copy, built once for the whole module.

    Every test below wants the same starting point: the real ``data/`` mirrored into a store, with
    two accounts. Building that per test meant copying the folder and syncing 362 records thirty
    times over, which was most of this file's runtime. It is built once here and each test gets a
    **file copy**, so the tests still cannot see each other's writes.
    """
    root = tmp_path_factory.mktemp("console-template")
    data = write_catalogued_data(root / "data")
    store_path = root / "template.sqlite"
    config_sync.sync(ConfigStore(store_path), data_dir=data, actor="test")
    auth = WebAuthStore(store_path)
    auth.create_user(username="thanh", password=PASSWORD, level=100, display_name="Thanh")
    auth.create_user(username="viewer", password=PASSWORD, level=1, display_name="Viewer")
    # Fold the write-ahead log back into the file before anything copies it. The store opens
    # SQLite in WAL mode, so a freshly written row can live entirely in template.sqlite-wal —
    # and a plain file copy would then hand each test a store with the schema and none of the
    # rows, which fails as "wrong password" rather than as "missing data".
    checkpoint = sqlite3.connect(store_path)
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    return store_path, data


@pytest.fixture()
def data_copy(_template) -> Path:
    return _template[1]


@pytest.fixture()
def console(tmp_path: Path, _template) -> WebApp:
    """A console over a private copy of the template store, with its own writable data folder.

    The data folder is copied per test rather than shared, because the edit tests write to it —
    a save rebuilds ``data/<file>.json``, which is the point of them.
    """
    template_store, template_data = _template
    store_path = tmp_path / "db_ops.sqlite"
    shutil.copy(template_store, store_path)
    data = tmp_path / "data"
    shutil.copytree(template_data, data)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "metrics.log").write_text("\n".join(
        f"2026-08-21 08:{i // 60:02d}:{i % 60:02d}|"
        f"{'ERROR' if i % 50 == 0 else 'LOGGING'}|metrics|host|metrics.collect|line {i}"
        for i in range(250)) + "\n", encoding="utf-8")
    (logs / "telegram.log").write_text(
        "2026-08-21 09:00:00|WARNING|telegram|host|send|queued\n"
        "Traceback (most recent call last):\n", encoding="utf-8")
    (logs / "metrics_20260819.log").write_text("rotated\n", encoding="utf-8")
    # Which log the page opens on is "whichever moved last", and three files written in the
    # same millisecond do not have a last. Stamped explicitly so the order is the fixture's
    # statement rather than the filesystem's timestamp resolution.
    now = time.time()
    os.utime(logs / "metrics.log", (now - 600, now - 600))
    os.utime(logs / "metrics_20260819.log", (now - 86400, now - 86400))
    os.utime(logs / "telegram.log", (now, now))
    return WebApp(auth_store=WebAuthStore(store_path), config_store=ConfigStore(store_path),
                  ops_store=None, request_store=RunRequestStore(store_path),
                  data_dir=data, log_dir=logs, settings=WebSettings())


def csrf_of(console: WebApp, cookie: str) -> str:
    """The token the console issued with this session.

    Read from the session rather than scraped off a page, because a viewer's pages carry no token
    at all — there is nothing on them to submit. A test that needed one from the markup would be
    asserting the absence of edit controls by accident.
    """
    session = console.auth.resolve_session(cookie.split("=", 1)[1])
    assert session is not None, "not signed in"
    return str(session["csrf_token"])


def users_on_disk(console: WebApp) -> list[dict]:
    path = Path(console.data_dir) / "telegram_users.json"
    return json.loads(path.read_text(encoding="utf-8"))["telegram_users"]


def get(path: str, *, cookie: str = "", **query: str) -> Request:
    return Request(method="GET", path=path,
                   query={key: [value] for key, value in query.items()},
                   headers={"cookie": cookie} if cookie else {})


def post_form(path: str, fields: dict[str, str], *, cookie: str = "") -> Request:
    from urllib.parse import urlencode

    headers = {"content-type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["cookie"] = cookie
    return Request(method="POST", path=path, headers=headers,
                   body=urlencode(fields).encode("utf-8"), client_ip="10.0.0.5")


def cookie_from(response) -> str:
    """The ``name=value`` pair a browser would send back, from the Set-Cookie header."""
    for name, value in response.headers:
        if name == "Set-Cookie":
            return value.split(";")[0]
    raise AssertionError("no Set-Cookie header on the response")


def sign_in(console: WebApp, username: str = "thanh", password: str = PASSWORD):
    response = console.handle(post_form("/db_ops/login",
                                        {"username": username, "password": password}))
    assert response.status == 303, "a good login must redirect, not re-render the form"
    return cookie_from(response)


# --------------------------------------------------------------------------- #
# Who owns which URL
# --------------------------------------------------------------------------- #
def test_the_console_claims_only_its_own_prefix(console: WebApp) -> None:
    """The report URLs must keep working exactly as they did; the console is a prefix beside them."""
    assert console.owns("/db_ops") and console.owns("/db_ops/login")
    assert not console.owns("/report_dba/database-inventory.html")
    assert not console.owns("/db_ops_other/page.html"), (
        "prefix matching must be on a path segment, or a sibling mount is swallowed")


# --------------------------------------------------------------------------- #
# Nothing without a session
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/db_ops/", "/db_ops/dashboard"])
def test_a_page_without_a_session_redirects_to_the_login_form(console: WebApp, path: str) -> None:
    response = console.handle(get(path))
    assert response.status == 303
    assert dict(response.headers)["Location"].startswith("/db_ops/login")


@pytest.mark.parametrize("path", ["/db_ops/api/session", "/db_ops/api/apps", "/db_ops/api/config"])
def test_an_api_without_a_session_answers_401_json(console: WebApp, path: str) -> None:
    """A fetch() must get an error it can read, not a login page it would parse as data."""
    response = console.handle(get(path))
    assert response.status == 401
    assert response.content_type.startswith("application/json")
    assert json.loads(response.body)["error"] == "not authenticated"


def test_a_forged_cookie_is_not_a_session(console: WebApp) -> None:
    response = console.handle(get("/db_ops/", cookie="db_ops_session=made-up-token"))
    assert response.status == 303


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #
def test_the_login_cookie_survives_closing_the_browser(console: WebApp) -> None:
    """`Max-Age` is the requirement. Without it the browser drops the cookie on exit."""
    response = console.handle(post_form("/db_ops/login",
                                        {"username": "thanh", "password": PASSWORD}))
    header = next(value for name, value in response.headers if name == "Set-Cookie")
    assert "Max-Age=" in header, "a cookie with no Max-Age is discarded when the browser closes"
    max_age = int(header.split("Max-Age=")[1].split(";")[0])
    assert 89 * 86400 < max_age <= 90 * 86400
    assert "HttpOnly" in header, "the token must be unreachable from any script on the page"
    assert "SameSite=Lax" in header


def test_a_wrong_password_re_renders_the_form_and_sets_no_cookie(console: WebApp) -> None:
    response = console.handle(post_form("/db_ops/login",
                                        {"username": "thanh", "password": "wrong"}))
    assert response.status == 401
    assert not any(name == "Set-Cookie" for name, _ in response.headers)
    assert b"Wrong username or password" in response.body


def test_the_form_says_the_same_thing_for_an_unknown_user_as_for_a_wrong_password(
        console: WebApp) -> None:
    """Otherwise the login page is a way to find out who works here."""
    unknown = console.handle(post_form("/db_ops/login",
                                       {"username": "ghost", "password": PASSWORD}))
    wrong = console.handle(post_form("/db_ops/login",
                                     {"username": "thanh", "password": "nope"}))
    assert unknown.status == wrong.status == 401
    assert b"Wrong username or password" in unknown.body
    assert b"Wrong username or password" in wrong.body


def test_signing_in_reaches_the_dashboard(console: WebApp) -> None:
    cookie = sign_in(console)
    response = console.handle(get("/db_ops/", cookie=cookie))
    assert response.status == 200
    assert b"db_ops console" in response.body
    assert b"Thanh" in response.body


def test_the_login_page_redirects_someone_already_signed_in(console: WebApp) -> None:
    cookie = sign_in(console)
    response = console.handle(get("/db_ops/login", cookie=cookie))
    assert response.status == 303 and dict(response.headers)["Location"] == "/db_ops/"


def test_signing_out_revokes_the_session_and_clears_the_cookie(console: WebApp) -> None:
    cookie = sign_in(console)
    response = console.handle(post_form("/db_ops/logout", {}, cookie=cookie))
    assert response.status == 303
    header = next(value for name, value in response.headers if name == "Set-Cookie")
    assert "Max-Age=0" in header
    assert console.handle(get("/db_ops/", cookie=cookie)).status == 303, (
        "the token must stop working server-side, not only be dropped by the browser")


def test_the_next_parameter_cannot_send_a_user_off_site(console: WebApp) -> None:
    """A login form is exactly where an open redirect gets used."""
    response = console.handle(post_form(
        "/db_ops/login",
        {"username": "thanh", "password": PASSWORD, "next": "https://evil.example/steal"}))
    assert dict(response.headers)["Location"] == "/db_ops/"


def test_an_empty_store_tells_the_operator_how_to_create_the_first_account(tmp_path: Path,
                                                                          data_copy: Path) -> None:
    store_path = tmp_path / "fresh.sqlite"
    config = ConfigStore(store_path)
    config_sync.sync(config, data_dir=data_copy, actor="test")
    app = WebApp(auth_store=WebAuthStore(store_path), config_store=config, data_dir=data_copy)
    body = app.handle(get("/db_ops/login")).body
    assert b"No accounts exist yet" in body
    assert b"user-add" in body


# --------------------------------------------------------------------------- #
# The dashboard
# --------------------------------------------------------------------------- #
def test_the_console_knows_every_app_that_is_installed(console: WebApp) -> None:
    blocks = console.app_blocks()
    # A *package*, not merely a directory: `db_ops/assets` holds the shipped SQL and scripts and
    # is data, not a component. `tests/test_docs_cover_every_component.py` draws the same line the
    # same way, and the console's app list has to agree with it.
    packages = {p.name for p in (REPO_ROOT / "db_ops").iterdir()
                if p.is_dir() and not p.name.startswith("__") and (p / "__init__.py").exists()}
    # Counted from the tree rather than written down. This asserted `== 14` until a distribution
    # shipped thirteen of them, and then a correct console failed a test for agreeing with the
    # install it was running in. The property is "the console lists what is there", and the number
    # is a consequence.
    assert len(blocks) == len(packages)
    assert {block["app_code"] for block in blocks} == packages
    assert [block["ord"] for block in blocks] == sorted(block["ord"] for block in blocks)


def test_the_sidebar_lists_every_app_in_order_on_every_page(console: WebApp) -> None:
    """The same names in the same place on every page is the whole point of a fixed list."""
    cookie = sign_in(console)
    order = [block["app_code"] for block in console.app_blocks()]
    for url in ("/db_ops/", "/db_ops/app/metrics", "/db_ops/config/telegram_users.json",
                "/db_ops/config/telegram_users.json/telegram_users/100000001"):
        html = console.handle(get(url, cookie=cookie)).body.decode()
        links = re.findall(r'href="/db_ops/app/([a-z_]+)"', html)
        # Sliced to the sidebar's own length rather than to a remembered 14: a page may carry app
        # links below the sidebar, and the number of apps depends on what this distribution
        # installs.
        assert links[:len(order)] == order, f"{url} lost or reordered the app list"


def test_the_sidebar_marks_which_app_is_open(console: WebApp) -> None:
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/telegram", cookie=cookie)).body.decode()
    active = re.search(r'<a class="item active" href="/db_ops/app/([a-z_]+)"', html)
    assert active and active.group(1) == "telegram"


def test_a_config_page_keeps_its_app_highlighted(console: WebApp) -> None:
    """Descending into a file must not lose which app you are in."""
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/config/metric_definitions.json", cookie=cookie)).body.decode()
    active = re.search(r'<a class="item active" href="/db_ops/app/([a-z_]+)"', html)
    assert active and active.group(1) == "metrics"


def test_clicking_an_app_shows_that_app(console: WebApp) -> None:
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/telegram", cookie=cookie)).body.decode()
    assert "Telegram App" in html
    assert "APP-TELEGRAM" in html
    assert "Metrics Engine" not in html.split('<section class="detail">')[1], (
        "the detail pane shows one app, not all of them")


def test_an_unknown_app_is_a_404_not_a_silent_fallback(console: WebApp) -> None:
    """Falling back to the first app would make a stale link look like it worked."""
    cookie = sign_in(console)
    response = console.handle(get("/db_ops/app/invented", cookie=cookie))
    assert response.status == 404
    assert b"No app called" in response.body


def test_the_overview_counts_the_estate(console: WebApp) -> None:
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/", cookie=cookie)).body.decode()
    assert "Overview" in html
    expected_apps = len(console.app_blocks())
    assert re.search(rf'<div class="n">{expected_apps}</div>\s*<div class="l">apps</div>', html), (
        "the overview's app count and the sidebar disagree"
    )
    assert "Needs attention" in html


def test_a_block_carries_the_schedule_of_the_commands_it_owns(console: WebApp) -> None:
    """"Daemon repeats every 10s" is the kind of thing the block has to say out loud."""
    telegram = next(b for b in console.app_blocks() if b["app_code"] == "telegram")
    command = telegram["commands"][0]
    assert command["app_command_id"] == "APP-TELEGRAM"
    assert command["repeat_interval"] == 1
    assert command["schedule_text"] == "every 1s"
    assert command["timeout"] == 300
    assert "python -m db_ops.telegram.cli" in command["command_text"]


def test_an_app_with_no_scheduled_command_says_so_rather_than_looking_broken(
        console: WebApp) -> None:
    lib = next(b for b in console.app_blocks() if b["app_code"] == "lib")
    assert lib["commands"] == []
    cookie = sign_in(console)
    body = console.handle(get("/db_ops/app/lib", cookie=cookie)).body
    assert b"No scheduled command" in body


def test_a_block_lists_the_config_it_owns_with_a_record_count(console: WebApp) -> None:
    metrics = next(b for b in console.app_blocks() if b["app_code"] == "metrics")
    files = {item["source_file"]: item["records"] for item in metrics["config"]}
    # The numbers are the fixture's, and the point is which rows they count: the collection's
    # records and not the `__document__` row that carries a file's top-level keys.
    assert files["metric_definitions.json"] == 2, "the __document__ row is not a record"
    assert files["db_instances.json"] == 1


@pytest.mark.parametrize(
    "window,expected",
    [
        ({"repeat_interval": 10}, "every 10s"),
        ({"repeat_interval": 120}, "every 2m"),
        ({"repeat_interval": 3600}, "every 1h"),
        ({"repeat_interval": 0}, "runs once and stays up"),
        ({"repeat_interval": -1}, "manual only"),
        ({}, "no schedule"),
        ({"repeat_interval": 60, "from_hour": 8, "to_hour": 20}, "every 1m, 08:00-20:59"),
    ],
)
def test_a_schedule_reads_the_way_an_operator_says_it(window: dict, expected: str) -> None:
    """`repeat_interval: 0` means run-once-and-stay-up; "every 0 seconds" reads as a broken clock."""
    assert _schedule_text(window) == expected


def test_an_app_page_renders_without_the_run_history(console: WebApp) -> None:
    """A store that cannot answer job_runs must cost the status column, not the whole page."""
    cookie = sign_in(console)
    response = console.handle(get("/db_ops/app/metrics", cookie=cookie))
    assert response.status == 200
    assert b"no run history" in response.body
    assert b"APP-METRICS" in response.body


# --------------------------------------------------------------------------- #
# API + escaping
# --------------------------------------------------------------------------- #
def test_the_session_api_reports_who_is_signed_in(console: WebApp) -> None:
    cookie = sign_in(console, "viewer")
    payload = json.loads(console.handle(get("/db_ops/api/session", cookie=cookie)).body)
    assert payload["username"] == "viewer" and payload["level"] == 1


def test_the_config_api_returns_the_mirrored_records(console: WebApp) -> None:
    cookie = sign_in(console)
    response = console.handle(get("/db_ops/api/config", cookie=cookie,
                                  source_file="reports_config.json"))
    items = json.loads(response.body)["items"]
    assert {item["source_file"] for item in items} == {"reports_config.json"}
    assert any(item["payload"].get("report_code") for item in items)


def test_a_config_value_containing_markup_is_escaped_on_the_page(tmp_path: Path,
                                                                 data_copy: Path) -> None:
    """The store is written to through a web form, so what comes back out is untrusted text."""
    data = tmp_path / "data"
    shutil.copytree(data_copy, data)
    path = data / "webhost_config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["apps"][0]["summary"] = '</p><script>alert("xss")</script>'
    path.write_text(json.dumps(payload, indent=4), encoding="utf-8")

    store_path = tmp_path / "xss.sqlite"
    config = ConfigStore(store_path)
    config_sync.sync(config, data_dir=data, actor="test")
    auth = WebAuthStore(store_path)
    auth.create_user(username="thanh", password=PASSWORD, level=100)
    app = WebApp(auth_store=auth, config_store=config, data_dir=data)

    cookie = sign_in(app)
    # The tampered summary is on the first app's own page, which is where a summary is shown.
    first = app.app_blocks()[0]["app_code"]
    body = app.handle(get(f"/db_ops/app/{first}", cookie=cookie)).body
    assert b"<script>alert" not in body
    assert b"&lt;script&gt;alert" in body


def test_an_unknown_console_page_is_a_404_not_a_redirect_loop(console: WebApp) -> None:
    cookie = sign_in(console)
    response = console.handle(get("/db_ops/nope", cookie=cookie))
    assert response.status == 404


def test_a_failure_inside_the_console_is_a_page_not_a_dropped_connection(console: WebApp,
                                                                        monkeypatch) -> None:
    """This process also serves the reports; an escaping exception looks like the worker being down."""
    monkeypatch.setattr(console, "app_blocks",
                        lambda: (_ for _ in ()).throw(RuntimeError("store is gone")))
    cookie = sign_in(console)
    response = console.handle(get("/db_ops/", cookie=cookie))
    assert response.status == 500
    assert b"store is gone" in response.body


# --------------------------------------------------------------------------- #
# Editing config
# --------------------------------------------------------------------------- #
NEW_USER = {
    "user_id": "424242", "is_bot": False, "user_type": 10, "first_name": "Web",
    "last_name": "", "username": "webadded", "language_code": "en", "status": "active",
    "note": "Added from the console.",
}


def test_a_config_file_page_lists_its_records(console: WebApp) -> None:
    cookie = sign_in(console)
    response = console.handle(get("/db_ops/config/telegram_users.json", cookie=cookie))
    assert response.status == 200
    assert b"telegram_users" in response.body
    assert b"100000001" in response.body, "the record keys are what an operator clicks"


def test_a_record_page_shows_the_json_that_will_be_saved(console: WebApp) -> None:
    """A JSON editor, not a generated form: these records have no fixed shape."""
    cookie = sign_in(console)
    response = console.handle(
        get("/db_ops/config/telegram_users.json/telegram_users/100000001", cookie=cookie))
    assert response.status == 200
    assert b"operator" in response.body
    assert b"<textarea" in response.body


def test_saving_a_new_record_writes_the_store_and_the_file(console: WebApp) -> None:
    """The file is what the apps read; a save that stopped at the store would do nothing."""
    cookie = sign_in(console)
    response = console.handle(post_form(
        "/db_ops/config/telegram_users.json/telegram_users",
        {"csrf": csrf_of(console, cookie), "payload": json.dumps(NEW_USER)}, cookie=cookie))

    assert response.status == 303
    assert "saved=inserted" in dict(response.headers)["Location"]
    assert any(user["user_id"] == "424242" for user in users_on_disk(console))
    assert console.config.get_item(source_file="telegram_users.json",
                                   collection="telegram_users", item_key="424242") is not None


def test_editing_a_record_bumps_its_revision_and_records_the_author(console: WebApp) -> None:
    cookie = sign_in(console)
    token = csrf_of(console, cookie)
    record = dict(users_on_disk(console)[0])
    record["user_type"] = 42
    console.handle(post_form(
        f"/db_ops/config/telegram_users.json/telegram_users/{record['user_id']}",
        {"csrf": token, "payload": json.dumps(record)}, cookie=cookie))

    row = console.config.get_item(source_file="telegram_users.json",
                                  collection="telegram_users", item_key=record["user_id"])
    assert row["revision"] == 2
    assert row["updated_by"] == "thanh"
    assert json.loads(row["item_json"])["user_type"] == 42


def test_retiring_a_record_from_the_console_keeps_its_row(console: WebApp) -> None:
    cookie = sign_in(console)
    token = csrf_of(console, cookie)
    target = users_on_disk(console)[0]["user_id"]
    response = console.handle(post_form(
        f"/db_ops/config/telegram_users.json/telegram_users/{target}/delete",
        {"csrf": token}, cookie=cookie))

    assert response.status == 303
    assert not any(user["user_id"] == target for user in users_on_disk(console))
    kept = [row for row in console.config.list_items(source_file="telegram_users.json",
                                                     include_inactive=True)
            if row["item_key"] == target]
    assert len(kept) == 1 and kept[0]["is_active"] == 0


def test_a_refused_edit_explains_itself_and_changes_nothing(console: WebApp) -> None:
    cookie = sign_in(console)
    before = users_on_disk(console)
    response = console.handle(post_form(
        "/db_ops/config/telegram_users.json/telegram_users",
        {"csrf": csrf_of(console, cookie), "payload": json.dumps({"username": "no-key"})},
        cookie=cookie))

    assert response.status == 400
    assert b"user_id" in response.body, "the refusal has to name the field that is missing"
    assert users_on_disk(console) == before


def test_malformed_json_is_refused_before_anything_is_written(console: WebApp) -> None:
    cookie = sign_in(console)
    response = console.handle(post_form(
        "/db_ops/config/telegram_users.json/telegram_users",
        {"csrf": csrf_of(console, cookie), "payload": "{not json"}, cookie=cookie))
    assert response.status == 400
    assert b"not valid JSON" in response.body


def test_the_retired_records_are_hidden_until_asked_for(console: WebApp) -> None:
    cookie = sign_in(console)
    token = csrf_of(console, cookie)
    target = users_on_disk(console)[0]["user_id"]
    console.handle(post_form(
        f"/db_ops/config/telegram_users.json/telegram_users/{target}/delete",
        {"csrf": token}, cookie=cookie))

    # Asserted on the row marker, not on the word: the page's own hint text explains what
    # retiring does, so a substring search for it matches whether or not any row is shown.
    plain = console.handle(get("/db_ops/config/telegram_users.json", cookie=cookie)).body
    assert b'<tr class="retired">' not in plain
    assert target.encode() not in plain

    shown = console.handle(
        get("/db_ops/config/telegram_users.json", cookie=cookie, retired="1")).body
    assert b'<tr class="retired">' in shown and target.encode() in shown


# --------------------------------------------------------------------------- #
# The two gates on every write
# --------------------------------------------------------------------------- #
def test_a_write_without_the_csrf_token_is_refused(console: WebApp) -> None:
    """SameSite=Lax already blocks the cross-site POST; this is the second lock."""
    cookie = sign_in(console)
    before = users_on_disk(console)
    response = console.handle(post_form(
        "/db_ops/config/telegram_users.json/telegram_users",
        {"csrf": "not-the-token", "payload": json.dumps(NEW_USER)}, cookie=cookie))

    assert response.status == 403
    assert b"came from somewhere else" in response.body
    assert users_on_disk(console) == before


def test_a_write_with_no_csrf_field_at_all_is_refused(console: WebApp) -> None:
    cookie = sign_in(console)
    response = console.handle(post_form(
        "/db_ops/config/telegram_users.json/telegram_users",
        {"payload": json.dumps(NEW_USER)}, cookie=cookie))
    assert response.status == 403


def test_a_viewer_may_read_config_but_not_change_it(console: WebApp) -> None:
    """Level 1 is the read gate; editing needs 50."""
    cookie = sign_in(console, "viewer")
    assert console.handle(get("/db_ops/config/telegram_users.json", cookie=cookie)).status == 200

    before = users_on_disk(console)
    response = console.handle(post_form(
        "/db_ops/config/telegram_users.json/telegram_users",
        {"csrf": csrf_of(console, cookie), "payload": json.dumps(NEW_USER)}, cookie=cookie))
    assert response.status == 403
    assert b"needs level 50" in response.body
    assert users_on_disk(console) == before


def test_a_viewer_sees_no_edit_controls(console: WebApp) -> None:
    """A button that answers 403 is a worse answer than no button."""
    cookie = sign_in(console, "viewer")
    body = console.handle(get("/db_ops/config/telegram_users.json", cookie=cookie)).body
    assert b"Add a record" not in body
    record = console.handle(
        get("/db_ops/config/telegram_users.json/telegram_users/100000001", cookie=cookie)).body
    assert b"Retire this record" not in record
    assert b"read-only access" in record


def test_editing_config_needs_a_session_at_all(console: WebApp) -> None:
    response = console.handle(post_form(
        "/db_ops/config/telegram_users.json/telegram_users", {"payload": "{}"}))
    assert response.status == 303
    assert dict(response.headers)["Location"].startswith("/db_ops/login")


# --------------------------------------------------------------------------- #
# Running an app
# --------------------------------------------------------------------------- #
def test_run_now_queues_a_request_and_starts_nothing(console: WebApp) -> None:
    """The console's whole side of this feature is one row; the daemon does the running."""
    cookie = sign_in(console)
    response = console.handle(post_form(
        "/db_ops/apps/APP-METRICS/run", {"csrf": csrf_of(console, cookie)}, cookie=cookie))

    assert response.status == 303
    assert dict(response.headers)["Location"].endswith("?queued=APP-METRICS")
    rows = console.requests.list_requests()
    assert len(rows) == 1
    assert rows[0]["app_command_id"] == "APP-METRICS"
    assert rows[0]["requested_by"] == "thanh"
    assert rows[0]["status"] == "pending"


def test_pressing_run_twice_does_not_queue_it_twice(console: WebApp) -> None:
    cookie = sign_in(console)
    token = csrf_of(console, cookie)
    console.handle(post_form("/db_ops/apps/APP-METRICS/run", {"csrf": token}, cookie=cookie))
    second = console.handle(post_form("/db_ops/apps/APP-METRICS/run", {"csrf": token},
                                      cookie=cookie))

    assert dict(second.headers)["Location"].endswith("?already_queued=APP-METRICS")
    assert len(console.requests.list_requests()) == 1


def test_a_queued_app_shows_as_queued_instead_of_offering_the_button(console: WebApp) -> None:
    cookie = sign_in(console)
    token = csrf_of(console, cookie)
    before = console.handle(get("/db_ops/app/metrics", cookie=cookie)).body.count(b"Run now")
    assert before == 1
    console.handle(post_form("/db_ops/apps/APP-METRICS/run", {"csrf": token}, cookie=cookie))

    body = console.handle(get("/db_ops/app/metrics", cookie=cookie)).body
    assert b"Run now" not in body
    assert b"pending" in body


def test_running_an_app_returns_to_the_page_it_was_pressed_on(console: WebApp) -> None:
    """Sending every run to the overview takes the operator off the app they were reading."""
    cookie = sign_in(console)
    response = console.handle(post_form(
        "/db_ops/apps/APP-METRICS/run",
        {"csrf": csrf_of(console, cookie), "from": "app/metrics"}, cookie=cookie))
    assert dict(response.headers)["Location"] == "/db_ops/app/metrics?queued=APP-METRICS"


@pytest.mark.parametrize("hostile", ["//evil.example/", "app/../../elsewhere", "app/invented",
                                     "https://evil.example", "../../etc"])
def test_the_return_target_cannot_be_used_to_go_anywhere_else(console: WebApp,
                                                              hostile: str) -> None:
    """A field that names where to go next is a redirect, however small it looks.

    Prefix-matching "app/" is not enough: "app/../../elsewhere" starts with it too. The target is
    checked against the apps that actually exist, so anything else lands on the overview.
    """
    cookie = sign_in(console)
    response = console.handle(post_form(
        "/db_ops/apps/APP-METRICS/run",
        {"csrf": csrf_of(console, cookie), "from": hostile}, cookie=cookie))
    assert dict(response.headers)["Location"].startswith("/db_ops/?")


def test_running_an_app_needs_the_level_and_the_token(console: WebApp) -> None:
    viewer = sign_in(console, "viewer")
    refused = console.handle(post_form("/db_ops/apps/APP-METRICS/run",
                                       {"csrf": csrf_of(console, viewer)}, cookie=viewer))
    assert refused.status == 403 and b"needs level 50" in refused.body

    admin = sign_in(console)
    no_token = console.handle(post_form("/db_ops/apps/APP-METRICS/run", {"csrf": "x"},
                                        cookie=admin))
    assert no_token.status == 403
    assert console.requests.list_requests() == []


def test_an_app_that_is_not_configured_cannot_be_queued(console: WebApp) -> None:
    """The id comes off a page, so a bad one means a stale page or a hand-written request."""
    cookie = sign_in(console)
    response = console.handle(post_form("/db_ops/apps/APP-INVENTED/run",
                                        {"csrf": csrf_of(console, cookie)}, cookie=cookie))
    assert response.status == 404
    assert b"No app command" in response.body
    assert console.requests.list_requests() == []


def test_the_dashboard_still_renders_without_a_run_queue(tmp_path: Path, _template) -> None:
    """A node with no queue shows no run buttons, rather than buttons that fail."""
    template_store, data = _template
    store_path = tmp_path / "no-queue.sqlite"
    shutil.copy(template_store, store_path)
    app = WebApp(auth_store=WebAuthStore(store_path), config_store=ConfigStore(store_path),
                 request_store=None, data_dir=data, settings=WebSettings())
    cookie = sign_in(app)
    assert b"may not run apps" in app.handle(get("/db_ops/", cookie=cookie)).body
    assert b"Run now" not in app.handle(get("/db_ops/app/metrics", cookie=cookie)).body


# --------------------------------------------------------------------------- #
# What `serve` actually builds
# --------------------------------------------------------------------------- #
def test_serve_hands_the_console_every_store_it_needs(estate, monkeypatch) -> None:
    """The console is only as capable as what `serve` wires into it.

    Every optional store degrades quietly by design — no run queue means no Run buttons, no ops
    store means no status column — which is right at runtime and useless as a check. This is the
    check: on 2026-08-20 `serve` was shipped without the run queue, so the deployed console had
    no Run buttons at all and every test still passed, because the tests built their own WebApp.
    """
    from db_ops.webhost import cli

    built = {}

    def fake_serve(**kwargs):
        built.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "serve", fake_serve)
    args = cli.parse_args(["serve", "--port", "0"])
    # A config of this test's own, not the machine's. Building a store only resolves a target and
    # opens no connection, so this exercises exactly what a deploy runs while needing no estate —
    # it used to read the repository's `config.json`, which made the check unrunnable anywhere
    # that file does not exist.
    config = cli.load_config(estate.config())

    cli._handle_serve(args, config, None)
    console = built["console"]
    assert console is not None, "the console must be built by default"
    assert console.auth is not None
    assert console.config is not None
    assert console.ops_store is not None, "without it the dashboard loses every status"
    assert console.requests is not None, "without it there are no Run buttons at all"
    assert console.data_dir is not None


def test_no_console_serves_reports_only(estate, monkeypatch) -> None:
    from db_ops.webhost import cli

    built = {}
    monkeypatch.setattr(cli, "serve", lambda **kwargs: built.update(kwargs) or 0)
    args = cli.parse_args(["serve", "--no-console"])
    config = cli.load_config(estate.config())
    cli._handle_serve(args, config, None)
    assert built["console"] is None


# --------------------------------------------------------------------------- #
# The field grid
# --------------------------------------------------------------------------- #
def _grid_fields(page: str) -> dict[str, str]:
    """The grid's inputs, as a browser would submit them: {field name: value}.

    Names and values are HTML-escaped on the page (a field path is JSON, so it is full of quotes),
    so they are unescaped back here — posting the escaped form would address a field that does not
    exist and silently drop the real one.

    A checkbox is modelled as its resulting single value rather than as the hidden/checkbox pair:
    the parser reads the last value, and one value is the last value. The pair itself is exercised
    in ``tests/test_record_form.py``, which posts it exactly as the page emits it.
    """
    import html as html_module

    fields: dict[str, str] = {}
    for name, value in re.findall(
            r'<input type="(?:hidden|text|number)" name="(f:[^"]+)" value="([^"]*)"', page):
        fields[html_module.unescape(name)] = html_module.unescape(value)
    for name, rest in re.findall(r'<input type="checkbox" name="(f:[^"]+)"([^>]*)>', page):
        fields[html_module.unescape(name)] = "true" if "checked" in rest else "false"
    for name, value in re.findall(r'<textarea name="(f:[^"]+)"[^>]*>(.*?)</textarea>',
                                  page, re.S):
        fields[html_module.unescape(name)] = html_module.unescape(value)
    return fields


def test_a_record_is_drawn_as_a_grid_not_as_raw_json(console: WebApp) -> None:
    """Raw JSON in a textarea was honest and unreadable; the fields are the readable form."""
    cookie = sign_in(console)
    html = console.handle(
        get("/db_ops/config/telegram_users.json/telegram_users/100000001", cookie=cookie)
    ).body.decode()

    assert '<table class="fields">' in html
    fields = _grid_fields(html)
    labels = {name.rsplit(":", 1)[-1] for name in fields}
    assert any("user_id" in label for label in labels)
    assert any("username" in label for label in labels)
    # The JSON box is still there, but as the escape hatch rather than the interface.
    assert "Edit as JSON" in html


def test_a_nested_block_is_drawn_as_its_own_section(console: WebApp) -> None:
    """`time_window` is how an operator already thinks of it — a heading, not eight loose rows."""
    cookie = sign_in(console)
    html = console.handle(
        get("/db_ops/config/reports_config.json/reports/rp_metric_daily_logging", cookie=cookie)
    ).body.decode()
    assert '<tr class="section">' in html
    assert "time_window" in html
    assert 'type="number"' in html, "repeat_interval is a number and should be typed as one"


def test_saving_the_grid_unchanged_changes_nothing(console: WebApp) -> None:
    """The strongest check there is: opening a record and pressing Save must be a no-op."""
    cookie = sign_in(console)
    url = "/db_ops/config/telegram_users.json/telegram_users/100000001"
    html = console.handle(get(url, cookie=cookie)).body.decode()

    before = console.config.get_item(source_file="telegram_users.json",
                                     collection="telegram_users", item_key="100000001")
    form = dict(_grid_fields(html))
    form["csrf"] = csrf_of(console, cookie)
    response = console.handle(post_form(url, form, cookie=cookie))

    assert response.status == 303
    after = console.config.get_item(source_file="telegram_users.json",
                                    collection="telegram_users", item_key="100000001")
    assert json.loads(after["item_json"]) == json.loads(before["item_json"])
    assert after["revision"] == before["revision"], (
        "an unchanged save must not bump a revision, or the history fills with edits nobody made")


def test_editing_one_field_in_the_grid_changes_only_that_field(console: WebApp) -> None:
    cookie = sign_in(console)
    url = "/db_ops/config/telegram_users.json/telegram_users/100000001"
    html = console.handle(get(url, cookie=cookie)).body.decode()
    form = dict(_grid_fields(html))
    target = next(name for name in form if name.endswith('["user_type"]'))
    form[target] = "42"
    form["csrf"] = csrf_of(console, cookie)
    console.handle(post_form(url, form, cookie=cookie))

    saved = json.loads(console.config.get_item(source_file="telegram_users.json",
                                               collection="telegram_users",
                                               item_key="100000001")["item_json"])
    assert saved["user_type"] == 42 and isinstance(saved["user_type"], int)
    assert saved["username"] == "operator", "nothing else may move"


def test_a_bad_number_in_the_grid_is_refused_and_names_the_field(console: WebApp) -> None:
    cookie = sign_in(console)
    url = "/db_ops/config/telegram_users.json/telegram_users/100000001"
    html = console.handle(get(url, cookie=cookie)).body.decode()
    form = dict(_grid_fields(html))
    form[next(name for name in form if name.endswith('["user_type"]'))] = "very high"
    form["csrf"] = csrf_of(console, cookie)

    response = console.handle(post_form(url, form, cookie=cookie))
    assert response.status == 400
    assert b"user_type" in response.body


def test_the_json_box_still_saves_and_is_how_a_key_is_added(console: WebApp) -> None:
    """The grid edits the fields a record has; adding one needs the JSON box."""
    cookie = sign_in(console)
    url = "/db_ops/config/telegram_users.json/telegram_users/100000001"
    record = dict(users_on_disk(console)[0])
    record["nickname"] = "added via json"
    console.handle(post_form(url, {"csrf": csrf_of(console, cookie),
                                   "payload": json.dumps(record)}, cookie=cookie))

    saved = json.loads(console.config.get_item(source_file="telegram_users.json",
                                               collection="telegram_users",
                                               item_key="100000001")["item_json"])
    assert saved["nickname"] == "added via json"


def test_a_viewer_gets_the_grid_read_only(console: WebApp) -> None:
    cookie = sign_in(console, "viewer")
    html = console.handle(
        get("/db_ops/config/telegram_users.json/telegram_users/100000001", cookie=cookie)
    ).body.decode()
    assert '<table class="fields">' in html
    assert "readonly" in html
    assert "Retire this record" not in html


# --------------------------------------------------------------------------- #
# The running log
# --------------------------------------------------------------------------- #
def test_the_logging_page_shows_the_running_log_newest_first(console: WebApp) -> None:
    """The line you want in a log you are watching is the one just written."""
    cookie = sign_in(console)
    html = console.handle(
        get("/db_ops/app/logging_ops", cookie=cookie, file="metrics.log")).body.decode()

    assert "Running log" in html
    assert html.index("line 249") < html.index("line 200"), "newest must be at the top"
    assert html.count("<tr class=") == 100, "one screenful, not the whole file"
    assert "line 149" not in html, "the 101st line waits for the scroll"


def test_the_log_that_moved_last_is_the_one_offered(console: WebApp) -> None:
    """Opening the page should land on whatever is being written now, not on an alphabetical first."""
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/logging_ops", cookie=cookie)).body.decode()
    assert '<option value="telegram.log" selected>' in html, (
        "telegram.log was written last in the fixture, so it is what an operator wants first")


def test_the_logging_page_shows_no_config_table(console: WebApp) -> None:
    """It used to list notify_levels.json — config nothing reads. The file is gone; so is the row."""
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/logging_ops", cookie=cookie)).body.decode()
    assert "notify_levels" not in html
    assert "owns no config file" in html


def test_only_the_logging_page_carries_the_viewer(console: WebApp) -> None:
    """Every app writes a log; the logging engine is the one you open to read them."""
    cookie = sign_in(console)
    other = console.handle(get("/db_ops/app/metrics", cookie=cookie)).body.decode()
    assert "Running log" not in other


def test_rotated_logs_are_not_offered(console: WebApp) -> None:
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/logging_ops", cookie=cookie)).body.decode()
    assert "metrics_20260819" not in html
    assert html.count("<option") == 2


def test_another_log_can_be_picked(console: WebApp) -> None:
    cookie = sign_in(console)
    html = console.handle(
        get("/db_ops/app/logging_ops", cookie=cookie, file="telegram.log")).body.decode()
    assert "queued" in html
    assert '<option value="telegram.log" selected>' in html


def test_the_log_api_pages_backwards_without_gaps(console: WebApp) -> None:
    cookie = sign_in(console)
    first = json.loads(console.handle(
        get("/db_ops/api/logs", cookie=cookie, file="metrics.log")).body)
    second = json.loads(console.handle(
        get("/db_ops/api/logs", cookie=cookie, file="metrics.log",
            before=str(first["next_before"]))).body)

    assert [line["message"] for line in first["lines"]][:2] == ["line 249", "line 248"]
    assert second["lines"][0]["message"] == "line 149"
    assert first["lines"][-1]["message"] == "line 150", "the pages must meet, not overlap"


def test_a_raw_stdout_line_survives_the_api(console: WebApp) -> None:
    cookie = sign_in(console)
    page = json.loads(console.handle(
        get("/db_ops/api/logs", cookie=cookie, file="telegram.log")).body)
    assert [line["structured"] for line in page["lines"]] == [False, True]
    assert page["lines"][0]["text"].startswith("Traceback")


@pytest.mark.parametrize("hostile", ["../../../etc/passwd", "/etc/passwd",
                                     "metrics_20260819.log", "", "metrics.log/../secret"])
def test_the_log_name_cannot_reach_outside_the_log_directory(console: WebApp,
                                                             hostile: str) -> None:
    """The name arrives from a URL, and joining it onto a directory is how that becomes a read."""
    cookie = sign_in(console)
    response = console.handle(get("/db_ops/api/logs", cookie=cookie, file=hostile))
    assert response.status == 404
    assert b"No current log named" in response.body


def test_reading_a_log_needs_a_session(console: WebApp) -> None:
    response = console.handle(get("/db_ops/api/logs", file="metrics.log"))
    assert response.status == 401


def test_a_node_with_no_logs_says_so(tmp_path: Path, _template) -> None:
    """A fresh node has written nothing yet; the panel should say that, not break."""
    template_store, data = _template
    store_path = tmp_path / "nolog.sqlite"
    shutil.copy(template_store, store_path)
    app = WebApp(auth_store=WebAuthStore(store_path), config_store=ConfigStore(store_path),
                 data_dir=data, log_dir=tmp_path / "empty-logs", settings=WebSettings())
    cookie = sign_in(app)
    html = app.handle(get("/db_ops/app/logging_ops", cookie=cookie)).body.decode()
    assert "No log files under" in html


# --------------------------------------------------------------------------- #
# An app with one config file opens it
# --------------------------------------------------------------------------- #
def test_an_app_with_a_single_config_file_opens_it_on_the_page(console: WebApp) -> None:
    """A table with one row whose only purpose is to be clicked is a click that says nothing.

    The daemon owns exactly ``app_commands.json``, and that file is what its page is *for*.
    """
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/jobs", cookie=cookie)).body.decode()

    assert "<table class=\"records\">" in html
    assert "APP-METRICS" in html and "APP-TELEGRAM" in html, "the records themselves, not a link"
    assert "app_commands" in html
    # The record links go where they always did, so the file's own page is still reachable.
    assert "/db_ops/config/app_commands.json/app_commands/APP-METRICS" in html


def test_the_single_file_block_is_named_after_the_file(console: WebApp) -> None:
    """"Config" above one file's records says less than the thing under it."""
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/jobs", cookie=cookie)).body.decode()
    assert "Scheduled app commands" in html


def test_an_app_with_several_config_files_still_lists_them(console: WebApp) -> None:
    """There the choice is real, so the table earns its place."""
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/metrics", cookie=cookie)).body.decode()

    assert "metric_definitions.json" in html and "db_instances.json" in html
    assert "/db_ops/config/metric_definitions.json" in html
    assert "INSTANCE_STATUS" not in html, "a list of files, not every record in all of them"


def test_an_app_with_no_config_says_so(console: WebApp) -> None:
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/lib", cookie=cookie)).body.decode()
    assert "owns no config file" in html


def test_the_inline_file_says_that_saving_writes_the_file(console: WebApp) -> None:
    """The same warning the file's own page carries: an edit here reaches data/*.json."""
    cookie = sign_in(console)
    html = console.handle(get("/db_ops/app/jobs", cookie=cookie)).body.decode()
    assert "rewrites" in html and "data/app_commands.json" in html


def test_a_record_edited_from_the_app_page_saves_the_same_way(console: WebApp) -> None:
    """The inline view links to the same editor, so there is one write path and not two."""
    cookie = sign_in(console)
    url = "/db_ops/config/app_commands.json/app_commands/APP-METRICS"
    html = console.handle(get(url, cookie=cookie)).body.decode()
    form = dict(_grid_fields(html))
    target = next(name for name in form if name.endswith('["display_name"]'))
    form[target] = "Collect DB metrics (edited)"
    form["csrf"] = csrf_of(console, cookie)
    assert console.handle(post_form(url, form, cookie=cookie)).status == 303

    saved = json.loads(console.config.get_item(source_file="app_commands.json",
                                               collection="app_commands",
                                               item_key="APP-METRICS")["item_json"])
    assert saved["display_name"] == "Collect DB metrics (edited)"
    on_disk = json.loads((Path(console.data_dir) / "app_commands.json").read_text(encoding="utf-8"))
    assert any(item["display_name"] == "Collect DB metrics (edited)"
               for item in on_disk["app_commands"])


def test_the_inline_view_and_the_file_page_show_the_same_records(console: WebApp) -> None:
    """One renderer. Two would eventually show the same file two different ways."""
    cookie = sign_in(console)
    inline = console.handle(get("/db_ops/app/jobs", cookie=cookie)).body.decode()
    own = console.handle(get("/db_ops/config/app_commands.json", cookie=cookie)).body.decode()

    keys = lambda html: sorted(set(re.findall(
        r'/db_ops/config/app_commands.json/app_commands/([A-Z0-9-]+)', html)))
    assert keys(inline) == keys(own) and keys(inline)
