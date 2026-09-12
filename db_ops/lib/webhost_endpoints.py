"""Where this installation's own web pages are, as URLs.

Pure: every function here is a function of its arguments. Reading ``app_commands.json`` and
``webhost_config.json`` is an operation and lives in ``db_ops.common.data_sources``.

The port and the two mounts are **deployment facts**, not code facts - the webhost serve command
carries them and an operator can change either. They were being retyped from memory into runbooks
and into ``report_base_url``, which is how a node that moved kept publishing links to the host it
had moved off.
"""

from __future__ import annotations

import shlex

#: Matches ``db_ops.webhost.cli serve`` - keep in step with the argparse defaults there, which are
#: what a command_text that omits the flag actually gets.
DEFAULT_PORT = 8080
DEFAULT_REPORTS_MOUNT = "report_dba"
DEFAULT_CONSOLE_MOUNT = "db_ops"

#: The pages the web host publishes under stable names (``--latest`` in db_ops.webhost.cli), as
#: opposed to a run's stamped file. A per-server page is not here: its file name carries a slug,
#: and naming one estate's server in shipped code is how an identifier leaks into a release.
STABLE_PAGES = {
    "inventory": "database-inventory.html",
    "server_metrics": "server-metrics.html",
    "sla": "sla.html",
}

#: Pages that exist once per server. The slug is the caller's to fill in - a real server_id in
#: shipped code is how an identifier leaks into a release, and `check-identifiers` refuses it.
PER_SERVER_PAGES = {
    "index_usage": "index-usage_{server_id}.html",
}


def parse_serve_options(command_text: str) -> dict[str, object]:
    """The ``--port`` and ``--mount`` a webhost serve command_text asks for.

    Falls back to the CLI's own defaults for whatever the command does not state, so a shortened
    command and the full one describe the same server. A command that is not a webhost serve at
    all returns ``{}`` - the caller decides whether that is an error, because on a node with no
    webhost entry it simply means the pages are not served here.
    """
    text = str(command_text or "")
    if "webhost" not in text or " serve" not in text:
        return {}
    try:
        # posix=False keeps Windows backslashes in --root intact; they are a path, not an escape.
        parts = shlex.split(text, posix=False)
    except ValueError:
        return {}

    options: dict[str, object] = {"port": DEFAULT_PORT, "mount": DEFAULT_REPORTS_MOUNT}
    for index, part in enumerate(parts):
        if index + 1 >= len(parts):
            break
        value = parts[index + 1].strip("\"'")
        if part == "--port":
            try:
                options["port"] = int(value)
            except ValueError:
                # A malformed port is worth ignoring rather than raising: this function feeds a
                # status report, and refusing to describe the node because one flag is mistyped
                # loses the other nine facts too.
                continue
        elif part == "--mount":
            options["mount"] = value.strip("/")
    return options


def base_url(*, host: str, port: int, mount: str, scheme: str = "http") -> str:
    """One mount's base URL, always with a trailing slash so a page name can be appended."""
    host_text = str(host or "").strip()
    if not host_text:
        return ""
    mount_text = str(mount or "").strip("/")
    port_text = "" if port in (80, 443) else f":{int(port)}"
    tail = f"{mount_text}/" if mount_text else ""
    return f"{scheme}://{host_text}{port_text}/{tail}"


def endpoints(*, host: str, port: int = DEFAULT_PORT,
              reports_mount: str = DEFAULT_REPORTS_MOUNT,
              console_mount: str = DEFAULT_CONSOLE_MOUNT,
              scheme: str = "http") -> dict[str, str]:
    """Every URL this node serves, keyed by what a reader would call it.

    An empty ``host`` yields an empty mapping rather than URLs with a hole in them: "not
    reachable" is a state worth reporting honestly, and ``http://:8080/`` is not an address.
    """
    reports = base_url(host=host, port=port, mount=reports_mount, scheme=scheme)
    console = base_url(host=host, port=port, mount=console_mount, scheme=scheme)
    if not reports:
        return {}
    urls = {"console": console, "reports": reports}
    urls.update({name: reports + page for name, page in STABLE_PAGES.items()})
    # Left as a template on purpose: the reader substitutes the server_id, exactly as the
    # server-metrics picker does with its query string.
    urls.update({name: reports + page for name, page in PER_SERVER_PAGES.items()})
    return urls


#: What to print where a host name would go when this node cannot know its own published address.
#: A visible blank the reader fills in beats a confident wrong answer - see `CONTAINER_RUNTIMES`.
PLACEHOLDER_HOST = "<host>"

#: Runtimes where the address a socket reports is **not** the address anyone reaches the node at.
#: Inside a container `gethostname`/`getsockname` yield the bridge address on Docker's private pool
#: (`172.30.240.2` on this estate), which routes from nowhere else, and the published port is a
#: mapping the container cannot see. Measured 2026-09-10, when `self-status` on the worker offered
#: `http://172.30.240.2:8080/db_ops/` as a link to click.
CONTAINER_RUNTIMES: frozenset[str] = frozenset({"docker", "containerd", "kubernetes", "lxc"})


def parse_base_url(url: str) -> dict[str, object]:
    """Split a published base URL back into scheme, host, port and mount.

    Used when the node cannot answer for itself: whoever configured `report_base_url` was stating
    where these pages are actually reachable, which is a better answer than a bridge address.
    """
    text = str(url or "").strip()
    if not text:
        return {}
    scheme, _, rest = text.partition("://")
    if not rest:
        scheme, rest = "http", text
    authority, _, mount = rest.partition("/")
    host, _, port_text = authority.partition(":")
    try:
        port = int(port_text) if port_text else (443 if scheme == "https" else 80)
    except ValueError:
        return {}
    # A host has no spaces and no empty labels. Without this, `parse_base_url("not a url at all")`
    # answered `host="not a url at all"` and the caller built a link from it - a malformed setting
    # turning into a confident wrong address, which is the failure this whole path exists to avoid.
    if not host or any(character.isspace() for character in host) or ".." in host:
        return {}
    if not all(part for part in host.split(".")):
        return {}
    return {"scheme": scheme, "host": host, "port": port, "mount": mount.strip("/")}


def same_site(left: str, right: str) -> bool:
    """Whether two base URLs name the same published location, ignoring a trailing slash."""
    return str(left or "").rstrip("/") == str(right or "").rstrip("/")
