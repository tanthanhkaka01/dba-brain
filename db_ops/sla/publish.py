"""Publish SLA results to the two db_ops delivery channels, without importing either app:

* Telegram — insert a row into ``telegram_send_messages`` (the telegram app's send queue).
* Web host — render a self-contained HTML page into the webhost serving root
  (``<runtime>/reports``), served at ``/report_dba/sla.html``.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from db_ops.lib import engine_sections, page_banner, page_style
from db_ops.sla.models import SlaPolicyResult, SlaValidationSummary, state_key
from db_ops.lib.timezone import file_stamp, format_display


# Status -> (emoji, css class) used in both the Telegram text and the HTML page.
STATUS_DISPLAY = {
    "PASSED": ("✅", "ok"),
    "AT_RISK": ("⚠️", "warn"),
    "FAILED": ("❌", "bad"),
    "NO_DATA": ("⬜", "nodata"),
    # Not a measurement: nothing was graded because nothing says what "good" is. It reads like
    # NO_DATA on purpose - blank, not green - and the summary's `reason` says which file is
    # missing. Green here would report an unjudged estate as compliant.
    "NOT_CONFIGURED": ("⬜", "nodata"),
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
                previous_state: dict[str, str] | None = None, history_limit: int = 0,
                report_dir: Path | None = None) -> str:
    generated_at = format_display()
    # Three levels, the way an SLO dashboard is read: the estate, each engine, each instance. The
    # page used to have one level only - a list of SLI codes per question ("AVAILABILITY_SUCCESS_
    # RATIO: STALE" sixteen times, no instance named) - and then every check of every server.
    instances = _instances(summary)
    history = "\n".join(_html_history_row(run) for run in recent_runs) or (
        '<tr><td colspan="6" class="muted">No stored runs.</td></tr>'
    )
    banner_emoji, banner_class = STATUS_DISPLAY.get(summary.status, ("", "nodata"))
    checks = len(summary.results)
    serving_bad = sum(1 for result in summary.results if result.current_status == "BAD")
    quality_bad = sum(1 for result in summary.results
                      if result.data_quality_status in UNMEASURABLE_QUALITY)
    return _PAGE_TEMPLATE.format(
        banner_css=page_banner.CSS,
        page_css=page_style.CSS,
        page_banner=page_banner.render(
            title="SLA / SLO compliance", snapshot_at=generated_at, here="sla.html",
            links=None if report_dir is None else page_banner.siblings_present(
                lambda name: name == "sla.html" or (report_dir / name).exists(),
                index_usage=page_banner.pick_index_usage(
                    path.name for path in report_dir.glob("index-usage_*.htm*")))),
        generated_at=html.escape(generated_at),
        status=html.escape(summary.status),
        banner_class=_PAGE_CLASS.get(banner_class, "idle"),
        banner_emoji=banner_emoji,
        window_end=html.escape(summary.window_end),
        scope_line=html.escape(_scope_line(instances, checks, summary.passed_count)),
        passed=summary.passed_count,
        at_risk=summary.at_risk_count,
        failed=summary.failed_count,
        serving_bad=serving_bad,
        serving_bad_class=page_style.kpi_class(serving_bad, "alert"),
        failed_class=page_style.kpi_class(summary.failed_count, "alert"),
        at_risk_class=page_style.kpi_class(summary.at_risk_count, "warnum"),
        quality_class=page_style.kpi_class(quality_bad, "warnum"),
        debt_objects=sum(result.affected_objects for result in summary.results
                         if result.policy_model == "finding_inventory"),
        quality_bad=quality_bad,
        headline_note=html.escape(_headline_note()),
        delta_note=html.escape(_delta_note(summary, previous_state)),
        history_note=html.escape(_history_note(recent_runs, history_limit)),
        engine_cards=_engine_cards(instances),
        matrix=_instance_matrix(instances, _area_columns(summary)),
        attention=_attention_table(instances),
        rows=_server_sections(summary, instances=instances),
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
    diff = diff_states(previous_state, current, severity_order=("FAILED", "AT_RISK", "NO_DATA", "NOT_CONFIGURED", "PASSED"),
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
                       history_limit=history_limit, report_dir=directory)
    stable_path = directory / "sla.html"
    stable_path.write_text(page, encoding="utf-8")
    # One archive per DAY, overwritten, via the shared helper — not one per run. Stamping every
    # hourly run left 696 files and 422 MB in the serving directory over four weeks, growing
    # without bound: an archive nobody prunes eventually costs more than the history is worth.
    # archive_daily also keeps the naming identical to the other published reports.
    from db_ops.lib.report_archive import archive_daily

    # The archive groups by day, so the stamp has to be on the same clock as every other
    # archived report - this one was UTC while all the rest were the host's.
    archive_daily([stable_path], stamp=file_stamp())
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
    generated_at = format_display()
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
        # Naming the command and what it waits for, because "not generated yet" reads as "wait and
        # it will appear" and on a fresh install it will not until something is monitored. The
        # command ships active since 2026-09-11 (it shipped inactive before, and this card was a
        # standing promise on a node where the page had 404'd since install); with no instance
        # registered it reports NOT_CONFIGURED and publishes nothing, which is correct and is
        # exactly what this card has to say. The same run writes `server-metrics.html` and the
        # `index-usage_*` pages, so all three are missing together and for this one reason.
        note=("Servers, storage, health triage · supports ?date=" if inventory_available else
              "not generated yet — produced hourly by "
              "<code>reports inventory-workflow --beauty 1</code> once an instance is registered "
              "in <code>data/db_instances.json</code>"),
        disabled=not inventory_available,
    )
    sla_card = _index_card(href="sla.html", emoji="📊", title="SLA / SLO compliance", note=sla_status, disabled=False)
    return _INDEX_TEMPLATE.format(
        banner_css=page_banner.CSS,
        page_banner=page_banner.render(
            title="Reports", snapshot_at=generated_at,
            links=page_banner.siblings_present(
                lambda name: (directory / name).exists(),
                index_usage=page_banner.pick_index_usage(
                    path.name for path in directory.glob("index-usage_*.htm*")))),
        generated_at=html.escape(generated_at),
        sla_card=sla_card, inventory_card=inventory_card)


def _index_card(*, href: str, emoji: str, title: str, note: str, disabled: bool) -> str:
    cls = "hub-card disabled" if disabled else "hub-card"
    inner = (
        f'<div class="hub-title">{emoji} {html.escape(title)}</div>'
        f'<div class="hub-note">{note}</div>'
    )
    if disabled:
        return f'<div class="{cls}">{inner}</div>'
    return f'<a class="{cls}" href="{html.escape(href)}">{inner}</a>'


#: Worst first. A check nobody could measure sorts above one merely at risk: "we cannot see it" is
#: the reading an operator can least afford to scroll past.
_STATUS_ORDER = {"FAILED": 0, "NO_DATA": 1, "STALE": 1, "INSUFFICIENT_DATA": 1, "NOT_CONFIGURED": 1,
                 "AT_RISK": 2, "PASSED": 3}

#: STATUS_DISPLAY's class, in this page's palette (the inventory page's): ok / warn / crit / idle.
_PAGE_CLASS = {"ok": "ok", "warn": "warn", "bad": "crit", "nodata": "idle"}


def _status_rank(status: str) -> int:
    return _STATUS_ORDER.get(str(status or ""), 9)


def _status_class(status: str) -> str:
    return _PAGE_CLASS.get(STATUS_DISPLAY.get(status, ("", "nodata"))[1], "idle")


def _worst_first(results) -> list[SlaPolicyResult]:
    return sorted(results, key=lambda result: (_status_rank(result.status), result.policy_id))


def _worst_status(results) -> str:
    return min((result.status for result in results), key=_status_rank, default="NO_DATA")


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


def _engine_of(result) -> str:
    """The engine section a result belongs to - the middle segment of ``target_id``.

    ``""`` for a fleet-wide result (``*``): it names no machine, so it belongs to no engine either.
    """
    target = str(getattr(result, "target_id", "") or "").strip()
    if not target or target == "*":
        return ""
    parts = target.split("/")
    return engine_sections.section_of(parts[1] if len(parts) > 1 else "")


#: The engine key and label of the results that name no machine. Last on every list.
_FLEET_KEY, _FLEET_LABEL = "", "Fleet-wide (no single target)"


def _instances(summary) -> list[dict]:
    """One entry per instance (``server_id``): its engine, its results, its verdict and counts.

    Ordered the way every section of the page reads them: by engine, then worst instance first,
    then by name - so the SQL Server instance that is failing is the first SQL Server row in the
    matrix, the first SQL Server card line and the first SQL Server detail block.
    """
    by_server: dict[str, list] = {}
    for result in summary.results:
        by_server.setdefault(_server_of(result), []).append(result)
    instances = []
    for server, results in by_server.items():
        engine = next((key for key in (_engine_of(result) for result in results) if key), "")
        if server and not engine:
            engine = "other"
        instances.append({
            "server": server,
            "engine": engine,
            "results": results,
            "status": _worst_status(results),
            "failed": sum(1 for r in results if r.status == "FAILED"),
            "at_risk": sum(1 for r in results if r.status == "AT_RISK"),
            "passed": sum(1 for r in results if r.status == "PASSED"),
            "no_data": sum(1 for r in results if _status_rank(r.status) == 1),
            "bad_now": sum(1 for r in results if getattr(r, "current_status", "") == "BAD"),
        })

    def rank(entry) -> tuple:
        # Fleet-wide rows last: they belong to no machine, so they answer no per-server question.
        engine_rank = engine_sections.section_rank(entry["engine"]) if entry["server"] else 99
        return (engine_rank, -entry["failed"], -entry["no_data"], -entry["at_risk"], entry["server"])

    return sorted(instances, key=rank)


def _engines(instances: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """``(key, label, instances)`` per engine present, in page order, fleet-wide last."""
    groups: list[tuple[str, str, list[dict]]] = []
    for key, label in engine_sections.present_sections(
            entry["engine"] for entry in instances if entry["server"]):
        groups.append((key, label, [entry for entry in instances
                                    if entry["server"] and entry["engine"] == key]))
    fleet = [entry for entry in instances if not entry["server"]]
    if fleet:
        groups.append((_FLEET_KEY, _FLEET_LABEL, fleet))
    return groups


def _anchor(server: str) -> str:
    """The id of an instance's detail block, so a matrix row and a card line can link to it."""
    slug = "".join(ch if ch.isalnum() else "-" for ch in (server or "fleet").lower()).strip("-")
    return f"inst-{slug or 'fleet'}"


def _instance_link(entry: dict) -> str:
    name = html.escape(entry["server"]) if entry["server"] else _FLEET_LABEL
    return f'<a href="#{_anchor(entry["server"])}">{name}</a>'


def _badge(status: str, text: str | None = None) -> str:
    emoji = STATUS_DISPLAY.get(status, ("", "nodata"))[0]
    label = html.escape(text if text is not None else status)
    return f'<span class="badge b-{_status_class(status)}">{emoji} {label}</span>'


def _scope_line(instances: list[dict], checks: int, passed: int) -> str:
    machines = sum(1 for entry in instances if entry["server"])
    engines = len({entry["engine"] for entry in instances if entry["server"]})
    return (f"{machines} instance{'s' if machines != 1 else ''} on {engines} "
            f"engine{'s' if engines != 1 else ''} · {passed} of {checks} checks passed")


def _engine_cards(instances: list[dict]) -> str:
    """One card per engine: its verdict, how much of it passes, and the instances to look at."""
    cards = []
    for _key, label, members in _engines(instances):
        results = [result for entry in members for result in entry["results"]]
        status = _worst_status(results)
        passed = sum(entry["passed"] for entry in members)
        share = round(100.0 * passed / len(results)) if results else 0
        counts = (f'<span class="c crit">{sum(e["failed"] for e in members)} failed</span>'
                  f'<span class="c idle">{sum(e["no_data"] for e in members)} cannot measure</span>'
                  f'<span class="c warn">{sum(e["at_risk"] for e in members)} at risk</span>'
                  f'<span class="c ok">{passed} passed</span>')
        attention = [entry for entry in members if entry["status"] != "PASSED"]
        if attention:
            names = ", ".join(_instance_link(entry) for entry in attention[:8])
            more = f" and {len(attention) - 8} more" if len(attention) > 8 else ""
            look = f'<div class="look"><b>Look at:</b> {names}{more}</div>'
        else:
            look = '<div class="look ok-text">Every instance passes every check.</div>'
        bad_now = sum(entry["bad_now"] for entry in members)
        now = (f'<span class="now crit">{bad_now} bad right now</span>' if bad_now
               else '<span class="now ok">nothing bad right now</span>')
        machines = len(members) if members and members[0]["server"] else 0
        cards.append(
            f'<div class="ecard {_status_class(status)}">'
            f'<div class="ehead"><span class="ename">{html.escape(label)}</span>{_badge(status)}</div>'
            f'<div class="ebig"><span class="pct">{share}%</span>'
            f'<span class="of">of {len(results)} checks passed'
            + (f" · {machines} instance{'s' if machines != 1 else ''}" if machines else "")
            + f"</span></div>"
            f'<div class="counts">{counts}</div>{now}{look}</div>'
        )
    return "".join(cards) or '<p class="muted">No policy results.</p>'


#: The SLI areas the matrix has a column for, in reading order. A result's ``domain`` (or its
#: ``category`` when it has none) picks the column; the same groups the page's old per-question
#: headings used, so a check does not move area because the layout changed.
_AREAS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("availability", "Availability", frozenset({"availability"})),
    ("recovery", "Backup &amp; recovery", frozenset({"backup", "recoverability", "data_protection"})),
    ("replication", "Replication / HA", frozenset({"replication", "ha"})),
    ("performance", "Performance", frozenset({"performance"})),
    ("capacity", "Capacity", frozenset({"capacity"})),
    ("integrity", "Integrity &amp; operations", frozenset({"integrity", "operational_health", "jobs"})),
    ("monitoring", "Monitoring", frozenset({"monitoring"})),
)


def _area_of(result) -> str:
    domain = str(getattr(result, "domain", "") or getattr(result, "category", "") or "").lower()
    return next((key for key, _label, domains in _AREAS if domain in domains), "other")


def _area_columns(summary) -> list[tuple[str, str]]:
    """The areas at least one result falls in. A column of dashes says nothing."""
    present = {_area_of(result) for result in summary.results}
    columns = [(key, label) for key, label, _domains in _AREAS if key in present]
    if "other" in present:
        columns.append(("other", "Other"))
    return columns


def _reading(result) -> str:
    """The shortest honest reading of one result, for a matrix cell."""
    if result.policy_model == "finding_inventory":
        return f"{result.affected_objects} obj"
    if result.policy_model == "current_state":
        return "ok" if result.actual_value else "not ok"
    if _status_rank(result.status) == 1:
        return "no data"
    value = result.actual_value if result.actual_value is not None else result.actual_percent
    return _number_with_unit(value, result.unit)


def _cell_title(result) -> str:
    objective = (result.objective_value if result.objective_value is not None
                 else result.objective_percent)
    return html.escape(
        f"{result.policy_id}: {result.status} · measured {_reading(result)} · objective "
        f"{result.comparison_operator} {objective} · now {getattr(result, 'current_status', '') or '-'}"
        f" · data {result.data_quality_status}", quote=True)


def _instance_matrix(instances: list[dict], columns: list[tuple[str, str]]) -> str:
    """Every instance against every SLI area, grouped by engine; the worst result fills a cell."""
    if not instances:
        return '<p class="muted">No policy results.</p>'
    head = ("<thead><tr><th>Instance</th><th>Verdict</th>"
            + "".join(f'<th class="center">{label}</th>' for _key, label in columns)
            + '<th class="center">Bad now</th></tr></thead>')
    body = []
    span = len(columns) + 3
    for _key, label, members in _engines(instances):
        body.append(f'<tr class="grp"><th colspan="{span}">{html.escape(label)}'
                    f'<span class="gcount">{len(members)}</span></th></tr>')
        for entry in members:
            cells = []
            for area, _label in columns:
                results = _worst_first(r for r in entry["results"] if _area_of(r) == area)
                if not results:
                    cells.append('<td class="center na">—</td>')
                    continue
                worst = results[0]
                extra = f'<span class="more">+{len(results) - 1}</span>' if len(results) > 1 else ""
                cells.append(f'<td class="center"><span class="cell b-{_status_class(worst.status)}" '
                             f'title="{_cell_title(worst)}">{_reading(worst)}</span>{extra}</td>')
            now = (f'<span class="now crit">{entry["bad_now"]}</span>' if entry["bad_now"]
                   else '<span class="muted">0</span>')
            body.append(f'<tr><td class="srv">{_instance_link(entry)}</td>'
                        f"<td>{_badge(entry['status'])}</td>{''.join(cells)}"
                        f'<td class="center">{now}</td></tr>')
    return f'<div class="tbl-scroll"><table class="matrix">{head}<tbody>{"".join(body)}</tbody></table></div>'


def _attention_table(instances: list[dict]) -> str:
    """Every check that did not pass, worst first, each naming its engine and its instance."""
    rows = []
    for entry in instances:
        for result in entry["results"]:
            if result.status == "PASSED":
                continue
            rows.append((entry, result))
    if not rows:
        return '<p class="good-line">✅ Nothing - every check passed.</p>'
    rows.sort(key=lambda pair: (_status_rank(pair[1].status),
                                0 if getattr(pair[1], "current_status", "") == "BAD" else 1,
                                engine_sections.section_rank(pair[0]["engine"]),
                                pair[0]["server"], pair[1].policy_id))
    body = []
    for entry, result in rows:
        engine = (engine_sections.section_label(entry["engine"]) if entry["server"]
                  else "Fleet-wide")
        objective = (result.objective_value if result.objective_value is not None
                     else result.objective_percent)
        reason = str(getattr(result, "reason", "") or "")
        body.append(
            "<tr>"
            f"<td>{_badge(result.status)}</td>"
            f"<td>{html.escape(engine)}</td>"
            f'<td class="srv">{_instance_link(entry)}</td>'
            f"<td>{html.escape(result.policy_id)}</td>"
            f"<td>{_now_cell(result)}</td>"
            f'<td class="num-cell">{_actual_cell(result)}</td>'
            f'<td class="num-cell">{html.escape(result.comparison_operator)} {objective}</td>'
            f"<td>{html.escape(result.data_quality_status)}</td>"
            f'<td class="prose">{html.escape(reason)}</td>'
            "</tr>"
        )
    return ('<div class="tbl-scroll"><table><thead><tr><th>Status</th><th>Engine</th><th>Instance</th>'
            "<th>Check</th><th>Now</th><th>Measured</th><th>Objective</th><th>Data</th><th>Why</th>"
            f'</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def _server_sections(summary, *, instances: list[dict] | None = None) -> str:
    """Every instance's checks, one block per server, grouped under its engine.

    A block is open when something in it did not pass: the page opens on what needs reading and
    keeps the 40 healthy instances one click away instead of 400 rows down.
    """
    instances = _instances(summary) if instances is None else instances
    output: list[str] = []
    for key, label, members in _engines(instances):
        if key != _FLEET_KEY:
            output.append(f'<h3 class="eng-head">{html.escape(label)}'
                          f'<span class="gcount">{len(members)}</span></h3>')
        for entry in members:
            title = html.escape(entry["server"]) if entry["server"] else _FLEET_LABEL
            counts = (f'{entry["failed"]} failed &middot; {entry["at_risk"]} at risk &middot; '
                      f'{entry["passed"]} passed &middot; {len(entry["results"])} checks')
            rows = "\n".join(_html_policy_row(result) for result in _worst_first(entry["results"]))
            opened = " open" if entry["status"] != "PASSED" else ""
            output.append(
                f'<details class="inst" id="{_anchor(entry["server"])}"{opened}>'
                f'<summary><span class="dot {_status_class(entry["status"])}"></span>'
                f'<span class="iname">{title}</span>{_badge(entry["status"])}'
                f'<span class="icount">{counts}</span></summary>'
                '<div class="tbl-scroll"><table><thead><tr>'
                "<th>Status</th><th>Check</th><th>Target</th><th>Area</th><th>Now</th><th>Measured</th>"
                "<th>Objective</th><th>Budget left</th><th>Good / total</th><th>Coverage / data</th>"
                f"</tr></thead><tbody>{rows}</tbody></table></div></details>"
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
        return '<span class="badge b-ok">OK now</span>'
    if result.current_status == "BAD":
        return '<span class="badge b-crit">bad now</span>'
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
    return _number_with_unit(value, result.unit)


def _number_with_unit(value, unit) -> str:
    """``94.87 %`` rather than ``94.87327188940093 percentage``: two decimals say all there is."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{html.escape(str(value))} {html.escape(str(unit or ''))}".strip()
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    if str(unit or "").lower() in ("percentage", "percent", "%"):
        return f"{text}%"
    return f"{text} {html.escape(str(unit or ''))}".strip()


def _target_cell(result) -> str:
    """The target inside its instance's block: ``db_type/service``, the server being the heading."""
    target = str(result.target_id or "")
    if not target or target == "*":
        return "(no data)"
    return html.escape(target.split("/", 1)[1] if "/" in target else target)


def _html_policy_row(result: SlaPolicyResult) -> str:
    objective = result.objective_value if result.objective_value is not None else result.objective_percent
    return (
        "<tr>"
        f"<td>{_badge(result.status)}</td>"
        f"<td>{html.escape(result.policy_id)}</td>"
        f'<td class="srv">{_target_cell(result)}</td>'
        f"<td>{html.escape(result.category or '')}</td>"
        f"<td>{_now_cell(result)}</td>"
        f'<td class="num-cell">{_actual_cell(result)}</td>'
        f'<td class="num-cell">{html.escape(result.comparison_operator)} {objective}</td>'
        f'<td class="num-cell">{_number_with_unit(result.error_budget_remaining, "")}</td>'
        f'<td class="num-cell">{result.good_count}/{result.total_count}</td>'
        f"<td>{result.coverage_percent}% / {html.escape(result.data_quality_status)}</td>"
        "</tr>"
    )


def _html_history_row(run: dict) -> str:
    return (
        "<tr>"
        f"<td>#{run.get('sla_run_id', '')}</td>"
        f"<td>{html.escape(str(run.get('finished_at') or run.get('started_at') or ''))}</td>"
        f"<td>{_badge(str(run.get('status') or ''))}</td>"
        f'<td class="num-cell">{run.get("passed_count", 0)}</td>'
        f'<td class="num-cell">{run.get("at_risk_count", 0)}</td>'
        f'<td class="num-cell">{run.get("failed_count", 0)}/{run.get("no_data_count", 0)}</td>'
        "</tr>"
    )


#: The page. Light, and in the inventory page's palette and parts - masthead, KPI strip, section
#: heads, bordered tables, badges - so the reports read as one product. It was the one dark page
#: among them, with a layout of its own that the operator called the ugliest of the set.
_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DB Ops · SLA / SLO compliance</title>
<style>
{page_css}
  .engines {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:12px; }}
  .ecard {{ background:var(--surface); border:1px solid var(--line); border-top:4px solid var(--faint); border-radius:10px; padding:13px 15px; }}
  .ecard.ok {{ border-top-color:var(--ok); }} .ecard.warn {{ border-top-color:var(--warn); }}
  .ecard.crit {{ border-top-color:var(--crit); }} .ecard.idle {{ border-top-color:var(--faint); }}
  .ehead {{ display:flex; justify-content:space-between; align-items:center; gap:8px; }}
  .ename {{ font-size:15px; font-weight:700; }}
  .ebig {{ margin:8px 0 6px; }}
  .ebig .pct {{ font-size:28px; font-weight:700; letter-spacing:-.02em; margin-right:6px; }}
  .ebig .of {{ font-size:12px; color:var(--muted); }}
  .counts {{ display:flex; flex-wrap:wrap; gap:4px 10px; font-size:12px; }}
  .counts .c.crit {{ color:var(--crit); }} .counts .c.warn {{ color:var(--warn); }}
  .counts .c.ok {{ color:var(--ok); }} .counts .c.idle {{ color:var(--muted); }}
  .now {{ display:inline-block; margin-top:6px; font-size:12px; font-weight:600; }}
  .now.crit {{ color:var(--crit); }} .now.ok {{ color:var(--ok); }}
  .look {{ margin-top:6px; font-size:12px; color:#3a4757; }}
  .look a {{ color:var(--link); text-decoration:none; font-family:var(--mono); font-size:11.5px; }}
  .ok-text {{ color:var(--ok); }}
  tbody td {{ white-space:nowrap; }}
  tr.grp th {{ background:#eef2f7; text-align:left; font-size:12px; letter-spacing:.04em; text-transform:uppercase; color:var(--brand); padding:7px 11px; border-bottom:1px solid var(--line); }}
  .gcount {{ display:inline-block; margin-left:8px; font-size:11px; font-weight:600; color:var(--muted); background:var(--surface); border:1px solid var(--line); border-radius:999px; padding:0 7px; text-transform:none; letter-spacing:0; }}
  td.srv a {{ color:var(--ink); text-decoration:none; font-weight:600; }}
  td.srv a:hover {{ color:var(--link); }}
  td.na {{ color:var(--faint); }}
  .cell {{ font-family:var(--mono); min-width:58px; text-align:center; cursor:default; }}
  .more {{ font-size:10.5px; color:var(--muted); margin-left:4px; }}
  .good-line {{ color:var(--ok); font-weight:600; }}
  h3.eng-head {{ font-size:14px; letter-spacing:.04em; text-transform:uppercase; color:var(--brand); margin:20px 0 8px; }}
  details.inst {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; margin-bottom:8px; }}
  details.inst > summary {{ cursor:pointer; list-style:none; display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:10px 14px; }}
  details.inst > summary::-webkit-details-marker {{ display:none; }}
  details.inst > summary::before {{ content:"\\25b8"; color:var(--faint); }}
  details.inst[open] > summary::before {{ content:"\\25be"; }}
  details.inst .iname {{ font-family:var(--mono); font-weight:650; }}
  details.inst .icount {{ font-size:12px; color:var(--muted); margin-left:auto; }}
  details.inst .tbl-scroll {{ border:none; border-top:1px solid var(--line); border-radius:0 0 10px 10px; margin:0; }}
  @media print {{ details.inst {{ break-inside:avoid; }} }}
{banner_css}
</style>
</head>
<body>
<header class="masthead">
  <div class="wrap">
  {page_banner}
  <h1 class="title">SLA / SLO compliance</h1>
  <p class="subtitle">Window end {window_end} · computed from collected metric results, no database connections</p>
  <div class="verdict {banner_class}">{banner_emoji} Overall: {status} <span class="scope">{scope_line}</span></div>
  <div class="kpi-strip">
    <div class="kpi {serving_bad_class}"><div class="num">{serving_bad}</div><div class="lbl">Bad right now</div></div>
    <div class="kpi {failed_class}"><div class="num">{failed}</div><div class="lbl">Window breach</div></div>
    <div class="kpi {at_risk_class}"><div class="num">{at_risk}</div><div class="lbl">At risk</div></div>
    <div class="kpi {quality_class}"><div class="num">{quality_bad}</div><div class="lbl">Cannot measure</div></div>
    <div class="kpi"><div class="num">{debt_objects}</div><div class="lbl">Objects in backlog</div></div>
    <div class="kpi good"><div class="num">{passed}</div><div class="lbl">Passed</div></div>
  </div>
  </div>
</header>
<div class="wrap">
  <div class="notes"><p>{headline_note}</p><p>{delta_note}</p></div>

  <section>
    <div class="sec-head"><h2>By database engine</h2>
      <span class="hint">One card per engine: its verdict, how many of its checks pass, and which instances to look at.</span></div>
    <div class="engines">{engine_cards}</div>
  </section>

  <section>
    <div class="sec-head"><h2>Instance matrix</h2>
      <span class="hint">Every instance against every SLI area, grouped by engine. A cell is the worst check in that area; hover it for the policy, the objective and the data quality. Click an instance for all its checks.</span></div>
    {matrix}
    <div class="legend"><span><span class="badge b-ok">passed</span></span><span><span class="badge b-warn">at risk</span></span>
      <span><span class="badge b-crit">failed</span></span><span><span class="badge b-idle">no data</span> cannot measure</span><span>— the area has no check for this instance</span></div>
  </section>

  <section>
    <div class="sec-head"><h2>Needs attention</h2>
      <span class="hint">Every check that did not pass, worst first. <b>Now</b> is the newest collection; the status is the rolling window.</span></div>
    {attention}
  </section>

  <section>
    <div class="sec-head"><h2>Instance detail</h2>
      <span class="hint">Grouped by engine. An instance with anything not passing is open; the rest are one click away. A server contributes several targets (<code>server_id/db_type/service</code>), so they are listed together.</span></div>
{rows}
  </section>

  <section>
    <div class="sec-head"><h2>Recent runs</h2><span class="hint">{history_note}</span></div>
    <div class="tbl-scroll">
    <table>
      <thead><tr><th>Run</th><th>Time</th><th>Status</th><th class="num-cell">Passed</th><th class="num-cell">At risk</th><th class="num-cell">Failed / no data</th></tr></thead>
      <tbody>
{history}
      </tbody>
    </table>
    </div>
  </section>
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
{banner_css}
</style>
</head>
<body>
<div class="wrap">
  {page_banner}
  <div class="sub">Web host landing page</div>
  {sla_card}
  {inventory_card}
</div>
</body>
</html>
"""
