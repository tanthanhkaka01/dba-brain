r"""Point this node at the address its published pages are reachable on.

The third of a set. ``store_config.json`` got ``db use-store`` in v0.14.0 and ``bot_telegram.json``
got ``telegram use-bot`` on 2026-09-14, both for the same reason: a catalogued file travels inside
a config bundle, so ``import-data`` faithfully hands a machine that has never run the *source's*
identity and nothing says so. ``report_base_url`` in ``data/reports_config.json`` is the same shape
and was still a hand-edit.

**What it is for.** Producers that build a page link fall back to a relative href when it is empty,
which is right; producers that need an absolute URL — the Telegram messages — leave the link out.
So an unconfigured base URL is a state, not a fault. It becomes a fault when it is *set to somebody
else's address*: on 2026-09-14 a node reported ``published links point at
http://<the worker>:8080/report_dba/ - not this node``, correct and unactionable, because there was
no command to change it.

**Why not just derive it every time.** :func:`db_ops.common.data_sources.report_base_url` already
falls back to a value worked out from the estate's declared worker host, which is right for the
estate's own pages and wrong for a node being proved: that node serves its own copies, and every
link it publishes would send the reader to the machine it was cloned from. ``--this-node`` writes
the address this node actually answers on; a bare URL writes what you say; ``--clear`` goes back to
the derived answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from db_ops.lib.json_io import atomic_write_text

#: The file the published address lives in, alongside the rest of the reports configuration.
REPORTS_CONFIG_FILENAME = "reports_config.json"

#: The key inside it. Named here so the command and the reader agree on one spelling.
BASE_URL_KEY = "report_base_url"


class UseBaseUrlError(RuntimeError):
    """The node could not be pointed at that address. Nothing was written."""


USAGE = """usage: python -m db_ops.reports.cli use-base-url [<url> | --this-node | --clear] [--dry-run]

Point this node at the address its published report pages are reachable on - the mirror of
`db use-store` and `telegram use-bot`, for data/reports_config.json.

  <url>        the address to publish, e.g. http://192.0.2.10:8080/report_dba/
  --this-node  work it out from this node's own ip and the web host command's port and mount
  --clear      remove it, and fall back to the address derived from the estate's worker host
  --dry-run    print what would be written; write nothing

An absolute URL is required with a scheme: `192.0.2.10:8080/report_dba/` is read by a browser as a
relative path, and the link 404s from every page that carries it.
"""


def _normalise(url: str) -> str:
    """One trailing slash, and a refusal for anything a browser would not follow."""
    text = str(url or "").strip()
    if not text:
        raise UseBaseUrlError("a URL is required, or --this-node, or --clear.")
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"}:
        raise UseBaseUrlError(
            f"{text!r} has no http:// or https:// scheme. Without one a browser reads it as a "
            "relative path and every published link 404s - which is why this is refused here "
            "rather than found later in somebody's inbox.")
    if not parts.netloc:
        raise UseBaseUrlError(f"{text!r} names no host.")
    return text.rstrip("/") + "/"


def _this_node(*, host: str, runtime: str, port: int, mount: str) -> str:
    """The address this node answers on, or a refusal saying why it cannot know.

    In a container the address a socket reports is on Docker's private pool and nobody outside
    reaches it; offering it as a published link is worse than offering none. v0.4.0 shipped
    one of those as a clickable link for exactly this reason.
    """
    from db_ops.lib import webhost_endpoints

    if runtime in getattr(webhost_endpoints, "CONTAINER_RUNTIMES", frozenset()):
        raise UseBaseUrlError(
            f"this node runs in a {runtime} runtime, where the address it can see is the one "
            "inside the container and not the one anyone reaches it on. Give the published URL "
            "instead of --this-node.")
    if not host:
        raise UseBaseUrlError(
            "this node cannot resolve its own address, so --this-node has nothing to write. "
            "Give the URL instead.")
    return _normalise(webhost_endpoints.base_url(host=host, port=port, mount=mount))


def use_base_url(url: str = "", *, data_dir: str | Path, this_node: bool = False,
                 clear: bool = False, dry_run: bool = False, host: str = "",
                 runtime: str = "host", port: int = 8080,
                 mount: str = "report_dba") -> dict[str, Any]:
    """Write ``report_base_url`` into ``reports_config.json``, preserving everything else."""
    given = [bool(str(url or "").strip()), this_node, clear]
    if sum(given) != 1:
        raise UseBaseUrlError(
            "give exactly one of a URL, --this-node or --clear. Two answers to 'where are the "
            "pages' is how the wrong one gets published.")

    root = Path(data_dir)
    path = root / REPORTS_CONFIG_FILENAME
    document: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_bytes().decode("utf-8-sig"))
        except ValueError as exc:
            raise UseBaseUrlError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise UseBaseUrlError(f"{path} is not a JSON object.")
        document = loaded

    before = str(document.get(BASE_URL_KEY) or "").strip()
    if clear:
        after = ""
    elif this_node:
        after = _this_node(host=host, runtime=runtime, port=port, mount=mount)
    else:
        after = _normalise(url)

    document[BASE_URL_KEY] = after
    if not dry_run:
        atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=4) + "\n")

    # Reported rather than assumed, for the same reason `use-store` prints the resolved connection:
    # the mistake being prevented is believing the node is on the other address.
    from db_ops.common.data_sources import derived_report_base_url

    try:
        derived = derived_report_base_url(root)
    except Exception:  # noqa: BLE001 - an unreadable config costs the note, not the write.
        derived = ""

    return {
        "was": before,
        "now": after,
        "effective": after or derived,
        "derived": derived,
        "source": "configured" if after else ("derived" if derived else "relative links only"),
        "file": str(path),
        "written": not dry_run,
    }
