"""Publish SLA results to the two db_ops delivery channels, without importing either app:

* Telegram — insert a row into ``telegram_send_messages`` (the telegram app's send queue).
* Web host — render a self-contained HTML page into the webhost serving root
  (``<runtime>/reports``), served at ``/report_dba/sla.html``.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from db_ops.sla.models import SlaPolicyResult, SlaValidationSummary, state_key


# Status -> (emoji, css class) used in both the Telegram text and the HTML page.
STATUS_DISPLAY = {
    "PASSED": ("✅", "ok"),
    "AT_RISK": ("⚠️", "warn"),
    "FAILED": ("❌", "bad"),
    "NO_DATA": ("⬜", "nodata"),
}


SLA_NOTIFY_LEVEL = "sla"


def summary_notify_level(summary: SlaValidationSummary, groups: dict[str, str] | None = None) -> str:
    """The notify level this SLA run reports at.

    A deployment that dedicates a group to SLA (``notify_level: "sla"``) gets **every** SLA run
    there, pass or fail — the point of a per-domain group is that one place holds the whole
    story, instead of a FAILED run landing in Criticals next to unrelated alerts.

    Without such a group configured, the old severity routing stands: FAILED -> critical,
    AT_RISK/NO_DATA -> warning, otherwise logging. ``groups`` is the level -> chat map; omitted
    (or unresolvable) it is fetched from the shared router.
    """
    if groups is None:
        try:
            from db_ops.lib.telegram_route import telegram_groups

            groups = telegram_groups()
        except Exception:  # noqa: BLE001 - routing unavailable: fall back to severity levels.
            groups = {}
    if str((groups or {}).get(SLA_NOTIFY_LEVEL) or "").strip():
        return SLA_NOTIFY_LEVEL
    if summary.status == "FAILED" or summary.failed_count > 0:
        return "critical"
    if summary.at_risk_count > 0 or summary.no_data_count > 0:
        return "warning"
    return "logging"


def chat_id_for_level(groups: dict[str, str], level: str) -> str:
    # Routing belongs to the shared layer; this app applies no rules of its own.
    from db_ops.lib.telegram_route import chat_id_for_level as _resolve

    return _resolve(groups, level)


# Telegram rejects a sendMessage body longer than 4096 chars with HTTP 400
# ("message is too long"). Keep a margin below that; the full detail is always on the
# published web report, so the message only needs the header + as many worst-first
# attention lines as fit, then a "N more" pointer.
TELEGRAM_MESSAGE_MAX_CHARS = 3900


#: How many findings of one kind get named before the message defers to the web report. Six is
#: about what a phone notification shows without being collapsed; past that the reader is scrolling
#: a wall of text to find the one line that changed, which is the habit this whole redesign breaks.
TRANSITION_DETAIL_LIMIT = 6


def build_transition_message(summary: SlaValidationSummary, diff, decision, *, report_url: str = "") -> str:
    """A message about what *changed*, not a re-listing of everything that is wrong.

    The old body restated every non-passing row every hour: 3,852 characters of mostly identical
    text, which is how a channel gets muted. This one leads with the transition counts, names only
    the findings that moved, and links to the page for the standing detail — the page is always
    current, so copying it into the message bought nothing but length.
    """
    counts = diff.counts
    # No leading emoji here: the send layer tags every outgoing message from its header line
    # (db_ops.telegram.severity), so one vocabulary covers all producers. What this function owes
    # it is a header that states the severity in words — see transition_status().
    if decision.kind == "reminder":
        headline = f"SLA daily reminder — {counts['unchanged']} unresolved"
    elif decision.kind == "baseline":
        headline = f"SLA baseline — {summary.failed_count} failing at first evaluation"
    else:
        # Every movement that happened, in one line. An earlier version said only "0 new, 3
        # recovered" while a policy had dropped to 0% in the same run: a headline that reports
        # good news and omits the bad is worse than no headline.
        moves = [f"{counts['new_failed']} new failed", f"{counts['recovered']} recovered"]
        if counts["worsened"]:
            moves.append(f"{counts['worsened']} worse")
        if counts["improved"]:
            moves.append(f"{counts['improved']} better")
        headline = "SLA changes — " + ", ".join(moves)

    lines = [
        headline,
        f"Window end: {summary.window_end}",
        f"❌ {counts['new_failed']} new failed · ✅ {counts['recovered']} recovered · "
        f"➖ {counts['unchanged']} unchanged",
        f"Fleet now: {summary.passed_count} passed · {summary.at_risk_count} at risk · "
        f"{summary.failed_count} failed · {summary.no_data_count} no-data",
    ]

    by_key = {state_key(result.policy_id, result.target_id): result for result in summary.results}
    lines += _transition_block("🆕 Newly failing", diff.new_bad, by_key)
    lines += _transition_block("✅ Recovered", diff.recovered, by_key, detail=False)
    lines += _transition_block("🔺 Worse", [item[0] for item in diff.worsened], by_key)
    lines += _transition_block("🔻 Better", [item[0] for item in diff.improved], by_key, detail=False)
    if decision.kind == "reminder":
        lines += _transition_block("➖ Still failing", diff.unchanged_bad, by_key)
    if diff.vanished_bad:
        # Explicitly not filed under "recovered": these stopped being evaluated. Saying so is the
        # difference between a fixed problem and a policy somebody quietly switched off.
        lines.append("")
        lines.append(f"⬜ {len(diff.vanished_bad)} no longer evaluated (policy or target removed)")

    if report_url:
        lines.append("")
        lines.append(f"Full detail: {report_url}")
    body = "\n".join(lines)
    if len(body) > TELEGRAM_MESSAGE_MAX_CHARS:
        body = body[: TELEGRAM_MESSAGE_MAX_CHARS - 1].rstrip() + "…"
    return body


def transition_status(diff, decision) -> str:
    """What this *message* is, for the send layer's emoji — not what the fleet is.

    The run status is FAILED on any estate with a standing backlog, so passing it through would
    stamp ❌ on a message whose only content is three recoveries. Severity here belongs to the
    change being reported: something newly broken is critical, something that got worse is a
    warning, and a message carrying only good news should look like good news.
    """
    if diff.new_bad:
        return "CRITICAL"
    if diff.worsened or decision.kind in ("reminder", "baseline"):
        return "WARNING"
    if diff.recovered or diff.improved:
        return "SUCCESS"
    return "WARNING"


def _transition_block(title: str, keys, by_key: dict, *, detail: bool = True) -> list[str]:
    """One titled group, capped, with a count of what was left out."""
    keys = list(keys)
    if not keys:
        return []
    lines = ["", f"{title} ({len(keys)}):"]
    for key in keys[:TRANSITION_DETAIL_LIMIT]:
        result = by_key.get(key)
        if result is not None and detail:
            lines.append(f"• {key}: {result.status} {result.actual_percent}% / SLO {result.objective_percent}%")
        else:
            lines.append(f"• {key}")
    if len(keys) > TRANSITION_DETAIL_LIMIT:
        lines.append(f"• …and {len(keys) - TRANSITION_DETAIL_LIMIT} more")
    return lines


def _result_label(result: SlaPolicyResult) -> str:
    """A per-instance label: ``POLICY @ target`` (or just the policy for aggregate/no-data)."""
    if result.target_id and result.target_id != "*":
        return f"{result.policy_id} @ {result.target_id}"
    return result.policy_id


#: Data-quality verdicts meaning the collector could not produce a usable measurement. Counted
#: apart from service failures: "we cannot see this server" and "this server is breaching its
#: objective" need different people to do different things.
UNMEASURABLE_QUALITY = ("NO_DATA", "COLLECTION_FAILED", "STALE", "INSUFFICIENT_DATA")


def render_html(summary: SlaValidationSummary, *, recent_runs: list[dict],
                previous_state: dict[str, str] | None = None, history_limit: int = 0) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    # Sections per server, not one fleet-wide list: see _server_sections.
    rows = _server_sections(summary)
    history = "\n".join(_html_history_row(run) for run in recent_runs) or (
        '<tr><td colspan="6" class="muted">No stored runs.</td></tr>'
    )
    banner_emoji, banner_class = STATUS_DISPLAY.get(summary.status, ("", "nodata"))
    return _PAGE_TEMPLATE.format(
        generated_at=html.escape(generated_at),
        status=html.escape(summary.status),
        banner_class=banner_class,
        banner_emoji=banner_emoji,
        window_end=html.escape(summary.window_end),
        passed=summary.passed_count,
        at_risk=summary.at_risk_count,
        failed=summary.failed_count,
        no_data=summary.no_data_count,
        serving_bad=sum(1 for result in summary.results if result.current_status == "BAD"),
        debt_objects=sum(result.affected_objects for result in summary.results
                         if result.policy_model == "finding_inventory"),
        quality_bad=sum(1 for result in summary.results
                        if result.data_quality_status in UNMEASURABLE_QUALITY),
        headline_note=html.escape(_headline_note()),
        delta_note=html.escape(_delta_note(summary, previous_state)),
        history_note=html.escape(_history_note(recent_runs, history_limit)),
        domain_sections=_domain_sections(summary),
        rows=rows,
        history=history,
    )


def _headline_note() -> str:
    """Say what the headline numbers mean, because they answer four different questions.

    "Failed" alone conflated a service that is down, a service that breached its objective some
    time in the last seven days, a maintenance backlog, and a target nobody could log into. All
    four were one red number, and an operator cannot act on that.
    """
    return (
        "Bad right now = the newest collection is failing; this is the one to act on. "
        "Window breach = the rolling objective is missed, and the service may already have "
        "recovered. Objects in backlog = operational debt, work to schedule rather than an "
        "incident. Cannot measure = the collector produced no reading, which is a monitoring "
        "fault rather than a service fault."
    )


def _delta_note(summary: SlaValidationSummary, previous_state: dict[str, str] | None) -> str:
    """How this run differs from the previous one.

    A page that shows only a level leaves the reader to remember what it said an hour ago. This
    uses the same comparison that drives the Telegram routing, so the page and the alert cannot
    disagree about what changed.
    """
    if previous_state is None:
        return "No previous run stored, so this page shows no change figures yet."
    from db_ops.lib.state_transition import diff_states

    current = {state_key(result.policy_id, result.target_id): result.status for result in summary.results}
    diff = diff_states(previous_state, current, severity_order=("FAILED", "AT_RISK", "NO_DATA", "PASSED"),
                       healthy=("PASSED",))
    counts = diff.counts
    text = (f"Since the previous run: {counts['new_failed']} newly failing, "
            f"{counts['recovered']} recovered, {counts['worsened']} worse, "
            f"{counts['improved']} better, {counts['unchanged']} unchanged.")
    if counts["vanished"]:
        # Never folded into "recovered": these stopped being evaluated, which is a monitoring
        # change and not a fix.
        text += f" {counts['vanished']} are no longer evaluated (policy or target removed)."
    return text


def _history_note(recent_runs: list[dict], history_limit: int) -> str:
    """State the range the table covers.

    It shows the newest 15 runs and said so nowhere, so on an hourly schedule a reader was looking
    at roughly the last 15 hours while reasonably assuming it was the whole history.
    """
    if not recent_runs:
        return "No runs stored yet."
    newest = str(recent_runs[0].get("finished_at") or recent_runs[0].get("started_at") or "")
    oldest = str(recent_runs[-1].get("finished_at") or recent_runs[-1].get("started_at") or "")
    capped = " (capped)" if history_limit and len(recent_runs) >= history_limit else ""
    return (f"Showing the newest {len(recent_runs)} runs{capped}, {oldest} to {newest}. "
            f"The store keeps them all.")


def publish_html(summary: SlaValidationSummary, *, recent_runs: list[dict], out_dir: str | Path,
                 previous_state: dict[str, str] | None = None, history_limit: int = 0) -> Path:
    """Write the stable ``sla.html`` (served at /report_dba/sla.html) plus a dated archive
    copy, refresh the ``index.html`` landing hub, and return the stable page path."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    page = render_html(summary, recent_runs=recent_runs, previous_state=previous_state,
                       history_limit=history_limit)
    stable_path = directory / "sla.html"
    stable_path.write_text(page, encoding="utf-8")
    # One archive per DAY, overwritten, via the shared helper — not one per run. Stamping every
    # hourly run left 696 files and 422 MB in the serving directory over four weeks, growing
    # without bound: an archive nobody prunes eventually costs more than the history is worth.
    # archive_daily also keeps the naming identical to the other published reports.
    from db_ops.lib.report_archive import archive_daily

    archive_daily([stable_path], stamp=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    publish_index(summary, out_dir=directory)
    return stable_path


def publish_index(summary: SlaValidationSummary | None, *, out_dir: str | Path) -> Path:
    """Write/refresh ``index.html`` — the landing hub for /report_dba/ that links to the SLA
    page and the inventory report. Idempotent; safe to call on every publish."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    index_path = directory / "index.html"
    index_path.write_text(render_index_html(summary, directory=directory), encoding="utf-8")
    return index_path


def render_index_html(summary: SlaValidationSummary | None, *, directory: Path) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    if summary is not None:
        emoji, css = STATUS_DISPLAY.get(summary.status, ("", "nodata"))
        sla_status = (
            f'<span class="badge {css}">{emoji} {html.escape(summary.status)}</span> '
            f"· {summary.passed_count} passed / {summary.at_risk_count} at risk / "
            f"{summary.failed_count} failed / {summary.no_data_count} no-data"
        )
    else:
        sla_status = '<span class="muted">not evaluated yet</span>'
    inventory_available = bool(list(directory.glob("*_database-inventory-report.html"))) or (directory / "database-inventory.html").exists()
    inventory_card = _index_card(
        href="database-inventory.html",
        emoji="🗄️",
        title="Database inventory report",
        note="Servers, storage, health triage · supports ?date=" if inventory_available else "not generated yet",
        disabled=not inventory_available,
    )
    sla_card = _index_card(href="sla.html", emoji="📊", title="SLA / SLO compliance", note=sla_status, disabled=False)
    return _INDEX_TEMPLATE.format(generated_at=html.escape(generated_at), sla_card=sla_card, inventory_card=inventory_card)


def _index_card(*, href: str, emoji: str, title: str, note: str, disabled: bool) -> str:
    cls = "hub-card disabled" if disabled else "hub-card"
    inner = (
        f'<div class="hub-title">{emoji} {html.escape(title)}</div>'
        f'<div class="hub-note">{note}</div>'
    )
    if disabled:
        return f'<div class="{cls}">{inner}</div>'
    return f'<a class="{cls}" href="{html.escape(href)}">{inner}</a>'


def _worst_first(results) -> list[SlaPolicyResult]:
    order = {"FAILED": 0, "NO_DATA": 1, "STALE": 1, "INSUFFICIENT_DATA": 1, "AT_RISK": 2, "PASSED": 3}
    return sorted(results, key=lambda result: (order.get(result.status, 9), result.policy_id))


def _server_of(result) -> str:
    """The machine a result belongs to: the first segment of ``target_id``.

    ``target_id`` is ``<server_id>/<db_type>/<service>``, so one server contributes several
    targets and, with 27 policies, tens of rows. A single flat table put 300 rows from 19 servers
    in one list ordered by status, which is the wrong axis for the question an operator actually
    has: "what is wrong with THIS server". Grouping by the server restores that.
    """
    target = str(getattr(result, "target_id", "") or "").strip()
    if not target or target == "*":
        return ""
    return target.split("/", 1)[0]


def _server_sections(summary) -> str:
    """One section per server, worst server first, plus a fleet-wide section for untargeted rows."""
    by_server: dict[str, list] = {}
    for result in summary.results:
        by_server.setdefault(_server_of(result), []).append(result)

    def rank(item) -> tuple:
        server, results = item
        failed = sum(1 for r in results if r.status == "FAILED")
        at_risk = sum(1 for r in results if r.status == "AT_RISK")
        # Fleet-wide rows last: they belong to no machine, so they answer no per-server question.
        return (0 if server else 1, -failed, -at_risk, server)

    output: list[str] = []
    for server, results in sorted(by_server.items(), key=rank):
        failed = sum(1 for r in results if r.status == "FAILED")
        at_risk = sum(1 for r in results if r.status == "AT_RISK")
        passed = sum(1 for r in results if r.status == "PASSED")
        title = html.escape(server) if server else "Fleet-wide (no single target)"
        css = "fail" if failed else ("warn" if at_risk else "ok")
        counts = (f'<span class="badge {css}">{failed} failed</span> '
                  f"{at_risk} at risk &middot; {passed} passed &middot; {len(results)} checks")
        rows = "\n".join(_html_policy_row(result) for result in _worst_first(results))
        output.append(
            f"<h3>{title}</h3><p class=\"muted\">{counts}</p>"
            '<div class="scroll"><table><thead><tr>'
            "<th>Status</th><th>Policy</th><th>Instance</th><th>Category</th><th>Now</th><th>SLI (actual)</th>"
            "<th>SLO (objective)</th><th>Budget left</th><th>Good/Total</th><th>Coverage / quality</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )
    return "".join(output) or '<p class="muted">No policy results.</p>'


def _now_cell(result: SlaPolicyResult) -> str:
    """The present tense, beside the rolling window.

    Without it a historical breach reads as an active incident. On ACME-192-0-2-250,
    OS_REBOOT_PENDING was warning 07-29 through 08-02 and OK on 08-03 and 08-04: the seven-day
    figure of 28.57% was arithmetically right and the host was not pending a reboot. Someone
    paged by that number goes looking for a problem that no longer exists.
    """
    if result.current_status == "OK":
        return '<span class="badge ok">OK now</span>'
    if result.current_status == "BAD":
        return '<span class="badge bad">bad now</span>'
    return '<span class="muted">—</span>'


def _actual_cell(result: SlaPolicyResult) -> str:
    """What this policy measured, in the terms its model actually means.

    The same column used to print a percentage for everything, so a maintenance backlog of 1,631
    objects appeared as "4.09 percentage" — a number that looks like availability and is not. Each
    model gets the reading that answers its own question.
    """
    if result.policy_model == "finding_inventory":
        return f"{result.affected_objects} objects affected"
    if result.policy_model == "current_state":
        verdict = "compliant" if result.actual_value else "not compliant"
        if result.affected_objects and not result.actual_value:
            return f"{html.escape(verdict)} ({result.affected_objects} affected)"
        return html.escape(verdict)
    value = result.actual_value if result.actual_value is not None else result.actual_percent
    return f"{value} {html.escape(result.unit)}"


def _html_policy_row(result: SlaPolicyResult) -> str:
    emoji, css = STATUS_DISPLAY.get(result.status, ("", "nodata"))
    instance = result.target_id if result.target_id and result.target_id != "*" else "(no data)"
    return (
        "<tr>"
        f'<td><span class="badge {css}">{emoji} {html.escape(result.status)}</span></td>'
        f"<td>{html.escape(result.policy_id)}</td>"
        f"<td>{html.escape(instance)}</td>"
        f"<td>{html.escape(result.category or '')}</td>"
        f"<td>{_now_cell(result)}</td>"
        f"<td>{_actual_cell(result)}</td>"
        f"<td>{html.escape(result.comparison_operator)} {result.objective_value if result.objective_value is not None else result.objective_percent}</td>"
        f"<td>{result.error_budget_remaining}</td>"
        f"<td>{result.good_count}/{result.total_count}</td>"
        f"<td>{result.coverage_percent}% / {html.escape(result.data_quality_status)}</td>"
        "</tr>"
    )


def _domain_sections(summary: SlaValidationSummary) -> str:
    sections = [
        ("Can serve business now?", {"availability"}),
        ("Can recover if failed?", {"backup", "recoverability", "data_protection"}),
        ("Replication and HA readiness", {"replication", "ha"}),
        ("Performance compliance", {"performance"}),
        ("Capacity risk", {"capacity"}),
        ("Integrity and operational health", {"integrity", "operational_health", "jobs"}),
        ("Monitoring/data quality", {"monitoring"}),
    ]
    output = ["<h2>Executive summary</h2><p class=\"muted\">Required SLIs determine the overall result; optional findings remain visible.</p>"]
    for title, domains in sections:
        results = [item for item in summary.results if (item.domain or item.category).lower() in domains]
        detail = ", ".join(f"{html.escape(item.sli_code or item.policy_id)}: {html.escape(item.status)}" for item in results)
        output.append(f"<h2>{html.escape(title)}</h2><p class=\"muted\">{detail or 'No configured SLI in this section.'}</p>")
    output.append("<h2>Error budget and burn rate</h2><p class=\"muted\">Detailed values are retained in JSON and Markdown output.</p>")
    output.append("<h2>Violations and recommended actions</h2><p class=\"muted\">Investigate FAILED, STALE, INSUFFICIENT_DATA, and NO_DATA before asserting compliance.</p>")
    output.append(f"<h2>Evidence and metric timestamps</h2><p class=\"muted\">Evaluation end: {html.escape(summary.window_end)}.</p>")
    return "".join(output)


def _html_history_row(run: dict) -> str:
    return (
        "<tr>"
        f"<td>#{run.get('sla_run_id', '')}</td>"
        f"<td>{html.escape(str(run.get('finished_at') or run.get('started_at') or ''))}</td>"
        f"<td>{html.escape(str(run.get('status') or ''))}</td>"
        f"<td>{run.get('passed_count', 0)}</td>"
        f"<td>{run.get('at_risk_count', 0)}</td>"
        f"<td>{run.get('failed_count', 0)}/{run.get('no_data_count', 0)}</td>"
        "</tr>"
    )


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DB Ops · SLA / SLO compliance</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #0f1419; color: #e6e6e6; }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  .sub {{ color: #9aa4af; font-size: .85rem; margin-bottom: 20px; }}
  .banner {{ display: inline-block; padding: 8px 16px; border-radius: 10px; font-weight: 600; margin-bottom: 20px; }}
  .banner.ok {{ background: #123d2b; color: #7ee2a8; }}
  .banner.warn {{ background: #3d3312; color: #f0d97a; }}
  .banner.bad {{ background: #3d1620; color: #f28ba0; }}
  .banner.nodata {{ background: #24303d; color: #9db4c7; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }}
  .card {{ background: #182029; border: 1px solid #263340; border-radius: 10px; padding: 12px 18px; min-width: 90px; }}
  .card .n {{ font-size: 1.6rem; font-weight: 700; }}
  .card .l {{ color: #9aa4af; font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; }}
  .scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .88rem; margin-bottom: 32px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #263340; white-space: nowrap; }}
  th {{ color: #9aa4af; font-weight: 600; font-size: .78rem; text-transform: uppercase; letter-spacing: .03em; }}
  .badge {{ padding: 2px 8px; border-radius: 6px; font-size: .8rem; font-weight: 600; }}
  .badge.ok {{ background: #123d2b; color: #7ee2a8; }}
  .badge.warn {{ background: #3d3312; color: #f0d97a; }}
  .badge.bad {{ background: #3d1620; color: #f28ba0; }}
  .badge.nodata {{ background: #24303d; color: #9db4c7; }}
  .muted {{ color: #7c8894; font-size: .82rem; }}
  h2 {{ font-size: 1rem; color: #cdd6df; margin: 8px 0 12px; }}
  @media (prefers-color-scheme: light) {{
    body {{ background: #f5f7fa; color: #1a2029; }}
    .card {{ background: #fff; border-color: #dbe2ea; }}
    th, td {{ border-color: #e3e9f0; }}
    .sub, .card .l, .muted {{ color: #5a6672; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>SLA / SLO compliance</h1>
  <div class="sub">Computed from collected metric_results · no database connections · generated {generated_at}</div>
  <div class="banner {banner_class}">{banner_emoji} Overall: {status} · window end {window_end}</div>
  <div class="cards">
    <div class="card"><div class="n">{serving_bad}</div><div class="l">Bad right now</div></div>
    <div class="card"><div class="n">{failed}</div><div class="l">Window breach</div></div>
    <div class="card"><div class="n">{debt_objects}</div><div class="l">Objects in backlog</div></div>
    <div class="card"><div class="n">{quality_bad}</div><div class="l">Cannot measure</div></div>
    <div class="card"><div class="n">{passed}</div><div class="l">Passed</div></div>
    <div class="card"><div class="n">{at_risk}</div><div class="l">At risk</div></div>
  </div>
  <p class="muted">{headline_note}</p>
  <p class="muted">{delta_note}</p>
  {domain_sections}
  <h2>Policies by server</h2>
  <p class="muted">One section per server, worst first. A server contributes several targets
  (<code>server_id/db_type/service</code>), so its checks are collected here rather than spread
  through one fleet-wide list.</p>
{rows}
  <h2>Recent runs</h2>
  <p class="muted">{history_note}</p>
  <div class="scroll">
  <table>
    <thead><tr><th>Run</th><th>Time</th><th>Status</th><th>Passed</th><th>At risk</th><th>Failed/No-data</th></tr></thead>
    <tbody>
{history}
    </tbody>
  </table>
  </div>
</div>
</body>
</html>
"""


_INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DB Ops · Reports</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #0f1419; color: #e6e6e6; }}
  .wrap {{ max-width: 760px; margin: 0 auto; padding: 40px 16px; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 4px; }}
  .sub {{ color: #9aa4af; font-size: .85rem; margin-bottom: 28px; }}
  .hub-card {{ display: block; text-decoration: none; color: inherit; background: #182029; border: 1px solid #263340;
              border-radius: 12px; padding: 20px 22px; margin-bottom: 14px; transition: border-color .15s, transform .05s; }}
  a.hub-card:hover {{ border-color: #3d5a73; transform: translateY(-1px); }}
  .hub-card.disabled {{ opacity: .55; }}
  .hub-title {{ font-size: 1.15rem; font-weight: 650; margin-bottom: 6px; }}
  .hub-note {{ color: #9aa4af; font-size: .88rem; }}
  .badge {{ padding: 2px 8px; border-radius: 6px; font-size: .8rem; font-weight: 600; }}
  .badge.ok {{ background: #123d2b; color: #7ee2a8; }}
  .badge.warn {{ background: #3d3312; color: #f0d97a; }}
  .badge.bad {{ background: #3d1620; color: #f28ba0; }}
  .badge.nodata {{ background: #24303d; color: #9db4c7; }}
  .muted {{ color: #7c8894; }}
  @media (prefers-color-scheme: light) {{
    body {{ background: #f5f7fa; color: #1a2029; }}
    .hub-card {{ background: #fff; border-color: #dbe2ea; }}
    .sub, .hub-note {{ color: #5a6672; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>DB Ops · Reports</h1>
  <div class="sub">Web host landing page · generated {generated_at}</div>
  {sla_card}
  {inventory_card}
</div>
</body>
</html>
"""
