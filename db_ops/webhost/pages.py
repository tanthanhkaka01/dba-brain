"""HTML for the web console — server-rendered, no framework, no assets to fetch.

Every page is one self-contained string: the CSS is inline and there is no JavaScript bundle, no
CDN and no build step. That is a deliberate constraint rather than minimalism for its own sake —
the console runs inside the worker container on a network that may not reach the internet, and a
page that silently loses its stylesheet on a locked-down host is worse than a plain one.

Escaping is not optional here: every value on these pages comes from the store, which is now
written to by a web form. :func:`escape` is applied to each interpolation, and
the few places that emit JSON for the page do it through :func:`json_script`, which closes the
one hole ``json.dumps`` leaves in an HTML context.

The palette follows the status vocabulary the rest of db_ops already uses — the severity emoji in
``db_ops/telegram/severity.py`` and the report colours — so "red means failing" carries over from
the Telegram alerts without anyone learning a second convention.
"""

from __future__ import annotations

import html
import json
from typing import Any


def escape(value: Any) -> str:
    """HTML-escape anything on its way into a page — including into an attribute.

    ``quote=True`` is not the default and is the half that matters here: without it a value
    containing a double quote breaks out of ``value="..."`` and can add its own attributes. Every
    interpolation below goes through this, whether it lands in text or in an attribute, because
    remembering which is which per call site is exactly the decision that eventually gets wrong.
    """
    return html.escape(str(value if value is not None else ""), quote=True)


_STYLE = """
:root {
  --bg: #0f1115; --panel: #171a21; --panel-2: #1e222b; --line: #2a2f3a;
  --text: #e6e8ee; --muted: #9aa3b2; --accent: #4f8cff;
  --ok: #3fb950; --warn: #d29922; --bad: #f85149; --idle: #6e7681;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.top { display: flex; align-items: center; justify-content: space-between; gap: 16px;
             padding: 14px 24px; background: var(--panel); border-bottom: 1px solid var(--line); }
header.top h1 { font-size: 16px; margin: 0; letter-spacing: .3px; }
header.top .who { color: var(--muted); font-size: 13px; }
header.top form { display: inline; }
main { padding: 24px; max-width: 1500px; margin: 0 auto; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
        padding: 16px 18px; }
.card h2 { margin: 0 0 2px; font-size: 15px; display: flex; align-items: center; gap: 8px; }
.card p.summary { color: var(--muted); margin: 6px 0 12px; font-size: 13px; }

/* The console proper: a fixed app list on the left, one app's detail on the right. */
.shell { display: flex; align-items: stretch; min-height: calc(100vh - 57px); }
nav.apps { width: 260px; flex: 0 0 260px; background: var(--panel);
           border-right: 1px solid var(--line); padding: 10px 0 24px; overflow-y: auto; }
nav.apps .group { color: var(--muted); font-size: 11px; letter-spacing: .6px;
                  text-transform: uppercase; padding: 14px 18px 6px; }
nav.apps a { display: flex; align-items: center; gap: 9px; padding: 7px 18px; color: var(--text);
             font-size: 13px; border-left: 2px solid transparent; }
nav.apps a:hover { background: var(--panel-2); text-decoration: none; }
nav.apps a.active { background: var(--panel-2); border-left-color: var(--accent);
                    color: var(--accent); font-weight: 600; }
nav.apps a .ord { color: var(--idle); font-variant-numeric: tabular-nums; font-size: 11px;
                  min-width: 16px; }
nav.apps a .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
nav.apps a .count { color: var(--idle); font-size: 11px; }
section.detail { flex: 1; padding: 22px 28px 40px; min-width: 0; }
section.detail > h2 { margin: 0 0 4px; font-size: 18px; }
section.detail > p.lede { color: var(--muted); margin: 0 0 4px; font-size: 13.5px; max-width: 70ch; }
section.detail > p.meta { color: var(--idle); margin: 0 0 20px; font-size: 12px; }
.tiles { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 22px; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 9px;
        padding: 12px 16px; min-width: 120px; }
.tile .n { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .l { color: var(--muted); font-size: 12px; margin-top: 2px; }
.tile.bad .n { color: var(--bad); } .tile.warn .n { color: var(--warn); }
.tile.ok .n { color: var(--ok); }
h3.block { font-size: 13px; color: var(--muted); font-weight: 500; margin: 24px 0 10px;
           text-transform: uppercase; letter-spacing: .5px; }
.cmd { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px;
       padding: 10px 12px; margin-bottom: 8px; }
.cmd .name { font-weight: 600; font-size: 13px; }
.cmd .meta { color: var(--muted); font-size: 12px; margin-top: 3px;
             display: flex; flex-wrap: wrap; gap: 4px 12px; }
.cmd code { font-size: 11px; color: var(--muted); word-break: break-all; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.dot.ok { background: var(--ok); } .dot.bad { background: var(--bad); }
.dot.warn { background: var(--warn); } .dot.idle { background: var(--idle); }
.tag { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line);
       color: var(--muted); }
.tag.ok { color: var(--ok); border-color: #23502c; }
.tag.bad { color: var(--bad); border-color: #5c2325; }
.tag.warn { color: var(--warn); border-color: #57430f; }
.tag.off { color: var(--idle); }
.cfg { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line); font-size: 12px; }
.cfg a { display: inline-block; margin: 2px 10px 2px 0; }
.cfg .count { color: var(--muted); }
.none { color: var(--idle); font-size: 12px; font-style: italic; }
footer { color: var(--muted); font-size: 12px; padding: 8px 24px 28px; text-align: center; }
button, input[type=submit] { font: inherit; cursor: pointer; border-radius: 6px;
       border: 1px solid var(--line); background: var(--panel-2); color: var(--text);
       padding: 7px 14px; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff;
       width: 100%; padding: 10px; font-weight: 600; }
button:hover { border-color: var(--accent); }
.login-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center;
              padding: 24px; }
.login { width: 100%; max-width: 360px; background: var(--panel); border: 1px solid var(--line);
         border-radius: 12px; padding: 28px; }
.login h1 { margin: 0 0 4px; font-size: 19px; }
.login p.sub { color: var(--muted); margin: 0 0 22px; font-size: 13px; }
.login label { display: block; font-size: 12px; color: var(--muted); margin: 14px 0 5px; }
.login input { width: 100%; padding: 9px 11px; border-radius: 6px; border: 1px solid var(--line);
               background: var(--bg); color: var(--text); font: inherit; }
.login input:focus { outline: none; border-color: var(--accent); }
.login .actions { margin-top: 22px; }
.alert { border-radius: 6px; padding: 9px 12px; font-size: 13px; margin-bottom: 4px; }
.alert.bad { background: #2a1416; border: 1px solid #5c2325; color: #ffb4ae; }
.alert.info { background: #14212a; border: 1px solid #234a5c; color: #9fd3ea; }
.alert.ok { background: #12210f; border: 1px solid #23502c; color: #a7e0ad; }
.crumbs { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
.wide { max-width: 1100px; margin: 0 auto; }
table.records { width: 100%; border-collapse: collapse; font-size: 13px; }
table.records th { text-align: left; color: var(--muted); font-weight: 500; font-size: 12px;
       border-bottom: 1px solid var(--line); padding: 7px 10px; }
table.records td { border-bottom: 1px solid var(--line); padding: 7px 10px; vertical-align: top; }
table.records tr.retired td { opacity: .55; }
table.records td.key { font-family: ui-monospace, Consolas, monospace; }
table.records td.actions { text-align: right; white-space: nowrap; }
.section-title { display: flex; align-items: baseline; justify-content: space-between;
       margin: 22px 0 8px; }
.section-title h3 { margin: 0; font-size: 14px; }
.section-title .keys { color: var(--muted); font-size: 12px; }
textarea.json { width: 100%; min-height: 420px; padding: 12px; border-radius: 8px;
       border: 1px solid var(--line); background: var(--bg); color: var(--text);
       font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; line-height: 1.55;
       resize: vertical; }
textarea.json:focus { outline: none; border-color: var(--accent); }
.row-actions { display: flex; gap: 10px; align-items: center; margin-top: 14px; flex-wrap: wrap; }
.row-actions .spacer { flex: 1; }
button.danger { color: var(--bad); border-color: #5c2325; }
button.danger:hover { background: #2a1416; border-color: var(--bad); }
button.small { padding: 4px 10px; font-size: 12px; }
.history { margin-top: 26px; font-size: 12.5px; }
.history h3 { font-size: 14px; margin: 0 0 8px; }
.history .entry { border-left: 2px solid var(--line); padding: 4px 0 4px 12px; margin-bottom: 6px;
       color: var(--muted); }
.history .entry b { color: var(--text); font-weight: 600; }
.hint { color: var(--muted); font-size: 12px; margin: 6px 0 0; }
form.inline { display: inline; }
table.fields { width: 100%; border-collapse: collapse; font-size: 13px; }
table.fields th { text-align: left; color: var(--muted); font-weight: 500; font-size: 12px;
       border-bottom: 1px solid var(--line); padding: 6px 10px; }
table.fields td { border-bottom: 1px solid var(--line); padding: 5px 10px; vertical-align: top; }
table.fields td.name { width: 30%; color: var(--text); font-size: 12.5px; white-space: nowrap; }
table.fields td.name .k { color: var(--muted); font-size: 11px; margin-left: 6px; }
table.fields td.val { width: 70%; }
table.fields tr.section td { background: var(--panel-2); border-top: 1px solid var(--line);
       padding-top: 9px; padding-bottom: 9px; }
table.fields tr.section .label { font-weight: 600; font-size: 12.5px; }
table.fields tr.section .hint { color: var(--muted); font-size: 11.5px; margin-left: 10px; }
table.fields input[type=text], table.fields input[type=number], table.fields textarea {
       width: 100%; padding: 5px 8px; border-radius: 5px; border: 1px solid var(--line);
       background: var(--bg); color: var(--text); font: inherit; font-size: 12.5px; }
table.fields textarea { font-family: ui-monospace, Consolas, monospace; font-size: 12px;
       resize: vertical; }
table.fields input:focus, table.fields textarea:focus { outline: none; border-color: var(--accent); }
table.fields input[readonly], table.fields textarea[readonly] { color: var(--muted); }
table.fields .indent { display: inline-block; }
table.fields .null-hint { color: var(--idle); font-size: 11px; margin-left: 8px; }
label.check { display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
       font-size: 12.5px; color: var(--muted); }
label.check input { width: 15px; height: 15px; accent-color: var(--accent); }
details.raw { margin-top: 20px; border: 1px solid var(--line); border-radius: 8px;
       padding: 10px 14px; background: var(--panel); }
details.raw summary { cursor: pointer; color: var(--muted); font-size: 12.5px; }
details.raw[open] summary { margin-bottom: 10px; }
details.raw p.hint { margin-top: 0; }
.logbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.logbar select { font: inherit; font-size: 12.5px; padding: 5px 9px; border-radius: 6px;
       border: 1px solid var(--line); background: var(--bg); color: var(--text); max-width: 340px; }
.logbar .note { color: var(--muted); font-size: 12px; }
#logscroll { max-height: 62vh; overflow-y: auto; border: 1px solid var(--line);
       border-radius: 8px; background: var(--panel); }
table.logrows { width: 100%; border-collapse: collapse;
       font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
table.logrows td { padding: 3px 10px; border-bottom: 1px solid #20242d; vertical-align: top;
       white-space: pre-wrap; word-break: break-word; }
table.logrows td.t { white-space: nowrap; color: var(--muted); width: 1%; }
table.logrows td.lv { white-space: nowrap; width: 1%; font-weight: 600; }
table.logrows td.fn { white-space: nowrap; width: 1%; color: var(--muted); }
table.logrows tr.LOGGING td.lv { color: var(--idle); }
table.logrows tr.WARNING td.lv { color: var(--warn); }
table.logrows tr.ERROR td.lv, table.logrows tr.CRITICAL td.lv { color: var(--bad); }
table.logrows tr.raw td { color: var(--muted); }
#logmore { padding: 10px; text-align: center; color: var(--muted); font-size: 12px; }
"""


def _document(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def json_script(payload: Any) -> str:
    """Embed JSON in a page without letting it close the script element.

    ``json.dumps`` escapes nothing an HTML parser cares about, so a config value containing
    ``</script>`` ends the block and everything after it is parsed as markup. Escaping the three
    sequences that can start a tag is the whole fix, and it keeps the text valid JSON.
    """
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def login_page(*, prefix: str, next_url: str = "", has_users: bool = True,
               error: str = "", username: str = "") -> str:
    """The login form. ``has_users=False`` explains a store with no accounts yet.

    Without that branch a fresh deployment answers every attempt with "wrong username or
    password", which reads as a broken login rather than an empty one.
    """
    banner = ""
    if error:
        banner = f'<div class="alert bad">{escape(error)}</div>'
    elif not has_users:
        banner = ('<div class="alert info">No accounts exist yet. Create the first one on the '
                  'worker with<br><code>python -m db_ops.webhost.cli user-add --username '
                  '&lt;name&gt; --level 100 --password-stdin</code></div>')
    body = f"""
<div class="login-wrap"><form class="login" method="post" action="{escape(prefix)}/login">
  <h1>db_ops console</h1>
  <p class="sub">DBA operations for the monitored estate.</p>
  {banner}
  <label for="username">Username</label>
  <input id="username" name="username" autocomplete="username" autofocus
         value="{escape(username)}" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <input type="hidden" name="next" value="{escape(next_url)}">
  <div class="actions"><button class="primary" type="submit">Sign in</button></div>
</form></div>
"""
    return _document("Sign in — db_ops", body)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def _top_bar(prefix: str, session: dict[str, Any], *, back: str = "") -> str:
    """The header every signed-in page carries: who, until when, and the way out.

    ``back`` is only for the pages *below* an app in the tree — a config file, a record — where the
    sidebar says which app you are in but not which file. Everywhere else the sidebar is the
    navigation and a back link would be a second, competing one.
    """
    who = escape(session.get("display_name") or session.get("username"))
    home = (f'<a href="{escape(back)}">&larr; back</a> &middot; ' if back else "")
    return f"""
<header class="top">
  <h1><a href="{escape(prefix)}/" style="color:inherit">db_ops console</a></h1>
  <div class="who">
    {home}{who} &middot; level {escape(session.get('user_level'))}
    &middot; session until {escape(str(session.get('expires_at'))[:10])}
    <form method="post" action="{escape(prefix)}/logout">
      <button type="submit">Sign out</button>
    </form>
  </div>
</header>"""


def _shell(*, prefix: str, session: dict[str, Any], nav: list[dict[str, Any]],
           active: str, detail: str, title: str) -> str:
    """The console layout: the app list on the left, one thing at a time on the right.

    It was a grid of fourteen cards. Fourteen cards is a wall — everything competing, nothing
    readable, and the app you wanted somewhere in the middle. A fixed list is how an operator
    actually navigates: the same fourteen names in the same order in the same place on every page,
    so the eye learns where "Telegram App" is and stops reading.

    The order is the docs' order (`ord`), which is also the order the estate is usually explained
    in — store, logging, scheduler, then the apps that do work.
    """
    body = f"""
{_top_bar(prefix, session)}
<div class="shell">
  {_sidebar(prefix, nav, active)}
  <section class="detail">{detail}</section>
</div>
"""
    return _document(title, body)


def _sidebar(prefix: str, nav: list[dict[str, Any]], active: str) -> str:
    """The fourteen apps, top to bottom, with a dot for how each is doing.

    The dot carries the whole estate at a glance, which is the one thing the card grid was good
    at: without it the list would make you click fourteen times to learn nothing is broken.
    """
    items = []
    for item in nav:
        cls = " active" if item["app_code"] == active else ""
        dot = f'<span class="dot {item.get("dot", "idle")}"></span>'
        count = (f'<span class="count">{escape(item["commands"])}</span>'
                 if item.get("commands") else "")
        items.append(
            f'<a class="item{cls}" href="{escape(prefix)}/app/{escape(item["app_code"])}">'
            f'{dot}<span class="ord">{int(item.get("ord") or 0):02d}</span>'
            f'<span class="name">{escape(item["display_name"])}</span>{count}</a>')
    overview = " active" if not active else ""
    return f"""
<nav class="apps">
  <a class="item{overview}" href="{escape(prefix)}/"><span class="dot idle"></span>
     <span class="ord"></span><span class="name">Overview</span></a>
  <div class="group">Apps</div>
  {"".join(items)}
</nav>"""


def nav_items(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The sidebar rows: name, order, how many commands, and the worst state among them."""
    rows = []
    for block in blocks:
        commands = [item for item in block.get("commands") or [] if not item.get("missing")]
        rows.append({
            "app_code": block["app_code"],
            "ord": block.get("ord") or 0,
            "display_name": block.get("display_name") or block["app_code"],
            "commands": len(commands),
            "dot": _worst_dot(commands),
        })
    return rows


def _worst_dot(commands: list[dict[str, Any]]) -> str:
    """Worst-first, because a sidebar that averaged its apps would hide the broken one."""
    worst = "idle"
    for item in commands:
        dot, _tag = _status_marks(item, item.get("status") or {})
        if dot == "bad":
            return "bad"
        if dot == "warn":
            worst = "warn"
        elif dot == "ok" and worst == "idle":
            worst = "ok"
    return worst


def overview_page(*, prefix: str, session: dict[str, Any], blocks: list[dict[str, Any]],
                  can_edit: bool, can_run: bool, generated_at: str, notice: str = "") -> str:
    """What the console opens on: how the estate is doing, and only what needs attention.

    Deliberately not "every app's detail at once" — that was the card grid, and it made the two
    apps that were failing indistinguishable from the twelve that were fine.
    """
    commands = [item for block in blocks for item in block.get("commands") or []
                if not item.get("missing")]
    failing = [item for item in commands if (item.get("status") or {}).get("failing")]
    overdue = [item for item in commands if (item.get("status") or {}).get("overdue")
               and not (item.get("status") or {}).get("failing")]
    queued = [item for item in commands if item.get("queued")]
    healthy = len(commands) - len(failing) - len(overdue)

    banner = f'<div class="alert ok" style="margin-bottom:16px">{escape(notice)}</div>' if notice else ""
    tiles = "".join([
        f'<div class="tile"><div class="n">{len(blocks)}</div><div class="l">apps</div></div>',
        f'<div class="tile ok"><div class="n">{healthy}</div><div class="l">healthy</div></div>',
        f'<div class="tile bad"><div class="n">{len(failing)}</div><div class="l">failing</div></div>',
        f'<div class="tile warn"><div class="n">{len(overdue)}</div><div class="l">overdue</div></div>',
        f'<div class="tile"><div class="n">{len(queued)}</div><div class="l">queued</div></div>',
    ])

    csrf = str(session.get("csrf_token") or "")
    attention = failing + overdue + queued
    if attention:
        rows = "\n".join(_command_row(item, prefix=prefix, can_run=can_run, csrf=csrf)
                         for item in attention)
        needs = f'<h3 class="block">Needs attention</h3>{rows}'
    else:
        needs = ('<h3 class="block">Needs attention</h3>'
                 '<div class="none">Nothing is failing, overdue or queued.</div>')

    # An empty dashboard is not the same as a healthy one, and it used to render identically:
    # five zeroed tiles and "nothing is failing". The console draws *blocks*, which come from
    # `webhost_config.json` **through the store**, so a tool root whose config has never been
    # synced shows nothing however many app commands are configured and active. That is what a
    # fresh install looked like until 2026-08-24 — and it looks like a working console with no
    # work in it, which is the worst thing it could look like.
    if not blocks:
        empty = (
            '<div class="alert warn" style="margin-bottom:16px">'
            '<b>No apps to show yet.</b> The console reads its layout and schedules from the '
            'config store, not from <code>data/*.json</code> directly, and nothing has been '
            'loaded into it. Run:'
            '<pre style="margin:8px 0 0">db-ops db sync-config &#39;{&quot;actor&quot;:&quot;you&quot;}&#39;</pre>'
            'then reload. If that reports a missing <code>data/config_catalog.json</code>, this '
            'tool root predates <code>db-ops init</code> writing one; copy '
            '<code>data/config_catalog.example.json</code> over it.'
            '</div>')
    else:
        empty = ""

    detail = f"""
{banner}
{empty}
<h2>Overview</h2>
<p class="lede">Every db_ops app on this node. Pick one on the left for its schedule, its run
   history and the config it owns.</p>
<p class="meta">{escape(generated_at)} &middot; {'editing enabled' if can_edit else 'read only'}
   &middot; {'may run apps' if can_run else 'may not run apps'}</p>
<div class="tiles">{tiles}</div>
{needs}
"""
    return _shell(prefix=prefix, session=session, nav=nav_items(blocks), active="",
                  detail=detail, title="db_ops console")


def app_page(*, prefix: str, session: dict[str, Any], blocks: list[dict[str, Any]],
             block: dict[str, Any], can_edit: bool, can_run: bool, notice: str = "",
             logs: dict[str, Any] | None = None,
             config_inline: dict[str, Any] | None = None) -> str:
    """One app: what it runs, how that has been going, and the config it owns.

    ``logs`` is only passed for the logging engine, whose subject *is* the running logs — see
    ``WebApp._logs_panel``.
    """
    csrf = str(session.get("csrf_token") or "")
    commands = block.get("commands") or []
    if commands:
        command_html = "\n".join(
            _command_row(item, prefix=prefix, can_run=can_run, csrf=csrf) for item in commands)
    else:
        command_html = ('<div class="none">No scheduled command &mdash; this component is used by '
                        'the other apps rather than run on its own.</div>')

    config = block.get("config") or []
    if config_inline is not None:
        # One file: shown, not linked. See WebApp._inline_config.
        config_html = _config_sections(config_inline, prefix=prefix, can_edit=can_edit,
                                       with_hint=True)
    elif config:
        rows = "\n".join(
            f'<tr><td class="key"><a href="{escape(prefix)}/config/{escape(item["source_file"])}">'
            f'{escape(item["source_file"])}</a></td>'
            f'<td>{escape(item["display_name"] or "")}</td>'
            f'<td>{escape(item["records"])} record(s)</td></tr>'
            for item in config)
        config_html = ('<table class="records">'
                       '<tr><th>file</th><th>what it holds</th><th></th></tr>'
                       f'{rows}</table>')
    else:
        config_html = '<div class="none">This app owns no config file of its own.</div>'

    doc = block.get("doc") or ""
    doc_html = f'<p class="meta">Reference: <code>{escape(doc)}</code></p>' if doc else ""
    banner = f'<div class="alert ok" style="margin-bottom:16px">{escape(notice)}</div>' if notice else ""

    detail = f"""
{banner}
<h2>{escape(block.get('display_name'))}</h2>
<p class="lede">{escape(block.get('summary'))}</p>
{doc_html}
{_logs_panel(prefix, logs) if logs is not None else ""}
<h3 class="block">Scheduled commands</h3>
{command_html}
<h3 class="block">{escape(_config_heading(config_inline))}</h3>
{config_html}
"""
    return _shell(prefix=prefix, session=session, nav=nav_items(blocks),
                  active=str(block["app_code"]), detail=detail,
                  title=f"{block.get('display_name')} — db_ops")


def _command_row(item: dict[str, Any], *, prefix: str, can_run: bool, csrf: str) -> str:
    if item.get("missing"):
        return (f'<div class="cmd"><div class="name">{escape(item["app_command_id"])}</div>'
                '<div class="meta"><span class="tag bad">not in app_commands.json</span></div></div>')

    status = item.get("status") or {}
    dot, tag = _status_marks(item, status)
    meta = [f"<span>{escape(item.get('schedule_text'))}</span>"]
    if item.get("timeout"):
        meta.append(f"<span>timeout {escape(item['timeout'])}s</span>")
    if item.get("node_role"):
        meta.append(f"<span>{escape(item['node_role'])}</span>")
    if status:
        meta.append(f"<span>last run {escape(status.get('last_run') or 'never')}</span>")
        meta.append(f"<span>{escape(status.get('runs', 0))} runs / "
                    f"{escape(status.get('failed', 0))} failed (24h)</span>")
    error = str(status.get("last_error") or "").strip()
    error_html = (f'<div class="meta"><span class="tag bad">{escape(error[:160])}</span></div>'
                  if error else "")
    return f"""
<div class="cmd">
  <div class="name"><span class="dot {dot}"></span>{escape(item.get('display_name'))} {tag}
    {_run_control(item, prefix=prefix, can_run=can_run, csrf=csrf)}</div>
  <div class="meta">{''.join(meta)}</div>
  <div class="meta"><code>{escape(item.get('command_text'))}</code></div>
  {error_html}
</div>"""


def _run_control(item: dict[str, Any], *, prefix: str, can_run: bool, csrf: str) -> str:
    """The Run button, or what is already queued.

    A queued request replaces the button rather than sitting beside it: offering "Run now" for
    something already waiting invites the double-click the queue then has to refuse, and the
    honest state is "it is about to run", not "you may ask again".
    """
    queued = item.get("queued")
    if queued:
        return (f'<span class="tag warn" title="requested by {escape(queued.get("requested_by"))} '
                f'at {escape(queued.get("requested_at"))}">{escape(queued.get("status"))}</span>')
    if not can_run or not item.get("active", True):
        return ""
    action = f'{escape(prefix)}/apps/{escape(item["app_command_id"])}/run'
    origin = (f'<input type="hidden" name="from" value="app/{escape(item["app_code"])}">'
              if item.get("app_code") else "")
    return (f'<form class="inline" method="post" action="{action}" style="float:right">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">{origin}'
            '<button class="small" type="submit">Run now</button></form>')


def _status_marks(item: dict[str, Any], status: dict[str, Any]) -> tuple[str, str]:
    """The dot and the tag for one command.

    Inactive wins over every run state: a command switched off in config is not "failing", and
    colouring it red is how a deliberate change starts looking like an incident.
    """
    if not item.get("active", True):
        return "idle", '<span class="tag off">disabled</span>'
    if not status:
        return "idle", '<span class="tag off">no run history</span>'
    if status.get("failing"):
        return "bad", '<span class="tag bad">failing</span>'
    if status.get("overdue"):
        return "warn", '<span class="tag warn">overdue</span>'
    return "ok", '<span class="tag ok">healthy</span>'


# --------------------------------------------------------------------------- #
# The running log
# --------------------------------------------------------------------------- #
def _logs_panel(prefix: str, logs: dict[str, Any]) -> str:
    """The live log, newest line first, a hundred at a time.

    Newest first is the only order that makes sense for a log you are watching: the line you want
    is the one just written. The hundred is what fits on a screen; older lines arrive as the
    operator scrolls, so opening the page costs one page of a file that may be hundreds of
    megabytes.
    """
    if logs.get("error"):
        return f'<h3 class="block">Running log</h3><div class="none">{escape(logs["error"])}</div>'

    options = "".join(
        f'<option value="{escape(item["name"])}"'
        f'{" selected" if item["name"] == logs["selected"] else ""}>'
        f'{escape(item["name"])} &middot; {_size_text(item["size"])}</option>'
        for item in logs["files"])
    rows = "".join(_log_row(line) for line in logs["lines"])
    more = ("Scroll for older lines" if logs.get("next_before") is not None
            else "That is the whole file.")

    return f"""
<h3 class="block">Running log</h3>
<div class="logbar">
  <select id="logfile">{options}</select>
  <span class="note">newest first &middot; 100 lines at a time, more as you scroll</span>
</div>
<div id="logscroll" data-prefix="{escape(prefix)}"
     data-file="{escape(logs["selected"])}"
     data-before="{escape(logs.get("next_before") if logs.get("next_before") is not None else "")}">
  <table class="logrows"><tbody id="logbody">{rows}</tbody></table>
  <div id="logmore">{escape(more)}</div>
</div>
{_LOG_SCRIPT}"""


def _log_row(line: dict[str, Any]) -> str:
    if not line.get("structured"):
        # A raw stdout line — a traceback, a driver's warning. Kept whole across the row rather
        # than forced into columns it does not have, because the line that does not fit the format
        # is usually the reason somebody opened the log.
        return f'<tr class="raw"><td colspan="4">{escape(line["text"])}</td></tr>'
    return (f'<tr class="{escape(line["level"])}">'
            f'<td class="t">{escape(line["timestamp"])}</td>'
            f'<td class="lv">{escape(line["level"])}</td>'
            f'<td class="fn">{escape(line["function"])}</td>'
            f'<td>{escape(line["message"])}</td></tr>')


def _size_text(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


#: The console's only JavaScript, and it is inline for the same reason the CSS is: this page is
#: served from inside the worker container, on a network that may not reach the internet, so there
#: is no bundle, no CDN and no build step. It does two things — swap the file, and fetch the next
#: hundred lines when the reader nears the bottom — and it degrades to "the newest hundred lines,
#: server-rendered" if scripting is off, which is still the useful part.
_LOG_SCRIPT = """
<script>
(function () {
  var box = document.getElementById("logscroll");
  var body = document.getElementById("logbody");
  var more = document.getElementById("logmore");
  var picker = document.getElementById("logfile");
  if (!box || !body) { return; }

  picker.addEventListener("change", function () {
    var url = new URL(window.location.href);
    url.searchParams.set("file", picker.value);
    window.location.href = url.toString();
  });

  var loading = false;
  function render(line) {
    var row = document.createElement("tr");
    if (!line.structured) {
      row.className = "raw";
      var whole = document.createElement("td");
      whole.colSpan = 4;
      whole.textContent = line.text;
      row.appendChild(whole);
      return row;
    }
    row.className = line.level;
    [["t", line.timestamp], ["lv", line.level], ["fn", line.function], ["", line.message]]
      .forEach(function (pair) {
        var cell = document.createElement("td");
        if (pair[0]) { cell.className = pair[0]; }
        // textContent, never innerHTML: a log line is whatever a database or a driver wrote,
        // and this page has just been handed it.
        cell.textContent = pair[1];
        row.appendChild(cell);
      });
    return row;
  }

  function loadMore() {
    var before = box.dataset.before;
    if (loading || !before) { return; }
    loading = true;
    more.textContent = "Loading older lines...";
    var url = box.dataset.prefix + "/api/logs?file=" + encodeURIComponent(box.dataset.file) +
              "&before=" + encodeURIComponent(before);
    fetch(url, { credentials: "same-origin" })
      .then(function (response) { return response.json(); })
      .then(function (page) {
        (page.lines || []).forEach(function (line) { body.appendChild(render(line)); });
        box.dataset.before = page.next_before === null ? "" : page.next_before;
        more.textContent = box.dataset.before ? "Scroll for older lines"
                                              : "That is the whole file.";
        loading = false;
        // One more round if the new rows did not fill the box, or the reader is stuck at a
        // bottom that never moves.
        if (box.dataset.before && box.scrollHeight <= box.clientHeight) { loadMore(); }
      })
      .catch(function () {
        more.textContent = "Could not load older lines.";
        loading = false;
      });
  }

  box.addEventListener("scroll", function () {
    if (box.scrollTop + box.clientHeight >= box.scrollHeight - 120) { loadMore(); }
  });
  if (box.scrollHeight <= box.clientHeight) { loadMore(); }
})();
</script>"""


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def error_page(title: str, detail: str, *, prefix: str, back: str = "") -> str:
    """A refusal the operator can act on: what was wrong, and the way back to where they were."""
    target = back or f"{prefix}/"
    body = f"""
<div class="login-wrap"><div class="login">
  <h1>{escape(title)}</h1>
  <div class="alert bad">{escape(detail)}</div>
  <div class="actions"><a href="{escape(target)}">Back</a></div>
</div></div>
"""
    return _document(f"{title} — db_ops", body)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _config_heading(config_inline: dict[str, Any] | None) -> str:
    """What the config block on an app page is called.

    Named after the file when there is only one, because "Config" above a single file's records is
    a heading that says less than the thing under it.
    """
    if config_inline is None:
        return "Config"
    return config_inline.get("display_name") or config_inline.get("source_file") or "Config"


def _config_sections(view: dict[str, Any], *, prefix: str, can_edit: bool,
                     with_hint: bool = False) -> str:
    """A config file's records, grouped by collection, with the file settings last.

    The one renderer for a file's records: the file's own page uses it, and so does an app page
    that opens its only file inline. Two renderers would eventually show the same file two ways.

    ``with_hint`` adds the "this writes the file too" line and the retired toggle — which the
    file's own page already carries in its header, and an app page does not.
    """
    from db_ops.db.config_store import DOCUMENT_COLLECTION

    groups = view["groups"]
    order = sorted(groups, key=lambda name: (name == DOCUMENT_COLLECTION, name))
    sections = "\n".join(
        _config_section(name, groups[name], prefix=prefix, source_file=view["source_file"],
                        can_edit=can_edit, is_document=(name == DOCUMENT_COLLECTION))
        for name in order)
    if not with_hint:
        return sections
    toggle = ('<a href="?">hide retired</a>' if view.get("showing_retired")
              else '<a href="?retired=1">show retired</a>')
    return (f'<p class="hint">Editing a record here writes the store <i>and</i> rewrites '
            f'<code>data/{escape(view["source_file"])}</code>, which is what the apps read. '
            f'Retiring one keeps its row and its history. &middot; {toggle}</p>') + sections


def config_file_page(*, prefix: str, session: dict[str, Any], blocks: list[dict[str, Any]],
                     source_file: str, display_name: str, description: str, app_code: str,
                     groups: dict[str, list[dict[str, Any]]], document_collection: str,
                     can_edit: bool, showing_retired: bool) -> str:
    """One config file: its records, grouped by collection, with the document settings last."""
    sections = _config_sections(
        {"groups": groups, "source_file": source_file, "showing_retired": showing_retired},
        prefix=prefix, can_edit=can_edit)
    toggle = ('<a href="?">hide retired</a>' if showing_retired
              else '<a href="?retired=1">show retired</a>')
    detail = f"""
<div class="crumbs">
  <a href="{escape(prefix)}/app/{escape(app_code)}">{escape(app_code)}</a> &rsaquo;
  <code>{escape(source_file)}</code> &middot; {toggle}
</div>
<h2>{escape(display_name)}</h2>
<p class="lede">{escape(description)}</p>
<p class="hint">Saving here writes the store <i>and</i> rewrites <code>data/{escape(source_file)}</code>,
   which is what the apps read. Retiring a record keeps its row and its history — the key becomes
   free to use again.</p>
{sections}
"""
    return _shell(prefix=prefix, session=session, nav=nav_items(blocks), active=app_code,
                  detail=detail, title=f"{display_name} — db_ops")


def _config_section(collection: str, records: list[dict[str, Any]], *, prefix: str,
                    source_file: str, can_edit: bool, is_document: bool) -> str:
    base = f"{escape(prefix)}/config/{escape(source_file)}/{escape(collection)}"
    if is_document:
        return f"""
<div class="section-title">
  <h3>File settings</h3>
  <span class="keys">everything in the file that is not a keyed record</span>
</div>
<div class="card">
  <a href="{base}/__document__">Open the file settings</a>
</div>"""

    add = (f'<a href="{base}/new"><button class="small" type="button">Add a record</button></a>'
           if can_edit else "")
    rows = "\n".join(_config_row(item, base=base, can_edit=can_edit) for item in records)
    return f"""
<div class="section-title">
  <h3>{escape(collection)} <span class="keys">&middot; {len(records)} record(s)</span></h3>
  {add}
</div>
<table class="records">
  <tr><th>key</th><th>name</th><th>rev</th><th>changed</th><th>by</th><th></th></tr>
  {rows}
</table>"""


def _config_row(item: dict[str, Any], *, base: str, can_edit: bool) -> str:
    retired = not item["is_active"]
    key = escape(item["item_key"])
    link = f'<a href="{base}/{key}">{key}</a>' if not retired else key
    tag = ' <span class="tag off">retired</span>' if retired else ""
    return f"""
<tr class="{'retired' if retired else ''}">
  <td class="key">{link}{tag}</td>
  <td>{escape(item['label'])}</td>
  <td>{escape(item['revision'])}</td>
  <td>{escape(item['updated_at'])}</td>
  <td>{escape(item['updated_by'])}</td>
  <td class="actions">{'' if retired or not can_edit else f'<a href="{base}/{key}">edit</a>'}</td>
</tr>"""


def config_record_page(*, prefix: str, session: dict[str, Any], blocks: list[dict[str, Any]],
                       source_file: str, collection: str, item_key: str | None, payload: Any,
                       history: list[dict[str, Any]], key_fields: list[str],
                       is_document: bool, can_edit: bool, app_code: str = "") -> str:
    """One record, as a grid of named fields.

    It used to be the record's JSON in one textarea. That was honest and unreadable: a metric
    definition is ninety lines of braces, and finding ``repeat_interval`` in it was worse than
    opening the file.

    The grid is generated **from the record itself** (:mod:`db_ops.lib.record_form`), not from a
    hand-written list of known fields — those records have no fixed shape, and a form built from a
    list silently drops everything it was not told about. Every leaf gets a row carrying the JSON
    type it came from, so a number stays a number and ``null`` stays null.

    The JSON box survives underneath, collapsed. It is the only way to **add or remove** a key,
    which a grid over an existing record cannot offer, and it is the escape hatch for a shape the
    grid renders awkwardly. Whichever one is submitted is what gets saved.
    """
    from db_ops.lib.record_form import flatten

    csrf = escape(str(session.get("csrf_token") or ""))
    back = f"{escape(prefix)}/config/{escape(source_file)}"
    action = f"{back}/{escape(collection)}" + (f"/{escape(item_key)}" if item_key else "/new")
    title = escape(item_key) if item_key else "new record"
    keys = (f'<span class="keys">keyed by {escape(", ".join(key_fields))}</span>'
            if key_fields else "")

    layout = flatten(payload if isinstance(payload, dict) else {})
    grid = "\n".join(_field_row(row, can_edit=can_edit, key_fields=key_fields)
                     for row in layout.rows)
    raw = escape(json.dumps(payload, ensure_ascii=False, indent=2))

    delete = ""
    if can_edit and item_key and not is_document:
        delete = f"""
<form class="inline" method="post" action="{action}/delete"
      onsubmit="return confirm('Retire {escape(item_key)}? The row and its history are kept, and the key becomes free to reuse.');">
  <input type="hidden" name="csrf" value="{csrf}">
  <button class="danger" type="submit">Retire this record</button>
</form>"""

    controls = f"""
<div class="row-actions">
  <button class="primary" type="submit" style="width:auto">Save</button>
  <a href="{back}"><button type="button">Cancel</button></a>
  <span class="spacer"></span>
  {delete}
</div>""" if can_edit else '<p class="hint">You have read-only access to config.</p>'

    detail = f"""
  <div class="crumbs">
    <a href="{back}"><code>{escape(source_file)}</code></a> &rsaquo;
    {escape(collection)} &rsaquo; <code>{title}</code> {keys}
  </div>
  <form method="post" action="{action}">
    <input type="hidden" name="csrf" value="{csrf}">
    <table class="fields">
      <tr><th>field</th><th>value</th></tr>
      {grid}
    </table>
    {controls}
  </form>

  <details class="raw">
    <summary>Edit as JSON &mdash; the only way to add or remove a key</summary>
    <p class="hint">The grid above edits the fields this record already has. To add a new one, or
       remove one, edit the JSON here and save it instead.</p>
    <form method="post" action="{action}">
      <input type="hidden" name="csrf" value="{csrf}">
      <textarea class="json" name="payload" spellcheck="false" style="min-height:280px"
                {'readonly' if not can_edit else ''}>{raw}</textarea>
      {'<div class="row-actions"><button type="submit">Save this JSON</button></div>' if can_edit else ''}
    </form>
  </details>

  {_history_block(history)}
"""
    return _shell(prefix=prefix, session=session, nav=nav_items(blocks), active=app_code,
                  detail=detail, title=f"{item_key or 'new'} — db_ops")


def _field_row(row: Any, *, can_edit: bool, key_fields: list[str]) -> str:
    """One row of the grid: a section heading, or a named field with a typed input."""
    from db_ops.lib.record_form import (
        KIND_BOOL,
        KIND_FLOAT,
        KIND_INT,
        KIND_JSON,
        KIND_NULL,
        Section,
    )

    indent = f'<span class="indent" style="width:{row.depth * 16}px"></span>'
    if isinstance(row, Section):
        hint = "empty" if row.empty else ""
        # An empty object has no fields under it to carry it through the round trip, so the
        # section posts a marker of its own.
        marker = (f'<input type="hidden" name="{escape(row.name)}" value="">'
                  if row.empty else "")
        return (f'<tr class="section"><td colspan="2">{indent}'
                f'<span class="label">{escape(row.label)}</span>'
                f'<span class="hint">{escape(hint)}</span>{marker}</td></tr>')

    name = escape(row.name)
    disabled = " readonly" if not can_edit else ""
    is_key = row.label in (key_fields or []) and row.depth == 0
    note = '<span class="k">key</span>' if is_key else f'<span class="k">{escape(_kind_label(row.kind))}</span>'

    if row.kind == KIND_BOOL:
        # The hidden field posts the "off" answer; the checkbox adds "on" when ticked, and the
        # parser reads the LAST value. Without it an unticked box submits nothing at all and the
        # field would read as unchanged rather than as false.
        checked = " checked" if row.value else ""
        control = (f'<input type="hidden" name="{name}" value="false">'
                   f'<label class="check"><input type="checkbox" name="{name}" value="true"'
                   f'{checked}{" disabled" if not can_edit else ""}>'
                   f'{"true" if row.value else "false"}</label>')
    elif row.is_list:
        rows_needed = max(2, min(12, len(row.value or []) + 1))
        control = (f'<textarea name="{name}" rows="{rows_needed}" spellcheck="false"'
                   f'{disabled}>{escape(row.text)}</textarea>'
                   '<span class="null-hint">one per line</span>')
    elif row.kind == KIND_JSON:
        control = (f'<textarea name="{name}" rows="6" spellcheck="false"{disabled}>'
                   f'{escape(row.text)}</textarea>')
    elif row.kind in (KIND_INT, KIND_FLOAT):
        step = ' step="any"' if row.kind == KIND_FLOAT else ""
        control = (f'<input type="number" name="{name}" value="{escape(row.text)}"'
                   f'{step}{disabled}>')
    elif row.kind == KIND_NULL:
        control = (f'<input type="text" name="{name}" value=""{disabled}>'
                   '<span class="null-hint">null &mdash; leave empty to keep it null</span>')
    else:
        control = f'<input type="text" name="{name}" value="{escape(row.text)}"{disabled}>'

    return (f'<tr><td class="name">{indent}{escape(row.label)}{note}</td>'
            f'<td class="val">{control}</td></tr>')


def _kind_label(kind: str) -> str:
    """What the type column says. The internal kind names are for the parser, not the reader."""
    from db_ops.lib.record_form import KIND_LIST_PREFIX

    if kind.startswith(KIND_LIST_PREFIX):
        return "list"
    return {"str": "text", "int": "number", "float": "number", "bool": "yes/no",
            "null": "empty", "json": "json"}.get(kind, kind)


def _history_block(history: list[dict[str, Any]]) -> str:
    if not history:
        return ""
    entries = "".join(
        f'<div class="entry"><b>rev {escape(item["revision"])} &middot; {escape(item["change_type"])}</b>'
        f' by {escape(item["changed_by"] or "?")} at {escape(item["changed_at"])}'
        + (f' &middot; {escape(item["note"])}' if item["note"] else "")
        + (' &middot; <span class="tag off">row retired</span>' if not item["is_active"] else "")
        + "</div>"
        for item in history[:25])
    return f'<div class="history"><h3>History</h3>{entries}</div>'
