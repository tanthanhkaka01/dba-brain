"""The login sends a user only to a path on this site, and the reports sit behind it.

Review 0.25.0: B6.1 - `next=//evil.example` passed the "starts with /" check and the login
answered `303 Location: //evil.example`. B6.2 - the report pages (server names, IPs, versions,
health) shared the console's listener and were readable by anyone who reached the port, with
directory listings on.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from db_ops.webhost.app import WebSettings, safe_next
from db_ops.webhost.server import make_handler


@pytest.mark.parametrize("bad", ["//evil.example/x", "/\\evil.example", "https://evil.example",
                                 "evil.example", "/\nLocation: x", ""])
def test_a_next_that_leaves_the_site_goes_home(bad):
    assert safe_next(bad, default="/db_ops/") == "/db_ops/"


@pytest.mark.parametrize("good", ["/db_ops/config", "/report_dba/database-inventory.html?date=2026-10-01"])
def test_a_path_on_this_site_is_kept(good):
    assert safe_next(good, default="/db_ops/") == good


def test_reports_require_a_login_by_default_and_it_can_be_switched_off():
    assert WebSettings.from_payload({}).reports_require_login is True
    assert WebSettings.from_payload({"web": {"reports_require_login": False}}).reports_require_login is False


class _Console:
    prefix = "/db_ops"

    def __init__(self, *, require=True, session=None):
        self.settings = SimpleNamespace(reports_require_login=require)
        self._session = session

    def owns(self, path):
        return path.startswith(self.prefix)

    def current_session(self, request):
        return self._session


def _get(tmp_path, console, path):
    (tmp_path / "report_dba").mkdir(exist_ok=True)
    (tmp_path / "report_dba" / "page.html").write_text("<p>report</p>", encoding="utf-8")
    handler = make_handler(directory=str(tmp_path), mount="report_dba", latest="latest.html",
                           latest_glob="*.html", root=tmp_path / "report_dba", console=console)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    try:
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(f"http://127.0.0.1:{httpd.server_address[1]}{path}") as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), b""
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_an_anonymous_report_request_is_sent_to_the_login(tmp_path):
    status, headers, _ = _get(tmp_path, _Console(), "/report_dba/page.html")

    assert status == 303
    assert headers["Location"] == "/db_ops/login?next=/report_dba/page.html"


def test_a_logged_in_user_gets_the_report(tmp_path):
    status, _, body = _get(tmp_path, _Console(session={"web_user_id": 1}), "/report_dba/page.html")

    assert status == 200 and b"report" in body


def test_switched_off_the_reports_are_open_as_before(tmp_path):
    status, _, _ = _get(tmp_path, _Console(require=False), "/report_dba/page.html")

    assert status == 200


def test_a_directory_is_never_listed(tmp_path):
    status, _, _ = _get(tmp_path, None, "/report_dba/")

    assert status == 403
