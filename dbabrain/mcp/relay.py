"""``dbabrain mcp connect`` - this machine's dbabrain as a node of dbabrain.dev (0.38.0, way 2).

The operator, 2026-11-11 (translated): *every user connects their own databases*. claude.ai reaches
``https://mcp.dbabrain.dev/mcp``; the Worker there signs the person in and hands each of their MCP
requests to **their** node - this process, on their machine - over one WebSocket this process opened
outbound. Nothing is opened on the machine (no port, no tunnel program), and one person's requests
reach only their own node: the Worker keeps one relay per account.

* **Linking** is a device sign-in, the way a TV signs in: ``connect`` asks the Worker for a code,
  prints a link, the person opens it, signs in with their email and approves; the Worker answers
  this process with a node token, kept in the node's encrypted secret store
  (``DBABRAIN_MCP_NODE_TOKEN``) - never a plain file. The Worker keeps only its hash.
* **Each request** arrives as one frame ``{id, headers, body}`` and is answered by the SDK's own MCP
  server, run in this process (the app ``serve --http`` serves, called directly) - so the protocol
  is the SDK's, not a copy of it. Every answer passes ``server.call_tool``: redacted, capped, logged
  with the signed-in email as ``asked_by``.
* **The connection** is kept: a dropped one is opened again after a back-off (1 s doubling to 60 s);
  a token the Worker refuses ends the process with the sentence that says so.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from typing import Any

from dbabrain.lib import errors

DEFAULT_SERVER = "https://mcp.dbabrain.dev"
NODE_TOKEN_REF = "DBABRAIN_MCP_NODE_TOKEN"
#: The frames a node takes at once - more wait their turn, so one person cannot flood their own node.
MAX_IN_FLIGHT = 8


class RelayError(errors.OperationFailed):
    """The Worker refused the node, or the link was not approved."""


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "user-agent": "dbabrain-node"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def link(server: str, *, sleep=time.sleep, post=_post) -> dict[str, Any]:
    """The device sign-in: a code, a link the person opens and approves, then the node token."""
    status, start = post(f"{server}/node/link/start", {})
    if status != 200 or not start.get("device_code"):
        raise RelayError(f"{server} did not start a link ({status}): {start.get('error') or start}")
    _say(f"\nOpen {start['verification_uri']}\nsign in, and approve the code  {start['user_code']}\n")
    deadline = time.monotonic() + int(start.get("expires_in") or 600)
    interval = max(2, int(start.get("interval") or 5))
    while time.monotonic() < deadline:
        sleep(interval)
        status, answer = post(f"{server}/node/link/poll", {"device_code": start["device_code"]})
        if status == 200 and answer.get("node_token"):
            return answer
        if status not in (202, 428):
            raise RelayError(f"the link was not approved ({status}): {answer.get('error') or answer}")
    raise RelayError("the link expired before it was approved - run `dbabrain mcp connect` again.")


def stored_token(node: Any) -> str:
    return str(node.secrets().get(NODE_TOKEN_REF) or "")


def store_token(node: Any, token: str) -> None:
    from dbabrain.lib.secret_text import set_secret_text

    set_secret_text(node.data_dir, NODE_TOKEN_REF, token, overwrite=True)
    node._secrets = None
    node._redactor = None


# --------------------------------------------------------------------------- #
# One frame through the SDK's own server, in this process
# --------------------------------------------------------------------------- #
class LocalMcp:
    """The MCP server ``serve --http`` serves, called directly: a frame in, a frame out."""

    def __init__(self, node: Any) -> None:
        from mcp.server.transport_security import TransportSecuritySettings

        from dbabrain.mcp.server import build_server

        self.server = build_server(node)
        # Called in this process, never over a network: the Host-header check (DNS rebinding) has
        # nothing to protect here, and on it every frame was refused 421.
        self.app = self.server.streamable_http_app(
            stateless_http=True, json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    def running(self) -> Any:
        """The context the SDK's session manager needs while frames are answered."""
        return self.server.session_manager.run()

    async def answer(self, frame: dict[str, Any]) -> dict[str, Any]:
        from dbabrain.mcp.server import ASKED_BY

        headers = {str(k).lower(): str(v) for k, v in (frame.get("headers") or {}).items()}
        headers.setdefault("content-type", "application/json")
        headers.setdefault("accept", "application/json, text/event-stream")
        body = str(frame.get("body") or "").encode()
        token = ASKED_BY.set(headers.get("x-dbabrain-user", ""))
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/mcp", "raw_path": b"/mcp", "query_string": b"", "root_path": "",
                 "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                 "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 80)}
        try:
            await self.app(scope, receive, send)
        finally:
            ASKED_BY.reset(token)
        start = next((m for m in sent if m["type"] == "http.response.start"), {"status": 500, "headers": []})
        out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return {"id": frame.get("id"), "status": int(start["status"]),
                "headers": {k.decode(): v.decode() for k, v in start.get("headers") or []
                            if k.decode().lower() in ("content-type", "mcp-session-id")},
                "body": out.decode("utf-8", "replace")}


# --------------------------------------------------------------------------- #
# The connection
# --------------------------------------------------------------------------- #
def socket_url(server: str) -> str:
    return server.replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/") + "/node/connect"


async def serve(node: Any, *, server: str, token: str, connect: Any = None, once: bool = False) -> None:
    """Answer frames until the Worker refuses the token; a dropped connection is opened again."""
    if connect is None:
        from websockets.asyncio.client import connect

    local = LocalMcp(node)
    gate = asyncio.Semaphore(MAX_IN_FLIGHT)
    backoff = 1.0
    refused: RelayError | None = None
    async with local.running():
        while refused is None:
            try:
                async with connect(socket_url(server), additional_headers={"authorization": f"Bearer {token}"},
                                   ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024) as socket:
                    _say(f"dbabrain node connected to {server} - Claude can reach this machine's dbabrain now.")
                    backoff = 1.0

                    async def handle(raw: str) -> None:
                        async with gate:
                            try:
                                frame = json.loads(raw)
                            except ValueError:
                                return
                            if frame.get("type") == "ping":
                                await socket.send(json.dumps({"type": "pong"}))
                                return
                            reply = await local.answer(frame)
                            await socket.send(json.dumps(reply))

                    tasks: set[asyncio.Task] = set()
                    async for raw in socket:
                        task = asyncio.create_task(handle(raw))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
            except Exception as exc:  # noqa: BLE001 - every way a connection ends, said once
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (401, 403):
                    # Raised once out of the SDK's context: inside it a task group wraps it in a group.
                    refused = RelayError(f"dbabrain.dev refused this node's token ({status}) - it was unlinked; "
                                         "run `dbabrain mcp connect --relink`.")
                    break
                _say(f"dbabrain node: the connection ended ({type(exc).__name__}: {str(exc)[:160]}); "
                     f"again in {int(backoff)} s")
            if once:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    if refused is not None:
        raise refused


def connect_node(node: Any, *, server: str = DEFAULT_SERVER, relink: bool = False) -> None:
    """``dbabrain mcp connect``: link this machine once, then keep its node connected."""
    token = "" if relink else stored_token(node)
    if not token:
        if node.secrets_error:
            raise RelayError(f"the secret store cannot be read ({node.secrets_error}) - the node token is kept there; "
                             "set DBABRAIN_SECRET_KEY.")
        answer = link(server)
        token = str(answer["node_token"])
        store_token(node, token)
        _say(f"linked to {answer.get('email') or 'your account'} - the token is in the encrypted secret store.")
    asyncio.run(serve(node, server=server, token=token))
