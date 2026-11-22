"""``key-request`` / ``key-renew`` / ``key-status`` - this node's dbabrain key (1.0.0, plan K3).

A key comes from dbabrain.dev through the device sign-in ``dbabrain mcp connect`` already uses: a
code and a link are printed, the person opens the link, signs in with their email and approves, and
dbabrain.dev hands this process a signed key once (``connector/worker/src/key-issue.ts``). It goes
into the node's encrypted secret store as ``DBABRAIN_KEY`` (:data:`lib.dbabrain_key.KEY_REF`) -
and, on a master, into the plaintext source the store is made from too, or the next deploy's
``encrypt-secret`` would drop it.

The order is the point of this module:

1. **the store opens first** - a key handed over and then not storable is lost, and it has already
   replaced the account's earlier key on dbabrain.dev;
2. **the key is read before it is kept** - one this release cannot read (a signing key it does not
   know) is refused with what to do, never stored to fail every gate later;
3. **stored, read back, compared** - nothing is said done until the store gives it back.

``key-status`` reads the key offline (signature, term, grace) and, when the node holds one, asks
dbabrain.dev about it (``GET /key/status`` - the daily check of D5 runs the same thing). The online
answer is kept in ``runtime/dbabrain_key_status.json`` beside the data folder, for the gate; a check
that fails is said, and is not a revocation.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from dbabrain.lib import dbabrain_key as dk
from dbabrain.lib import errors
from dbabrain.lib import secret_text as _secret_text

DEFAULT_SERVER = dk.DEFAULT_SERVER
#: How long the person has to approve - dbabrain.dev's own expiry of a pending sign-in.
LINK_SECONDS = 600


def _http(method: str, url: str, *, body: dict[str, Any] | None = None,
          bearer: str = "", timeout: float = 30) -> tuple[int, dict[str, Any]]:
    headers = {"content-type": "application/json", "user-agent": "dbabrain-node"}
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


#: Where the last online answer is kept: the tool root's ``runtime/``, never ``data/`` (deployed).
status_file = dk.status_file


def _passphrase() -> str:
    key = os.environ.get(_secret_text.SECRET_KEY_ENV_VAR) or ""
    if not key:
        raise errors.NotConfigured(
            f"the secret store's passphrase is needed ({_secret_text.SECRET_KEY_ENV_VAR}): the key is kept "
            "in the encrypted store, never in a file in clear")
    return key


def _held(data_dir: Path, key: str) -> str:
    return str(_secret_text.load_secret_text(data_dir, key=key).get(dk.KEY_REF) or "")


def request_key(
    data_dir: str | Path,
    *,
    server: str = DEFAULT_SERVER,
    node: str = "",
    plaintext_store: str | Path | None = None,
    http: Callable[..., tuple[int, dict[str, Any]]] = _http,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = lambda text: print(text, file=sys.stderr, flush=True),
    public_keys: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The device sign-in, then the key stored - see the module's order."""
    data_dir = Path(data_dir)
    key = _passphrase()
    earlier = _held(data_dir, key)  # 1. the store opens (a wrong passphrase raises here)
    server = server.rstrip("/")
    status, start = http("POST", f"{server}/node/link/start",
                         body={"purpose": "key", "node": node or socket.gethostname()})
    if status != 200 or not start.get("device_code"):
        raise errors.Unreachable(f"{server} did not start a sign-in ({status}): {start.get('error') or start}")
    say(f"\nOpen {start['verification_uri']}\nsign in with your email, and approve the code  {start['user_code']}\n")
    deadline = time.monotonic() + int(start.get("expires_in") or LINK_SECONDS)
    interval = max(2, int(start.get("interval") or 5))
    token = ""
    while time.monotonic() < deadline:
        sleep(interval)
        status, answer = http("POST", f"{server}/node/link/poll", body={"device_code": start["device_code"]})
        if status == 200 and answer.get("dbabrain_key"):
            token = str(answer["dbabrain_key"])
            break
        if status == 200 and answer.get("node_token"):
            raise errors.OperationFailed(f"{server} answered a node link, not a key - it does not issue keys yet")
        if status not in (202, 428):
            raise errors.Refused(f"the sign-in was not approved ({status}): {answer.get('error') or answer}")
    if not token:
        raise errors.Refused("the sign-in expired before it was approved - run key-request again")
    try:
        parsed = dk.parse_key(token, public_keys=public_keys)  # 2. read before it is kept
    except dk.KeyInvalid as exc:
        raise errors.Refused(
            f"dbabrain.dev gave a key this release cannot read ({exc}) - upgrade dbabrain, then run "
            "key-request again; nothing was stored") from exc
    if plaintext_store is not None and Path(plaintext_store).is_file():
        _secret_text.set_secret_everywhere(data_dir, dk.KEY_REF, token, key=key, plaintext_store=plaintext_store)
    else:
        _secret_text.set_secret_text(data_dir, dk.KEY_REF, token, key=key, overwrite=True)
    if _held(data_dir, key) != token:  # 3. read back
        raise errors.OperationFailed(f"the key was written but the store does not give it back as {dk.KEY_REF}")
    standing = dk.standing(token, now=now or datetime.now(timezone.utc), public_keys=public_keys)
    return {
        "ref": dk.KEY_REF, "store": str(data_dir / _secret_text.ENCRYPTED_SECRET_TEXT_FILENAME),
        "replaced": bool(earlier), "key": parsed.describe(), "state": standing.state,
    }


def key_status(
    data_dir: str | Path,
    *,
    server: str = DEFAULT_SERVER,
    online: bool = True,
    http: Callable[..., tuple[int, dict[str, Any]]] = _http,
    public_keys: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The key's standing offline, the online answer when one came, and what each feature may do."""
    data_dir = Path(data_dir)
    moment = now or datetime.now(timezone.utc)
    token = _held(data_dir, _passphrase())
    kept = status_file(data_dir)
    last = dk.read_status(data_dir)
    check: dict[str, Any] = {"asked": False}
    if token and online:
        check["asked"] = True
        try:
            status, answer = http("GET", f"{server.rstrip('/')}/key/status", bearer=token, timeout=15)
        except (OSError, ValueError) as exc:
            check["error"] = f"dbabrain.dev was not reached ({exc}) - the offline answer stands"
        else:
            if status == 200 and answer.get("state"):
                last = {"kid": answer.get("kid"), "state": answer.get("state"),
                        "reason": answer.get("reason") or "", "checked_at": dk.iso_utc(moment)}
                _write_status(kept, last)
            else:
                check["error"] = f"dbabrain.dev answered {status}: {answer.get('error') or answer} - the offline answer stands"
    standing = dk.standing(token, now=moment, online=last, public_keys=public_keys)
    return {
        "standing": standing.describe(),
        "online": {**check, "last": last, "file": str(kept)},
        "features": {name: dk.may_run(standing, name).allowed for name in dk.FEATURES},
        "how_to_get": dk.HOW_TO_GET if standing.state in ("none", "invalid") else "",
    }


def _write_status(path: Path, status: dict[str, Any]) -> None:
    from dbabrain.lib.json_io import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(status, indent=2) + "\n")
