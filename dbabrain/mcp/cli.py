"""``dbabrain mcp`` - dbabrain as an MCP server for Claude Desktop and Claude Code (0.38.0, way 1).

    dbabrain mcp serve  [--home DIR]          speak MCP over stdio (what Claude starts)
    dbabrain mcp tools  [--home DIR] [--json] the tools this node offers, and their hints
    dbabrain mcp connect [--home DIR] [--relink]
                                              this machine as YOUR node of dbabrain.dev: claude.ai
                                              on the web and the phone reach this dbabrain
    dbabrain mcp call   TOOL REQUEST [--home DIR]
                                              one tool without Claude: REQUEST is the tool's arguments,
                                              inline JSON, @file or - ; for a command tool
                                              {"request": {...}}

``--home`` is the tool root (``DBABRAIN_HOME``): Claude starts the server from a folder of its own,
so the config names the root - in Claude's config as ``"env": {"DBABRAIN_HOME": "D:/dbabrain"}``.
The secret key is NEVER in Claude's config: the server reads ``DBABRAIN_SECRET_KEY`` from the user's
own environment, as every dbabrain command does. ``serve`` needs the ``mcp`` extra
(``pip install "dba-brain[mcp]"``) and says so without it. See ``docs/16_mcp_app.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

EXTRA = "pip install \"dba-brain[mcp]\""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbabrain mcp", description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("serve", "speak MCP over stdio - what Claude Desktop / Claude Code start"),
                       ("tools", "the tools this node offers, and the hints the client shows"),
                       ("connect", "link this machine to your dbabrain.dev account and keep it connected"),
                       ("call", "run one tool without Claude")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--home", help="the tool root (DBABRAIN_HOME); default: the usual resolution")
        if name == "tools":
            command.add_argument("--json", action="store_true", help="the list as JSON")
        if name == "serve":
            command.add_argument("--http", action="store_true",
                                 help="Streamable HTTP on /mcp instead of stdio (way 2's origin); the bearer "
                                      "token is read from DBABRAIN_MCP_HTTP_TOKEN")
            command.add_argument("--bind", default="127.0.0.1", help="the address to listen on (--http)")
            command.add_argument("--port", type=int, default=8000, help="the port (--http)")
            command.add_argument("--allowed-host", action="append", default=[],
                                 help="a Host header the tunnel delivers, e.g. mcp-origin.dbabrain.dev (--http)")
        if name == "connect":
            command.add_argument("--server", default="https://mcp.dbabrain.dev", help="the connector")
            command.add_argument("--relink", action="store_true", help="link again (a new token)")
        if name == "call":
            command.add_argument("tool")
            command.add_argument("arguments", help="the tool's arguments: JSON, @file or -")
    return parser


def _node(home: str | None) -> Any:
    if home:
        # Set before anything resolves the root: the commands a tool runs are child processes, and
        # they find the tool root by this variable too.
        os.environ["DBABRAIN_HOME"] = str(Path(home).resolve())
    from dbabrain.lib.paths import resolve_data_dir, resolve_tool_root
    from dbabrain.mcp.server import load_node

    root = resolve_tool_root(home=os.environ.get("DBABRAIN_HOME") or None)
    os.environ.setdefault("DBABRAIN_HOME", str(root))
    return load_node(tool_root=root, data_dir=resolve_data_dir(tool_root=root))


def _arguments(text: str) -> dict[str, Any]:
    if text == "-":
        raw = sys.stdin.read()
    elif text.startswith("@"):
        raw = Path(text[1:]).read_text(encoding="utf-8-sig")
    else:
        raw = text
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise SystemExit("the arguments are one JSON object.")
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    node = _node(args.home)
    if args.command == "tools":
        from dbabrain.mcp.server import tool_list

        tools = tool_list(node)
        if args.json:
            print(json.dumps(tools, ensure_ascii=False, indent=1))
        else:
            for tool in tools:
                mark = "DESTRUCTIVE" if tool["destructive"] else ("read-only" if tool["read_only"] else "changes")
                print(f"{tool['name']:<34} {mark:<11} {tool['description'].splitlines()[0][:90]}")
            print(f"\n{len(tools)} tool(s); switched off: {', '.join(node.settings.disabled_tools) or 'none'}"
                  + ("" if node.settings.enabled else " - the server is OFF (enabled false)"))
        return 0
    if args.command == "connect":
        try:
            from dbabrain.mcp.relay import connect_node

            connect_node(node, server=args.server.rstrip("/"), relink=args.relink)
        except ImportError as exc:
            print(f"dbabrain mcp connect needs the MCP extra: {EXTRA} ({exc})", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 0
        return 0
    if args.command == "call":
        from dbabrain.mcp.server import call_tool

        print(call_tool(node, args.tool, _arguments(args.arguments)))
        return 0
    try:
        from dbabrain.mcp.server import build_server

        server = build_server(node)
    except ImportError as exc:
        print(f"dbabrain mcp serve needs the MCP SDK: {EXTRA} ({exc})", file=sys.stderr)
        return 2
    if not node.settings.enabled:
        print("dbabrain mcp: enabled is false in data/mcp_tools.json - no tool is offered.", file=sys.stderr)
    if args.http:
        import uvicorn

        from dbabrain.mcp.server import HTTP_TOKEN_ENV, http_app

        app = http_app(node, token=os.environ.get(HTTP_TOKEN_ENV, ""), allowed_hosts=args.allowed_host)
        uvicorn.run(app, host=args.bind, port=args.port, log_level="warning")
        return 0
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
