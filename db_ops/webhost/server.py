"""Static web host built on the stdlib ``http.server``.

Responsibilities:
* ``build_webroot``     — lay out a web root whose ``<mount>`` entry points at the report
  directory, so the served URL gets the desired path prefix (e.g. ``/report_dba/``).
* ``refresh_latest``    — keep a stable symlink (e.g. ``database-inventory.html``) pointing
  at the newest timestamped report, so a fixed link always serves the latest report.
* ``serve``             — run a threaded HTTP server and refresh the ``latest`` symlink in a
  background thread.

Symlinks are the preferred mechanism and neither one is required. On Linux they always work; on
Windows both need `SeCreateSymbolicLinkPrivilege`, which an ordinary account does not hold, and
until 2026-09-05 the failure was logged and shrugged off — the note here said the server "still
serves the timestamped files directly", which was wrong twice over:

* the **mount** symlink is what put the report directory under ``/<mount>/``. Without it the
  server served an empty ``webroot`` and *every* report URL was 404, timestamped or not;
* the **latest link** is the fixed URL the reports, the SLA page and `/spbot_self_status` all
  hand out, so it 404'd on every Windows node.

Both now degrade to something that works: the mount is resolved in-process by stripping the URL
prefix, and the latest link falls back to copying the newest report over it.
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

# Report stamps are ``YYYYMMDD_HHMMSS`` (15 chars) prefixing the filename; they sort by time.
_STAMP_LEN = 15


def _newest_match(root: Path, pattern: str) -> Path | None:
    """Newest file in ``root`` matching ``pattern`` (timestamp-prefixed name sorts by time)."""
    candidates = [p for p in root.glob(pattern) if p.is_file() and not p.is_symlink()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.name)


def parse_date_param(value: str) -> str | None:
    """Turn a ``?date=`` query value into a comparable ``YYYYMMDD_HHMMSS`` stamp.

    Accepts a bare date ``2026-06-23`` (treated as the end of that day, so the match
    is the *latest* snapshot of the day) or a datetime ``2026-06-23T18:00:00`` (also
    space-separated, with partial ``HH`` / ``HH:MM`` times). Returns ``None`` when the
    value cannot be parsed, so the caller can fall back to serving the latest report.
    """
    v = value.strip().replace(" ", "T")
    if not v:
        return None
    date_part, _, time_part = v.partition("T")
    try:
        day = _dt.datetime.strptime(date_part, "%Y-%m-%d").date()
    except ValueError:
        return None
    if time_part:
        clock = None
        for fmt in ("%H:%M:%S", "%H:%M", "%H"):
            try:
                clock = _dt.datetime.strptime(time_part, fmt).time()
                break
            except ValueError:
                continue
        if clock is None:
            return None
    else:
        clock = _dt.time(23, 59, 59)  # date-only -> latest snapshot that day
    return f"{day:%Y%m%d}_{clock:%H%M%S}"


def normalize_stamp(stamp: str) -> str:
    """Pad a ``YYYYMMDD`` stamp to a comparable ``YYYYMMDD_HHMMSS``.

    Two stamp widths are in use and they have to sort against each other. The inventory is
    published once per run and keeps the full ``YYYYMMDD_HHMMSS``; the daily archive written by
    :mod:`db_ops.lib.report_archive` keeps only ``YYYYMMDD``, because a date-only query can
    address just one snapshot per day anyway.

    A day-only stamp reads as the **end** of its day, matching what that archive holds — the last
    build of the day — and matching ``parse_date_param``'s reading of a bare ``?date=``.
    """
    text = str(stamp or "")
    if len(text) == 8 and text.isdigit():
        return f"{text}_235959"
    return text


def _leading_stamp(file_name: str, suffix: str) -> str:
    """The stamp ``file_name`` carries in front of ``_<suffix>``, normalized for comparison."""
    if suffix and file_name.endswith(f"_{suffix}"):
        return normalize_stamp(file_name[: -(len(suffix) + 1)])
    return normalize_stamp(file_name[:_STAMP_LEN])


def snapshot_at_or_before(root: Path, pattern: str, target_stamp: str,
                          *, suffix: str = "") -> Path | None:
    """Newest file matching ``pattern`` whose leading stamp is ``<= target_stamp``.

    ``suffix`` is the stable file name the stamped copies are named after (``server-metrics.html``
    → ``20260801_server-metrics.html``); without it the stamp is read as a fixed-width prefix, which
    is what the inventory's ``<stamp>_database-inventory-report.html`` needs.
    """
    target = normalize_stamp(target_stamp)
    best: tuple[str, Path] | None = None
    for p in root.glob(pattern):
        if not p.is_file() or p.is_symlink():
            continue
        stamp = _leading_stamp(p.name, suffix)
        if stamp <= target and (best is None or stamp > best[0]):
            best = (stamp, p)
    return best[1] if best else None


def make_handler(*, directory: str, mount: str, latest: str, latest_glob: str, root: Path,
                 logger: Any | None = None, console: Any = None,
                 strip_mount: bool = False) -> type:
    """Build a request handler that serves a dated snapshot for any ``?date=`` request.

    A ``?date=`` (or ``?at=``) query is answered with the newest snapshot at or before that
    moment; without one, the newest build is served unchanged. The served snapshot's filename
    comes back in the ``X-Report-Snapshot`` header, so a caller can tell which build it got.

    Two naming schemes are resolved, because the reports are published two different ways:

    * the **latest link** (``database-inventory.html``) is a symlink onto a per-run stamped file
      whose name differs from the link's (``<stamp>_database-inventory-report.html``), so it is
      matched by ``latest_glob``;
    * **everything else** keeps a stable name and is archived once a day under
      ``YYYYMMDD_<name>`` by :mod:`db_ops.lib.report_archive`, so the snapshot for
      ``server-metrics.html`` or ``index-usage_<slug>.html`` (or a series ``.json`` the page
      fetches) is simply that name with a day stamp in front.

    The latest link is a moving target: it is re-pointed at the newest report on every
    request for it (not only by the background loop, which can lag a request by up to its
    interval), and served with ``Cache-Control: no-cache`` so a browser revalidates instead
    of showing the snapshot it cached under the same URL.

    ``console`` is the optional :class:`db_ops.webhost.app.WebApp`. When one is given, requests
    under its own prefix are answered by it and everything else falls through to the static
    behaviour above, unchanged. The split is a URL prefix rather than a second port on purpose:
    one listener, one firewall rule, and the reports the console links to are same-origin.
    """

    class _ReportHandler(SimpleHTTPRequestHandler):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, directory=directory, **kw)

        def translate_path(self, path: str) -> str:  # noqa: ANN001 - matches stdlib signature
            """Map ``/<mount>/x`` onto ``x`` when the report directory is served directly.

            With the mount symlink in place the prefix is a real directory and the stdlib maps it.
            Without it — Windows without the privilege — the server is pointed at the report
            directory itself and the prefix is removed here instead, so the same URL answers on
            both. `send_head` rewrites `self.path` to `/<mount>/<snapshot>` for a `?date=` request,
            which passes through this the same way.
            """
            if strip_mount:
                prefix = f"/{mount}"
                if path == prefix:
                    path = "/"
                elif path.startswith(f"{prefix}/") or path.startswith(f"{prefix}?"):
                    path = path[len(prefix):] or "/"
            return super().translate_path(path)

        # -- console ------------------------------------------------------ #
        def _console_path(self) -> str:
            return unquote(urlsplit(self.path).path)

        def _serve_console(self) -> bool:
            """Answer this request from the console, or return False to fall through."""
            if console is None or not console.owns(self._console_path()):
                return False
            from db_ops.webhost.app import Request

            split = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            # Bounded read: an unbounded one lets a single request pin the process's memory, and
            # nothing this console accepts is anywhere near a megabyte.
            body = self.rfile.read(min(length, 1_048_576)) if length > 0 else b""
            request = Request(
                method=self.command,
                path=unquote(split.path),
                query=parse_qs(split.query) if split.query else {},
                headers={key.lower(): value for key, value in self.headers.items()},
                body=body,
                client_ip=self.client_address[0] if self.client_address else "",
            )
            response = console.handle(request)
            payload = response.body or b""
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(payload)))
            # The console is per-user and session-bound; a cached page is another user's page.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD" and payload:
                self.wfile.write(payload)
            return True

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if not self._serve_console():
                super().do_GET()

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
            if not self._serve_console():
                super().do_HEAD()

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            # The static server has no POST at all, so anything not the console is a 405 rather
            # than the 501 SimpleHTTPRequestHandler would give for an unimplemented verb.
            if not self._serve_console():
                self.send_error(405, "Method Not Allowed")

        def send_head(self):  # noqa: ANN201 - matches stdlib signature
            self._served_snapshot: str | None = None
            self._is_latest_link = False
            split = urlsplit(self.path)
            name = Path(unquote(split.path)).name
            query = parse_qs(split.query) if split.query else {}
            values = query.get("date") or query.get("at")
            if name == latest:
                self._is_latest_link = True
                refresh_latest(root, latest, latest_glob, logger=logger)
            target = parse_date_param(values[0]) if values else None
            if target is not None:
                if self._is_latest_link:
                    match = snapshot_at_or_before(root, latest_glob, target)
                else:
                    # The daily archive of a stable-named file. Nothing matches for a name that
                    # has never been archived (or a date before the archive starts), and then the
                    # request falls through to the live file — the newest build is a better answer
                    # than a 404, and the page still says which snapshot it is.
                    match = snapshot_at_or_before(root, f"*_{name}", target, suffix=name)
                if match is not None:
                    self._served_snapshot = match.name
                    self.path = f"/{mount}/{match.name}"
            return super().send_head()

        def end_headers(self) -> None:
            if getattr(self, "_served_snapshot", None):
                self.send_header("X-Report-Snapshot", self._served_snapshot)
            if getattr(self, "_is_latest_link", False):
                self.send_header("Cache-Control", "no-cache, must-revalidate")
            super().end_headers()

    return _ReportHandler


def refresh_latest(root: Path, link_name: str, pattern: str, *, logger: Any | None = None) -> Path | None:
    """Point ``root/link_name`` at the newest file matching ``pattern``.

    A relative symlink when the platform allows one, and **a copy when it does not**. The fixed
    URL is what the reports, the SLA page and `/spbot_self_status` all hand out; on Windows the
    symlink needs a privilege an ordinary account lacks, and returning ``None`` there left that
    URL 404 on every node. A copy costs one duplicated file per build and the link works.

    Idempotent either way: the symlink is rewritten only when its target changed, and the copy
    only when the newest report is not already the one sitting there.
    """
    newest = _newest_match(root, pattern)
    if newest is None:
        return None
    link = root / link_name
    if _already_current(link, newest):
        return newest
    try:
        # Only now, with a replacement due: the old code unlinked first and then tried the
        # symlink, so on a platform that refuses one the attempt *destroyed* whatever was there.
        # A hand-placed copy of the newest report survived exactly until the next request for it.
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(newest.name)  # relative target keeps it valid across host/container mounts
        return newest
    except OSError as exc:  # noqa: BLE001 - Windows w/o privilege, read-only fs, etc.
        return _copy_as_latest(link, newest, symlink_error=exc, logger=logger)


def _already_current(link: Path, newest: Path) -> bool:
    """Is ``link`` already the newest report — as a symlink onto it, or as a copy of it?

    Both forms have to be recognised here rather than inside the branch that writes them. This
    function runs on **every request** for the latest URL, and answering "no" for a copy that is
    already correct means re-copying a 1.5 MB report per page view.
    """
    try:
        if link.is_symlink():
            return Path(link.readlink()).name == newest.name
        if link.is_file():
            current, source = link.stat(), newest.stat()
            return current.st_size == source.st_size and current.st_mtime == source.st_mtime
    except OSError:
        return False
    return False


def _copy_as_latest(link: Path, newest: Path, *, symlink_error: OSError,
                    logger: Any | None = None) -> Path | None:
    """Copy ``newest`` over ``link`` — the fallback when a symlink cannot be created.

    ``copy2`` carries the source's mtime across, which is what lets :func:`_already_current`
    recognise the copy next time by comparing timestamps rather than reading 1.5 MB back.
    """
    import shutil

    try:
        shutil.copy2(newest, link)
        return newest
    except OSError as copy_error:  # noqa: BLE001 - report both, the first is the reason for this
        if logger is not None:
            from db_ops.logging_ops import log_event

            log_event(logger, level="warning",
                      message=(f"webhost: could not publish {link} as the latest report "
                               f"({newest.name}): symlink failed with {symlink_error}, "
                               f"and the copy fallback failed with {copy_error}"))
        return None


def build_webroot(root: Path, mount: str, webroot: Path, *, logger: Any | None = None) -> Path:
    """Ensure ``webroot/<mount>`` is a symlink to ``root`` so URLs are served under ``/<mount>/``.

    Returns the web root directory to hand to the HTTP handler.
    """
    webroot.mkdir(parents=True, exist_ok=True)
    target = webroot / mount
    try:
        if target.is_symlink():
            if Path(target.readlink()).resolve() == root.resolve():
                return webroot
            target.unlink()
        elif target.exists():
            # A real dir/file is in the way — leave it, but warn (URL may not map as expected).
            if logger is not None:
                from db_ops.logging_ops import log_event

                log_event(logger, level="warning",
                          message=f"webhost: {target} exists and is not a symlink; leaving as-is.")
            return webroot
        target.symlink_to(root.resolve(), target_is_directory=True)
    except OSError as exc:  # noqa: BLE001
        if logger is not None:
            from db_ops.logging_ops import log_event

            log_event(logger, level="warning",
                      message=f"webhost: could not create mount symlink {target} -> {root}: {exc}")
    return webroot


def _refresh_loop(root: Path, link_name: str, pattern: str, interval: int, *, logger: Any | None) -> None:
    while True:
        time.sleep(max(1, interval))
        try:
            refresh_latest(root, link_name, pattern, logger=logger)
        except Exception:  # noqa: BLE001 - never let the refresher kill the server
            pass


def serve(
    *,
    root: str | Path,
    mount: str,
    port: int,
    bind: str = "0.0.0.0",
    webroot: str | Path,
    latest: str,
    latest_glob: str,
    refresh_seconds: int = 60,
    logger: Any | None = None,
    console: Any = None,
) -> int:
    """Serve ``root`` under ``http://<bind>:<port>/<mount>/`` and keep ``latest`` fresh.

    ``console`` is the optional web console (:class:`db_ops.webhost.app.WebApp`), served from its
    own URL prefix on this same listener.
    """
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    webroot_path = Path(webroot).resolve()

    build_webroot(root_path, mount, webroot_path, logger=logger)
    # Did the mount symlink actually appear? When it did not, serve the report directory itself
    # and take the prefix off in the handler. Without this the server sat on an empty webroot and
    # answered 404 for every report on the node - which is what it did on Windows.
    mount_entry = webroot_path / mount
    strip_mount = not (mount_entry.is_symlink() or mount_entry.exists())
    served_directory = root_path if strip_mount else webroot_path
    if strip_mount and logger is not None:
        from db_ops.logging_ops import log_event

        log_event(logger, level="logging",
                  message=(f"webhost: no mount symlink at {mount_entry}; serving {root_path} "
                           f"directly and resolving the /{mount}/ prefix in process"))
    refresh_latest(root_path, latest, latest_glob, logger=logger)

    refresher = threading.Thread(
        target=_refresh_loop,
        args=(root_path, latest, latest_glob, refresh_seconds),
        kwargs={"logger": logger},
        daemon=True,
        name="webhost-refresh",
    )
    refresher.start()

    handler = make_handler(
        directory=str(served_directory),
        strip_mount=strip_mount,
        mount=mount,
        latest=latest,
        latest_glob=latest_glob,
        root=root_path,
        logger=logger,
        console=console,
    )
    httpd = ThreadingHTTPServer((bind, port), handler)
    message = (f"webhost serving {root_path} at http://{bind}:{port}/{mount}/ "
               f"(latest='{latest}' <- '{latest_glob}', refresh={refresh_seconds}s)")
    if console is not None:
        message += f"; console at http://{bind}:{port}{console.prefix}/"
    if logger is not None:
        from db_ops.logging_ops import log_event

        log_event(logger, level="logging", message=message)
    print(message, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
