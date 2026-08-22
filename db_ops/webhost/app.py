"""The web console: login, session cookie, the dashboard, config editing, and running an app.

The web host used to serve files and nothing else. This is the part of it that has state — who is
logged in, what they are allowed to do, and what they change — and it is deliberately kept as a
**pure request -> response function** rather than a set of methods on an ``http.server`` handler:

    :meth:`WebApp.handle` takes a :class:`Request` and returns a :class:`Response`.

That is what lets the whole console be tested offline, with no socket, no browser and no live
store — the same rule the rest of the suite follows. :mod:`db_ops.webhost.server` is the only
place that knows about sockets, and all it does is translate.

**Sessions are cookies over a store row, three months long.** The cookie carries `Max-Age`, not a
session lifetime, which is the entire reason closing Chrome or Firefox does not log anyone out:
a session cookie (no Max-Age) is discarded on exit by design. The cookie holds a random token;
the store holds only its fingerprint (:mod:`db_ops.lib.web_auth`), so the table cannot be replayed.

**Authorisation is a level, 1..100**, the same ladder ``telegram_users.user_type`` uses, checked
against the ``min_level_*`` settings in ``data/webhost_config.json``. Viewing needs the lowest;
editing config and running an app need more.

**This module decides nothing about config or about running.** Editing goes through
:mod:`db_ops.db.config_edit`, which is also what the CLI calls, so a record saved from a browser
is validated by exactly the rules a record saved from a shell is. "Run now" writes a row through
:mod:`db_ops.db.run_requests` and the **daemon** starts the command — the console never spawns a
process, because the daemon owns the reaper, the log scope and the forwarded key, and a second
executor would have none of them.

Two rules hold for every state-changing request here, and both are checked before the work:

* **the CSRF token** must match the one issued with the session. Combined with ``SameSite=Lax`` on
  the cookie, a cross-site POST cannot both arrive with credentials and carry the token.
* **the level** must reach the gate for that action. Refusals are 403 with the reason, not a
  redirect: an operator who cannot do a thing needs to be told which level it takes.
"""

from __future__ import annotations

import importlib.util

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from db_ops.lib import web_auth
from db_ops.webhost import pages

#: Where the console lives, under the same server that publishes the reports. A prefix rather than
#: the root so the existing ``/report_dba/`` URLs keep working untouched.
DEFAULT_MOUNT = "db_ops"

_COOKIE_MAX_BYTES = 4096
#: Bound on a submitted config record. Generous next to the largest real one (a metric definition
#: with every variant, ~6 KB) and small enough that a runaway paste cannot fill the store.
_MAX_PAYLOAD_BYTES = 256 * 1024

#: How many log lines a page holds, and the ceiling a caller may ask for. 100 is what fits on a
#: screen without scrolling past it; the ceiling stops a hand-written request pulling a whole log
#: into memory in one response.
LOG_PAGE_SIZE = 100
LOG_PAGE_MAX = 1000

#: The app whose page carries the log viewer. See ``WebApp._logs_panel``.
LOG_VIEWER_APP = "logging_ops"



def _app_is_installed(block: dict[str, Any]) -> bool:
    """Is the package this block describes actually present in this install?

    The block definitions come from configuration, which describes the whole toolkit; a
    distribution may carry a subset of it. A console that lists an app which is not installed sends
    somebody to a page that cannot work, and the same defect was already found and fixed once in
    the `db-ops` dispatcher, which advertised twelve apps and could run five.

    A block naming nothing, or naming something that is not a package, is kept: a missing
    `app_code` is a configuration question and hiding the block would hide the question.
    """
    code = str(block.get("app_code") or "").strip()
    if not code:
        return True
    try:
        return importlib.util.find_spec(f"db_ops.{code}") is not None
    except (ImportError, AttributeError, ValueError):
        # `find_spec` raises rather than returning None when a parent package is missing.
        return False


class WebAppError(RuntimeError):
    """The console cannot serve a request as configured."""


class Refused(Exception):
    """A request is refused with a status and a reason the operator can act on."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Request:
    """One HTTP request, reduced to what the console actually reads."""

    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    client_ip: str = ""

    @property
    def form_all(self) -> dict[str, list[str]]:
        """The urlencoded body as ``parse_qs`` gives it — every value, not just the first.

        The config grid needs this: a checkbox posts a hidden "false" *and* its own "true" when
        ticked, and only the last value is the answer. Flattening to the first would make every
        checkbox permanently false.
        """
        content_type = str(self.headers.get("content-type", "")).lower()
        if "application/x-www-form-urlencoded" not in content_type:
            return {}
        return parse_qs(self.body.decode("utf-8", errors="replace"), keep_blank_values=True)

    @property
    def form(self) -> dict[str, str]:
        """A urlencoded body as a flat mapping. Empty for anything else."""
        return {key: values[0] for key, values in self.form_all.items()}

    @property
    def json_body(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.body.decode("utf-8") or "{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def cookie(self, name: str) -> str:
        """One cookie value, parsed from the raw header.

        Hand-parsed rather than via ``http.cookies``: that module raises on a malformed cookie,
        and a browser carrying some other site's junk in the header must not turn every request
        into a 500.
        """
        raw = str(self.headers.get("cookie", ""))
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key.strip() == name:
                return value.strip()
        return ""

    @property
    def first(self) -> dict[str, str]:
        """The query string as a flat mapping."""
        return {key: values[0] for key, values in self.query.items() if values}


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def html(cls, markup: str, *, status: int = 200) -> "Response":
        return cls(status=status, body=markup.encode("utf-8"))

    @classmethod
    def json(cls, payload: Any, *, status: int = 200) -> "Response":
        return cls(status=status,
                   body=json.dumps(payload, ensure_ascii=False, default=str, indent=1).encode("utf-8"),
                   content_type="application/json; charset=utf-8")

    @classmethod
    def redirect(cls, location: str, *, status: int = 303) -> "Response":
        # 303 rather than 302: after a POST the browser must follow with GET, which is what stops
        # a refresh on the dashboard re-submitting the form that got there.
        return cls(status=status, body=b"", headers=[("Location", location)])

    def with_cookie(self, name: str, value: str, *, max_age: int | None = None,
                    secure: bool = False, samesite: str = "Lax", path: str = "/") -> "Response":
        parts = [f"{name}={value}", f"Path={path}", "HttpOnly", f"SameSite={samesite}"]
        if max_age is not None:
            # Max-Age, not Expires, and never omitted: a cookie without one is a *session* cookie
            # that the browser throws away when it closes. That single missing attribute is the
            # difference between "log in once a quarter" and "log in every morning".
            parts.append(f"Max-Age={int(max_age)}")
        if secure:
            parts.append("Secure")
        header = "; ".join(parts)
        if len(header.encode("utf-8")) > _COOKIE_MAX_BYTES:
            raise WebAppError("session cookie is too large to set.")
        self.headers.append(("Set-Cookie", header))
        return self


@dataclass(frozen=True)
class WebSettings:
    """The ``web`` block of ``data/webhost_config.json``, with defaults for a missing file."""

    mount: str = DEFAULT_MOUNT
    cookie_name: str = "db_ops_session"
    session_days: int = web_auth.DEFAULT_SESSION_DAYS
    cookie_secure: bool = False
    cookie_samesite: str = "Lax"
    max_failed_logins: int = 8
    lockout_minutes: int = 15
    min_level_view: int = 1
    min_level_edit: int = 50
    min_level_run: int = 50
    min_level_admin: int = 90

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "WebSettings":
        block = dict((payload or {}).get("web") or {})
        defaults = cls()
        return cls(
            mount=str(block.get("mount") or defaults.mount).strip("/") or defaults.mount,
            cookie_name=str(block.get("cookie_name") or defaults.cookie_name),
            session_days=int(block.get("session_days") or defaults.session_days),
            cookie_secure=bool(block.get("cookie_secure", defaults.cookie_secure)),
            cookie_samesite=str(block.get("cookie_samesite") or defaults.cookie_samesite),
            max_failed_logins=int(block.get("max_failed_logins", defaults.max_failed_logins)),
            lockout_minutes=int(block.get("lockout_minutes", defaults.lockout_minutes)),
            min_level_view=int(block.get("min_level_view", defaults.min_level_view)),
            min_level_edit=int(block.get("min_level_edit", defaults.min_level_edit)),
            min_level_run=int(block.get("min_level_run", defaults.min_level_run)),
            min_level_admin=int(block.get("min_level_admin", defaults.min_level_admin)),
        )


class WebApp:
    """The console. Holds the stores it reads and nothing per-request."""

    def __init__(self, *, auth_store, config_store, ops_store=None, request_store=None,
                 data_dir: str | Path | None = None, log_dir: str | Path | None = None,
                 settings: WebSettings | None = None, now: Any = None) -> None:
        self.auth = auth_store
        self.config = config_store
        # The DbOpsStore that job_runs lives in. Optional so a test can build a console without
        # one; the dashboard then shows the schedule without the run history rather than failing.
        self.ops_store = ops_store
        # The "run now" queue. Optional for the same reason — without it the run buttons are
        # absent rather than broken.
        self.requests = request_store
        self.data_dir = Path(data_dir) if data_dir else None
        # Where this node writes its logs. The console reads them straight off disk rather than
        # out of the store: these are the running logs, written by every app process, and nothing
        # ships them anywhere.
        self.log_dir = Path(log_dir) if log_dir else None
        self.settings = settings or WebSettings()
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #
    @property
    def prefix(self) -> str:
        return f"/{self.settings.mount}"

    def owns(self, path: str) -> bool:
        """Is this a console URL? Everything else belongs to the static report server."""
        return path == self.prefix or path.startswith(f"{self.prefix}/")

    def handle(self, request: Request) -> Response:
        """Route one request. Never raises: an unexpected failure is a page, not a traceback.

        The catch-all is deliberate. This process also serves the inventory reports, and an
        exception escaping into ``http.server`` closes the connection with no response at all —
        which looks to an operator exactly like the worker being down.
        """
        route = request.path[len(self.prefix):].strip("/")
        segments = [part for part in route.split("/") if part]
        try:
            return self._route(segments, request)
        except Refused as exc:
            if segments[:1] == ["api"]:
                return Response.json({"error": exc.message}, status=exc.status)
            return Response.html(
                pages.error_page("Not allowed", exc.message, prefix=self.prefix),
                status=exc.status)
        except Exception as exc:  # noqa: BLE001 - see docstring.
            return Response.html(pages.error_page(
                "Something went wrong", str(exc), prefix=self.prefix), status=500)

    def _route(self, segments: list[str], request: Request) -> Response:
        head = segments[0] if segments else ""
        if head == "login":
            return self._post_login(request) if request.method == "POST" else self._get_login(request)
        if head == "logout":
            return self._post_logout(request)

        session = self.current_session(request)
        if session is None:
            # Everything past this point needs an account. An API path answers 401 with JSON so a
            # fetch() sees an error instead of parsing a login page as data.
            if head == "api":
                return Response.json({"error": "not authenticated"}, status=401)
            return Response.redirect(f"{self.prefix}/login?next={quote(request.path)}")

        if not segments or head == "dashboard":
            return self._get_overview(request, session)
        if head == "app":
            return self._get_app(segments[1:], request, session)
        if head == "config":
            return self._route_config(segments[1:], request, session)
        if head == "apps":
            return self._route_apps(segments[1:], request, session)
        if head == "api":
            return self._route_api(segments[1:], request, session)
        return Response.html(
            pages.error_page("Not found", f"No console page at /{'/'.join(segments)}.",
                             prefix=self.prefix),
            status=404)

    def _route_api(self, rest: list[str], request: Request, session: dict[str, Any]) -> Response:
        name = rest[0] if rest else ""
        if name == "session":
            return Response.json({
                "username": session["username"],
                "display_name": session["display_name"],
                "level": session["user_level"],
                "expires_at": session["expires_at"],
            })
        if name == "apps":
            return Response.json({"apps": self.app_blocks()})
        if name == "config":
            return Response.json(self._api_config(request))
        if name == "logs":
            return Response.json(self._api_logs(request))
        return Response.json({"error": f"no api endpoint /{'/'.join(rest)}"}, status=404)

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    def current_session(self, request: Request) -> dict[str, Any] | None:
        token = request.cookie(self.settings.cookie_name)
        if not token:
            return None
        return self.auth.resolve_session(token)

    def _get_login(self, request: Request) -> Response:
        if self.current_session(request) is not None:
            return Response.redirect(f"{self.prefix}/")
        return Response.html(pages.login_page(
            prefix=self.prefix,
            next_url=request.first.get("next", ""),
            has_users=self.auth.has_any_user(),
        ))

    def _post_login(self, request: Request) -> Response:
        form = request.form
        user, reason = self.auth.authenticate(
            username=form.get("username", ""),
            password=form.get("password", ""),
            client_ip=request.client_ip,
            user_agent=request.headers.get("user-agent", ""),
            max_failed=self.settings.max_failed_logins,
            lockout_minutes=self.settings.lockout_minutes,
        )
        if user is None:
            from db_ops.db.web_auth_store import REASON_LOCKED

            # One message for every failure except the lockout. A lockout has to be
            # distinguishable or the user retries forever against a door that will not open for
            # fifteen minutes; the rest stay identical so the form cannot be used to find out who
            # has an account.
            message = ("Too many failed attempts. This account is locked for "
                       f"{self.settings.lockout_minutes} minutes.") if reason == REASON_LOCKED \
                else "Wrong username or password."
            # A failed login must not be instant: it is the cheapest possible rate limit against
            # a script, and a person notices nothing.
            time.sleep(0.4)
            return Response.html(pages.login_page(
                prefix=self.prefix, next_url=form.get("next", ""),
                has_users=self.auth.has_any_user(), error=message,
                username=form.get("username", "")), status=401)

        issued = self.auth.issue_session(
            web_user_id=int(user["web_user_id"]),
            session_days=self.settings.session_days,
            client_ip=request.client_ip,
            user_agent=request.headers.get("user-agent", ""),
        )
        target = form.get("next") or f"{self.prefix}/"
        if not target.startswith("/"):
            # Never redirect to an absolute URL from a form field: that is an open redirect, and
            # a login page is exactly where one gets used.
            target = f"{self.prefix}/"
        return Response.redirect(target).with_cookie(
            self.settings.cookie_name, issued["token"],
            max_age=issued["max_age_seconds"],
            secure=self.settings.cookie_secure,
            samesite=self.settings.cookie_samesite,
        )

    def _post_logout(self, request: Request) -> Response:
        token = request.cookie(self.settings.cookie_name)
        if token:
            self.auth.revoke_session(token, reason="logout")
        # Max-Age=0 clears it. Setting the same attributes it was written with matters: a browser
        # matches a cookie for deletion on name, path and domain, not on value.
        return Response.redirect(f"{self.prefix}/login").with_cookie(
            self.settings.cookie_name, "", max_age=0,
            secure=self.settings.cookie_secure, samesite=self.settings.cookie_samesite)

    # ------------------------------------------------------------------ #
    # Guards
    # ------------------------------------------------------------------ #
    def _require_level(self, session: dict[str, Any], needed: int, action: str) -> None:
        if int(session["user_level"]) < int(needed):
            raise Refused(403, f"{action} needs level {needed}; "
                               f"{session['username']} is level {session['user_level']}.")

    def _require_csrf(self, request: Request, session: dict[str, Any]) -> None:
        """Every state-changing POST carries the session's token.

        The cookie is ``SameSite=Lax``, so a cross-site POST already arrives without credentials
        and fails at the session check. This is the second lock, for the cases SameSite does not
        cover — a same-site page an attacker managed to get content onto.
        """
        supplied = request.form.get("csrf") or request.json_body.get("csrf") or \
            request.headers.get("x-csrf-token", "")
        if not web_auth.tokens_match(str(supplied), str(session.get("csrf_token") or "")):
            raise Refused(403, "This form has expired or came from somewhere else. "
                               "Reload the page and try again.")

    # ------------------------------------------------------------------ #
    # Config: read, edit, retire
    # ------------------------------------------------------------------ #
    def _route_config(self, rest: list[str], request: Request,
                      session: dict[str, Any]) -> Response:
        from db_ops.db.config_edit import ConfigEditError

        if not rest:
            return Response.redirect(f"{self.prefix}/")
        source_file = rest[0]

        if request.method == "POST":
            self._require_csrf(request, session)
            self._require_level(session, self.settings.min_level_edit, "Editing config")
            try:
                return self._post_config(source_file, rest[1:], request, session)
            except ConfigEditError as exc:
                return Response.html(
                    pages.error_page("That change was refused", str(exc), prefix=self.prefix,
                                     back=f"{self.prefix}/config/{quote(source_file)}"),
                    status=400)

        if len(rest) == 1:
            return self._get_config_file(source_file, request, session)
        if len(rest) == 3:
            collection, item_key = rest[1], rest[2]
            if item_key == "new":
                return self._get_config_record(source_file, collection, None, session)
            return self._get_config_record(source_file, collection, item_key, session)
        return Response.html(
            pages.error_page("Not found", "No such config page.", prefix=self.prefix), status=404)

    def _config_view(self, source_file: str, *, include_retired: bool = False) -> dict[str, Any]:
        """One config file as the page needs it: its records grouped by collection.

        Shared by the file's own page and by an app page that opens its only file inline, so the
        two cannot drift into showing different things about the same file.
        """
        source = self._source_row(source_file)
        rows = self.config.list_items(source_file=source_file, include_inactive=include_retired)
        if not rows and source is None:
            raise Refused(404, f"{source_file} is not mirrored into the store. "
                               "Run sync-config, or check data/config_catalog.json.")
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(str(row["collection"]), []).append({
                "item_key": str(row["item_key"]),
                "label": str(row["label"] or ""),
                "revision": int(row["revision"]),
                "is_active": int(row["is_active"]),
                "updated_at": str(row["updated_at"]),
                "updated_by": str(row["updated_by"] or ""),
                "payload": json.loads(row["item_json"]),
            })
        return {
            "source_file": source_file,
            "display_name": str(source["display_name"]) if source is not None else source_file,
            "description": str(source["description"]) if source is not None else "",
            "app_code": str(source["app_code"]) if source is not None else "",
            "groups": groups,
            "showing_retired": include_retired,
        }

    def _get_config_file(self, source_file: str, request: Request,
                         session: dict[str, Any]) -> Response:
        from db_ops.db.config_sync import DOCUMENT_COLLECTION

        view = self._config_view(source_file,
                                 include_retired=request.first.get("retired") == "1")
        return Response.html(pages.config_file_page(
            prefix=self.prefix, session=session, blocks=self.app_blocks(),
            source_file=view["source_file"], display_name=view["display_name"],
            description=view["description"], app_code=view["app_code"],
            groups=view["groups"], document_collection=DOCUMENT_COLLECTION,
            can_edit=self._can(session, self.settings.min_level_edit),
            showing_retired=view["showing_retired"],
        ))

    def _get_config_record(self, source_file: str, collection: str, item_key: str | None,
                           session: dict[str, Any]) -> Response:
        from db_ops.db.config_edit import record_history
        from db_ops.db.config_sync import DOCUMENT_COLLECTION, collection_spec, spec_for

        spec = spec_for(source_file, data_dir=self.data_dir)
        column = collection_spec(spec, collection)
        payload: Any = {}
        history: list[dict[str, Any]] = []
        if item_key is not None:
            row = self.config.get_item(source_file=source_file, collection=collection,
                                       item_key=item_key)
            if row is None:
                raise Refused(404, f"No active record '{item_key}' in "
                                   f"{source_file}:{collection}.")
            payload = json.loads(row["item_json"])
            history = record_history(self.config, source_file=source_file, collection=collection,
                                     item_key=item_key)
        elif column is not None:
            # A new record starts from the shape of an existing one: an empty box would make the
            # operator rediscover which fields the collection needs, and getting one wrong is a
            # refusal at save time rather than a hint at edit time.
            payload = self._blank_record(source_file, collection, column)

        source = self._source_row(source_file)
        return Response.html(pages.config_record_page(
            prefix=self.prefix, session=session, blocks=self.app_blocks(),
            app_code=str(source["app_code"]) if source is not None else "",
            source_file=source_file, collection=collection,
            item_key=item_key, payload=payload, history=history,
            key_fields=list(column.key_fields) if column is not None else [],
            is_document=(collection == DOCUMENT_COLLECTION),
            can_edit=int(session["user_level"]) >= self.settings.min_level_edit,
        ))

    def _post_config(self, source_file: str, rest: list[str], request: Request,
                     session: dict[str, Any]) -> Response:
        from db_ops.db import config_edit

        form = request.form
        collection = rest[0] if rest else form.get("collection", "")
        item_key = rest[1] if len(rest) > 1 else None
        action = rest[2] if len(rest) > 2 else form.get("action", "save")
        back = f"{self.prefix}/config/{quote(source_file)}"

        if action == "delete":
            if item_key is None:
                raise Refused(400, "Which record should be retired?")
            config_edit.delete_record(
                self.config, source_file=source_file, collection=collection, item_key=item_key,
                actor=session["username"], data_dir=self.data_dir,
                note=form.get("note", ""))
            return Response.redirect(f"{back}?retired_ok=1")

        payload = self._submitted_payload(request)

        result = config_edit.save_record(
            self.config, source_file=source_file, collection=collection, payload=payload,
            item_key=item_key if item_key not in (None, "new") else None,
            actor=session["username"], data_dir=self.data_dir, note=form.get("note", ""))
        return Response.redirect(
            f"{back}/{quote(collection)}/{quote(result['item_key'])}?saved={result['action']}")

    def _submitted_payload(self, request: Request) -> Any:
        """The record the operator just edited — from the field grid, or from the JSON box.

        Both forms post to the same URL, and which one was used is read off the submission itself
        rather than from a mode flag: the grid's fields are all named ``f:...``, the JSON box posts
        a single ``payload``. A flag would be a third thing that can disagree with the other two.
        """
        from db_ops.lib.record_form import RecordFormError, has_form_fields, rebuild

        submitted = request.form_all
        if len(request.body) > _MAX_PAYLOAD_BYTES:
            raise Refused(413, f"That record is larger than {_MAX_PAYLOAD_BYTES // 1024} KB.")

        if has_form_fields(submitted):
            try:
                return rebuild(submitted)
            except RecordFormError as exc:
                raise Refused(400, str(exc)) from exc

        raw = request.form.get("payload", "")
        try:
            return json.loads(raw or "{}")
        except ValueError as exc:
            raise Refused(400, f"That is not valid JSON: {exc}") from exc

    def _blank_record(self, source_file: str, collection: str, column) -> dict[str, Any]:
        """A template for a new record: the fields an existing one has, emptied."""
        rows = self.config.list_items(source_file=source_file, collection=collection)
        if not rows:
            return {field: "" for field in column.key_fields}
        sample = json.loads(rows[0]["item_json"])
        blank: dict[str, Any] = {}
        for key, value in sample.items():
            if isinstance(value, bool):
                blank[key] = value
            elif isinstance(value, (int, float)):
                blank[key] = 0
            elif isinstance(value, list):
                blank[key] = []
            elif isinstance(value, dict):
                blank[key] = value  # a nested policy block is a shape, not a value: keep it
            else:
                blank[key] = ""
        return blank

    def _source_row(self, source_file: str) -> Any | None:
        for row in self.config.list_sources():
            if str(row["source_file"]) == source_file:
                return row
        return None

    def _api_config(self, request: Request) -> dict[str, Any]:
        first = request.first
        rows = self.config.list_items(
            app_code=first.get("app_code") or None,
            source_file=first.get("source_file") or None,
            collection=first.get("collection") or None,
            include_inactive=first.get("include_inactive") == "1",
        )
        return {"items": [{
            "config_item_id": row["config_item_id"],
            "app_code": row["app_code"],
            "source_file": row["source_file"],
            "collection": row["collection"],
            "item_key": row["item_key"],
            "label": row["label"],
            "revision": row["revision"],
            "is_active": row["is_active"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
            "payload": json.loads(row["item_json"]),
        } for row in rows]}

    # ------------------------------------------------------------------ #
    # Running an app
    # ------------------------------------------------------------------ #
    def _route_apps(self, rest: list[str], request: Request,
                    session: dict[str, Any]) -> Response:
        if request.method != "POST" or len(rest) != 2 or rest[1] not in {"run", "cancel"}:
            return Response.html(
                pages.error_page("Not found", "No such app action.", prefix=self.prefix),
                status=404)
        self._require_csrf(request, session)
        self._require_level(session, self.settings.min_level_run, "Running an app")
        if self.requests is None:
            raise Refused(503, "The run queue is not available on this node.")

        app_command_id = rest[0]
        # Back to wherever the button was pressed. Sending every run back to the overview would
        # take the operator off the app they were reading, which is where the result shows up.
        #
        # The target is checked against the apps that exist rather than merely prefix-matched:
        # "app/" is a prefix of "app/../../elsewhere" too, and a field that names where to go next
        # is a redirect however small it looks.
        blocks = self.app_blocks()
        known_apps = {str(item["app_code"]) for item in blocks}
        origin = request.form.get("from", "")
        target_app = origin[4:] if origin.startswith("app/") else ""
        home = (f"{self.prefix}/app/{quote(target_app)}" if target_app in known_apps
                else f"{self.prefix}/")
        if rest[1] == "cancel":
            self.requests.cancel_request(int(request.form.get("request_id") or 0),
                                         actor=session["username"])
            return Response.redirect(f"{home}?cancelled=1")

        known = {str(item["app_command_id"]) for block in blocks
                 for item in block["commands"] if not item.get("missing")}
        if app_command_id not in known:
            raise Refused(404, f"No app command '{app_command_id}' is configured.")
        answer = self.requests.request_run(
            app_command_id=app_command_id, requested_by=session["username"],
            source="console", note=f"Requested from the console by {session['username']}.")
        flag = "queued" if answer["created"] else "already_queued"
        return Response.redirect(f"{home}?{flag}={quote(app_command_id)}")

    # ------------------------------------------------------------------ #
    # Dashboard data
    # ------------------------------------------------------------------ #
    def app_blocks(self) -> list[dict[str, Any]]:
        """One block per db_ops app, with its schedule and how its last runs went.

        Four sources, joined here: the block definitions (``webhost_config.json``), the schedules
        (``app_commands.json``), the run history (``job_runs``), and any queued run. The first two
        are read through the **store** rather than off disk, so the console shows what the estate
        is configured with rather than what this checkout happens to contain.
        """
        blocks = [block for block in self._blocks_from_store() if _app_is_installed(block)]
        commands = {row["app_command_id"]: row for row in self._app_commands_from_store()}
        status_by_code = self._ops_status_by_code()
        queued = self._open_requests()

        result: list[dict[str, Any]] = []
        for block in blocks:
            entries: list[dict[str, Any]] = []
            for code in block.get("app_command_ids") or []:
                command = commands.get(code)
                if command is None:
                    # Configured to own a command that no longer exists. Shown rather than hidden:
                    # a block quietly losing its schedule is how "why is nothing running" starts.
                    entries.append({"app_command_id": code, "missing": True})
                    continue
                window = command.get("time_window") or {}
                entries.append({
                    "app_command_id": code,
                    # Which block owns it, so the Run button can send the operator back to the
                    # page they pressed it on rather than to the overview.
                    "app_code": block["app_code"],
                    "display_name": command.get("display_name") or command.get("app_name") or code,
                    "active": bool(command.get("active", True)),
                    "node_role": command.get("node_role") or "",
                    "command_text": command.get("command_text") or "",
                    "repeat_interval": window.get("repeat_interval"),
                    "retry_interval": window.get("retry_interval"),
                    "timeout": window.get("timeout"),
                    "from_hour": window.get("from_hour"),
                    "to_hour": window.get("to_hour"),
                    "schedule_text": _schedule_text(window),
                    "status": status_by_code.get(code, {}),
                    "queued": queued.get(code),
                })
            result.append({
                "app_code": block["app_code"],
                "ord": block.get("ord") or 0,
                "display_name": block.get("display_name") or block["app_code"],
                "summary": block.get("summary") or "",
                "doc": block.get("doc") or "",
                "commands": entries,
                "config": self._config_counts(block["app_code"]),
            })
        result.sort(key=lambda item: (int(item["ord"] or 0), item["app_code"]))
        return result

    def _blocks_from_store(self) -> list[dict[str, Any]]:
        rows = self.config.list_items(source_file="webhost_config.json", collection="apps")
        return [json.loads(row["item_json"]) for row in rows]

    def _app_commands_from_store(self) -> list[dict[str, Any]]:
        rows = self.config.list_items(source_file="app_commands.json", collection="app_commands")
        return [json.loads(row["item_json"]) for row in rows]

    def _config_counts(self, app_code: str) -> list[dict[str, Any]]:
        """What config this app owns, per file — the entry point for editing it."""
        from db_ops.db.config_store import DOCUMENT_COLLECTION

        counts: dict[str, dict[str, Any]] = {}
        for row in self.config.list_items(app_code=app_code):
            entry = counts.setdefault(str(row["source_file"]), {
                "source_file": row["source_file"],
                "display_name": row["source_display_name"],
                "records": 0,
            })
            # The __document__ row is the file's leftover settings, not a record an operator
            # thinks of as a row; counting it would report "1 record" for a file with none.
            if str(row["collection"]) != DOCUMENT_COLLECTION:
                entry["records"] += 1
        return sorted(counts.values(), key=lambda item: str(item["source_file"]))

    def _ops_status_by_code(self) -> dict[str, dict[str, Any]]:
        if self.ops_store is None or self.data_dir is None:
            return {}
        try:
            from db_ops.db import ops_status

            status = ops_status.build_ops_status(
                store=self.ops_store, data_dir=self.data_dir, window_hours=24)
        except Exception:  # noqa: BLE001 - the dashboard must render without the run history.
            return {}
        return {item["app"]: item for item in status.get("apps", [])}

    def _open_requests(self) -> dict[str, dict[str, Any]]:
        if self.requests is None:
            return {}
        try:
            return {code: {"request_id": int(row["request_id"]),
                           "status": str(row["status"]),
                           "requested_by": str(row["requested_by"] or ""),
                           "requested_at": str(row["requested_at"])}
                    for code, row in self.requests.open_requests().items()}
        except Exception:  # noqa: BLE001 - a queue that cannot be read costs a badge, not the page.
            return {}

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #
    def _get_overview(self, request: Request, session: dict[str, Any]) -> Response:
        return Response.html(pages.overview_page(
            prefix=self.prefix,
            session=session,
            blocks=self.app_blocks(),
            can_edit=self._can(session, self.settings.min_level_edit),
            can_run=self._can(session, self.settings.min_level_run) and self.requests is not None,
            notice=_dashboard_notice(request.first),
            generated_at=self._now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        ))

    def _get_app(self, rest: list[str], request: Request, session: dict[str, Any]) -> Response:
        """One app's page. An unknown code is a 404, not a silent fall back to the first app."""
        blocks = self.app_blocks()
        app_code = rest[0] if rest else ""
        block = next((item for item in blocks if item["app_code"] == app_code), None)
        if block is None:
            raise Refused(404, f"No app called '{app_code}'. The console knows: "
                               + ", ".join(item["app_code"] for item in blocks) + ".")
        return Response.html(pages.app_page(
            prefix=self.prefix,
            session=session,
            blocks=blocks,
            block=block,
            can_edit=self._can(session, self.settings.min_level_edit),
            can_run=self._can(session, self.settings.min_level_run) and self.requests is not None,
            notice=_dashboard_notice(request.first),
            logs=self._logs_panel(app_code, request),
            config_inline=self._inline_config(block, request),
        ))

    def _inline_config(self, block: dict[str, Any], request: Request) -> dict[str, Any] | None:
        """The app's config, opened here, when there is only one file to open.

        A table with a single row whose only purpose is to be clicked is a click that tells the
        operator nothing. Where an app owns exactly one config file — the daemon and its
        ``app_commands.json``, the SRE app and its lab databases — the file is what the page is
        *for*, so it is shown rather than linked. Apps with several keep the table, because there
        the choice is real.
        """
        config = block.get("config") or []
        if len(config) != 1:
            return None
        return self._config_view(str(config[0]["source_file"]),
                                 include_retired=request.first.get("retired") == "1")

    def _logs_panel(self, app_code: str, request: Request) -> dict[str, Any] | None:
        """The log viewer, for the app whose subject *is* the logs.

        Only ``logging_ops`` gets it. Every app writes a log, but the logging engine is the one an
        operator opens *to read them* — bolting a viewer onto all fourteen pages would put the same
        panel fourteen times and still leave nothing at the place people look.
        """
        from db_ops.lib.log_tail import list_logs, read_tail, resolve_log

        if app_code != LOG_VIEWER_APP:
            return None
        available = list_logs(self._log_dir())
        if not available:
            return {"files": [], "selected": "", "lines": [], "next_before": None,
                    "error": f"No log files under {self._log_dir()}."}

        wanted = request.first.get("file", "")
        selected = wanted if any(item["name"] == wanted for item in available) \
            else available[0]["name"]
        page = read_tail(resolve_log(self._log_dir(), selected), limit=LOG_PAGE_SIZE)
        return {
            "files": available,
            "selected": selected,
            "lines": [line.as_dict() for line in page["lines"]],
            "next_before": page["next_before"],
            "error": "",
        }

    def _can(self, session: dict[str, Any], needed: int) -> bool:
        return int(session["user_level"]) >= int(needed)

    # ------------------------------------------------------------------ #
    # Logs
    # ------------------------------------------------------------------ #
    def _api_logs(self, request: Request) -> dict[str, Any]:
        """One page of a log file, newest line first.

        ``before`` is the byte offset the previous page returned, so scrolling asks for the lines
        *before* the oldest one on screen. A byte offset rather than a page number because the file
        is being appended to while it is read: any cursor counted in lines would shift under the
        reader every time an app logged something.
        """
        from db_ops.lib.log_tail import read_tail, resolve_log

        first = request.first
        limit = max(1, min(int(first.get("limit") or LOG_PAGE_SIZE), LOG_PAGE_MAX))
        before = first.get("before")
        try:
            path = resolve_log(self._log_dir(), first.get("file", ""))
        except FileNotFoundError as exc:
            raise Refused(404, str(exc)) from exc

        page = read_tail(path, limit=limit,
                         before=int(before) if str(before or "").isdigit() else None)
        return {
            "file": path.name,
            "lines": [line.as_dict() for line in page["lines"]],
            "next_before": page["next_before"],
            "exhausted": page["exhausted"],
            "size": page["size"],
        }

    def _log_dir(self) -> Path:
        """Where the logs are. Falls back beside ``data/`` when no config was handed over."""
        if self.log_dir is not None:
            return self.log_dir
        from db_ops.lib.paths import TOOL_ROOT

        return Path(TOOL_ROOT) / "logs"


def _dashboard_notice(query: dict[str, str]) -> str:
    """What just happened, carried back through the redirect after a POST."""
    if query.get("queued"):
        return (f"{query['queued']} is queued. The daemon starts it on its next scan — "
                "the run appears here and in job_runs like any other.")
    if query.get("already_queued"):
        return f"{query['already_queued']} was already queued; it has not been queued twice."
    if query.get("cancelled"):
        return "The queued run was cancelled."
    return ""


def _schedule_text(window: dict[str, Any]) -> str:
    """A schedule in the words an operator uses: "every 10s", "runs once and stays up".

    ``repeat_interval: 0`` genuinely means run-once-and-stay-up (the web host itself), and
    ``-1`` means manual only. Rendering either as "every 0 seconds" is how a reader concludes the
    scheduler is broken.
    """
    interval = window.get("repeat_interval")
    if interval is None:
        return "no schedule"
    interval = int(interval)
    if interval < 0:
        return "manual only"
    if interval == 0:
        base = "runs once and stays up"
    elif interval < 60:
        base = f"every {interval}s"
    elif interval % 3600 == 0:
        base = f"every {interval // 3600}h"
    elif interval % 60 == 0:
        base = f"every {interval // 60}m"
    else:
        base = f"every {interval}s"
    from_hour, to_hour = window.get("from_hour"), window.get("to_hour")
    if from_hour is not None and to_hour is not None and not (int(from_hour) == 0 and int(to_hour) == 23):
        base += f", {int(from_hour):02d}:00-{int(to_hour):02d}:59"
    return base
